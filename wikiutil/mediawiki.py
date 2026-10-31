"""MediaWiki APIへの要求を、間隔制御と再試行付きで共通化する。"""

import email.utils
import time
from collections.abc import Callable, Collection, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

import requests

USER_AGENT = (
    "wikipedia-util/0.1 (Wikipedia maintenance tools; "
    "contact via ja.wikipedia.org)"
)
WIKIDATA_API_URL = "https://www.wikidata.org/w/api.php"
REQUEST_TIMEOUT_SECONDS = 30
DEFAULT_MIN_INTERVAL_SECONDS = 1.0
MAX_RETRIES = 5
MAX_BACKOFF_SECONDS = 60
DEFAULT_MAXLAG_SECONDS = 5
# maxlag の応答に Retry-After がない場合の待機（秒）。
DEFAULT_MAXLAG_WAIT_SECONDS = 5
RETRY_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
# MediaWikiがページ名から取り除く方向制御文字（LRM・RLM・LRE〜RLO）。
_DIRECTION_MARKS = dict.fromkeys([0x200E, 0x200F, *range(0x202A, 0x202F)])


class MediaWikiError(RuntimeError):
    """MediaWiki API操作の失敗を表す。"""


class MediaWikiAPIError(MediaWikiError):
    """APIがエラーを返した。エラーコードを `code` に持つ。"""

    def __init__(self, error: Any) -> None:
        """応答の `error` を受け取る。"""
        super().__init__("MediaWiki APIがエラーを返しました: {}".format(error))
        self.code = error.get("code") if isinstance(error, dict) else None


def api_url(language: str) -> str:
    """言語コードに対応するWikipediaのAPIのURLを返す。"""
    return "https://{}.wikipedia.org/w/api.php".format(language)


def title_key(title: str) -> str:
    """比較用にページ名の空白とアンダースコアを正規化する。"""
    return title.replace("_", " ").strip().casefold()


def strip_direction_marks(title: str) -> str:
    """MediaWikiと同じく、ページ名から方向制御文字を取り除く。"""
    return title.translate(_DIRECTION_MARKS)


def parse_retry_after(value: str | None, now: datetime) -> datetime | None:
    """Retry-Afterヘッダーを秒数またはHTTP日時として解析する。

    Args:
        value: Retry-Afterヘッダーの値。
        now: 秒数指定の基準にする現在時刻。

    Returns:
        再試行してよい時刻。解析できない場合はNone。
    """
    if not value:
        return None
    try:
        seconds = float(value.strip())
    except ValueError:
        try:
            parsed = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return now + timedelta(seconds=max(0.0, seconds))


def json_from_response(response: requests.Response) -> dict[str, Any]:
    """HTTP応答を検査し、MediaWiki APIのJSONオブジェクトを返す。

    Raises:
        requests.HTTPError: HTTPエラー応答の場合。
        MediaWikiError: JSONがオブジェクトでない場合。
        MediaWikiAPIError: APIエラーを含む場合。
    """
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise MediaWikiError("MediaWiki APIの応答の形式が不正です")
    if "error" in data:
        raise MediaWikiAPIError(data["error"])
    return data


class MediaWikiSession(requests.Session):
    """ホストごとの要求間隔を守り、一時的な失敗を再試行するSession。

    User-Agentと既定のタイムアウトを設定する。再試行は `retry_methods` に
    含まれるHTTPメソッドだけに行い、編集のように副作用のある要求を
    重複して送らないようにする。
    """

    def __init__(
        self,
        *,
        user_agent: str = USER_AGENT,
        timeout: Any = REQUEST_TIMEOUT_SECONDS,
        min_intervals: Mapping[str, float] | None = None,
        default_interval: float = DEFAULT_MIN_INTERVAL_SECONDS,
        max_retries: int = MAX_RETRIES,
        retry_methods: Collection[str] = ("GET",),
        on_retry: Callable[[str], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """要求間隔、再試行、ログ出力の設定を受け取る。

        Args:
            user_agent: 全要求に付けるUser-Agent。
            timeout: 要求ごとに指定がない場合のタイムアウト。
            min_intervals: ホスト名ごとの最小要求間隔（秒）。
            default_interval: `min_intervals` にないホストの最小間隔（秒）。
            max_retries: 最初の要求に加えて再試行する最大回数。
            retry_methods: 再試行してよいHTTPメソッド。
            on_retry: 再試行の理由を受け取るログ関数。
            sleep: 待機に使う関数。テストで差し替える。
        """
        super().__init__()
        self.headers.update({"User-Agent": user_agent})
        self.timeout = timeout
        self.min_intervals = dict(min_intervals or {})
        self.default_interval = default_interval
        self.max_retries = max_retries
        self.retry_methods = frozenset(
            method.upper() for method in retry_methods
        )
        self.on_retry = on_retry
        self.sleep = sleep
        self._last_requests: dict[str, float] = {}

    def _wait_for_host(self, host: str) -> None:
        """同じホストへの前回要求から最小間隔が空くまで待つ。"""
        last = self._last_requests.get(host)
        if last is None:
            return
        interval = self.min_intervals.get(host, self.default_interval)
        wait = interval - (time.monotonic() - last)
        if wait > 0:
            self.sleep(wait)

    def _retry(self, reason: str, url: str, delay: float) -> None:
        """再試行の理由をログに出し、指定秒数だけ待つ。"""
        if self.on_retry is not None:
            self.on_retry(
                "再試行: {} 原因={} 待機={:.1f}秒".format(url, reason, delay)
            )
        self.sleep(delay)

    @staticmethod
    def _backoff(attempt: int) -> float:
        """試行回数に応じた指数バックオフの秒数を返す。"""
        return min(2**attempt, MAX_BACKOFF_SECONDS)

    def request(
        self, method: str | bytes, url: str | bytes, *args: Any, **kwargs: Any
    ) -> requests.Response:
        """間隔制御と再試行を行ってHTTP要求を送る。

        `retry=False` を渡すと、`retry_methods` に含まれるメソッドでも
        再試行しない。

        Raises:
            requests.ConnectionError: 再試行後も接続できない場合。
            requests.Timeout: 再試行後もタイムアウトした場合。
            requests.exceptions.ChunkedEncodingError: 再試行後も応答が
                途中で切れる場合。
        """
        kwargs.setdefault("timeout", self.timeout)
        retry = kwargs.pop("retry", True)
        method_name = (
            method.decode() if isinstance(method, bytes) else method
        ).upper()
        url_text = url.decode() if isinstance(url, bytes) else url
        host = urlparse(url_text).hostname or ""
        retryable = retry and method_name in self.retry_methods
        attempt = 0
        while True:
            self._wait_for_host(host)
            try:
                response = super().request(method, url, *args, **kwargs)
            except (
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
                # 応答の本文を読む途中で接続が切れた場合
                requests.exceptions.ChunkedEncodingError,
            ) as error:
                self._last_requests[host] = time.monotonic()
                if not retryable or attempt >= self.max_retries:
                    raise
                reason = "{}: {}".format(type(error).__name__, error)
                self._retry(reason, url_text, self._backoff(attempt))
                attempt += 1
                continue
            self._last_requests[host] = time.monotonic()
            if (
                not retryable
                or response.status_code not in RETRY_STATUS_CODES
                or attempt >= self.max_retries
            ):
                return response
            now = datetime.now(timezone.utc)
            retry_header = response.headers.get("Retry-After")
            retry_at = parse_retry_after(retry_header, now)
            delay = (
                (retry_at - now).total_seconds()
                if retry_at is not None
                else self._backoff(attempt)
            )
            reason = "HTTP {}".format(response.status_code)
            if retry_header:
                reason += " (Retry-After={})".format(retry_header)
            response.close()
            self._retry(reason, url_text, max(0.0, delay))
            attempt += 1

    def get_json(
        self, url: str, params: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """GET要求を送り、MediaWiki APIのJSON応答を返す。"""
        return json_from_response(self.get(url, params=params))

    def post_json(self, url: str, data: Mapping[str, Any]) -> dict[str, Any]:
        """POST要求を送り、MediaWiki APIのJSON応答を返す。"""
        return json_from_response(self.post(url, data=data))

    def _token(self, url: str, token_type: str) -> str:
        """指定した種類のトークンを取得する。"""
        data = self.get_json(
            url,
            {
                "action": "query",
                "meta": "tokens",
                "type": token_type,
                "format": "json",
                "formatversion": 2,
            },
        )
        token = data.get("query", {}).get("tokens", {}).get(
            "{}token".format(token_type)
        )
        if not isinstance(token, str):
            raise MediaWikiError(
                "{}トークンを取得できません".format(token_type)
            )
        return token

    def login(self, url: str, username: str, password: str) -> None:
        """BotPasswordでログインする。以降の要求はログインした状態で送る。

        Args:
            url: APIのURL。
            username: `利用者名@BotPassword名` の形のログイン名。
            password: BotPasswordのパスワード。

        Raises:
            MediaWikiError: ログインできない場合。パスワードは含めない。
        """
        token = self._token(url, "login")
        data = self.post_json(
            url,
            {
                "action": "login",
                "lgname": username,
                "lgpassword": password,
                "lgtoken": token,
                "format": "json",
                "formatversion": 2,
            },
        )
        result = data.get("login", {})
        if result.get("result") != "Success":
            raise MediaWikiError(
                "BotPasswordでログインできません: {} {}".format(
                    result.get("result"), result.get("reason", "")
                ).rstrip()
            )

    def csrf_token(self, url: str) -> str:
        """編集に使うCSRFトークンを取得する。"""
        return self._token(url, "csrf")

    def user_info(self, url: str) -> dict[str, Any]:
        """ログイン中の利用者の名前・グループ・新着メッセージの有無を返す。

        Returns:
            `meta=userinfo` の応答。`name`・`groups` を持ち、会話ページに
            新しいメッセージがあれば `messages` が真になる。
        """
        data = self.get_json(
            url,
            {
                "action": "query",
                "meta": "userinfo",
                "uiprop": "groups|hasmsg",
                "format": "json",
                "formatversion": 2,
            },
        )
        info = data.get("query", {}).get("userinfo")
        if not isinstance(info, dict):
            raise MediaWikiError("利用者の情報を取得できません")
        return info

    def edit(
        self,
        url: str,
        *,
        title: str,
        text: str,
        summary: str,
        token: str,
        base_revision_id: int,
        base_timestamp: str,
        start_timestamp: str,
        bot: bool,
        assertion: str = "user",
        maxlag: int = DEFAULT_MAXLAG_SECONDS,
    ) -> dict[str, Any]:
        """既存のページの本文を置き換える。

        編集の競合と、取得後の削除を検出するため、本文を取得した版と時刻を
        渡す。ページがなければ作らない。HTTPの失敗では送り直さない（編集が
        反映されたか分からないため）。`maxlag` で断られた場合は、反映されて
        いないので、待って送り直す。

        Args:
            url: APIのURL。
            title: ページ名。
            text: 新しい本文。
            summary: 編集の要約。
            token: CSRFトークン。
            base_revision_id: 本文を取得した版のID。
            base_timestamp: 本文を取得した版の時刻。
            start_timestamp: 本文の取得を始めた時刻。
            bot: ボットの編集として記録するか。
            assertion: 送る前に確かめる状態（`user` または `bot`）。
            maxlag: サーバーの遅延の許容秒数。

        Returns:
            応答の `edit`。`newrevid` を持ち、本文が同じなら `nochange` を持つ。

        Raises:
            MediaWikiAPIError: 編集の競合などのAPIエラーの場合。
            MediaWikiError: 結果が `Success` でない場合や、混雑が続く場合。
        """
        data = {
            "action": "edit",
            "title": title,
            "text": text,
            "summary": summary,
            "token": token,
            "baserevid": base_revision_id,
            "basetimestamp": base_timestamp,
            "starttimestamp": start_timestamp,
            "nocreate": 1,
            "watchlist": "nochange",
            "assert": assertion,
            "maxlag": maxlag,
            "format": "json",
            "formatversion": 2,
        }
        if bot:
            data["bot"] = 1
        for attempt in range(self.max_retries + 1):
            response = self.post(url, data=data, retry=False)
            try:
                result = json_from_response(response)
            except MediaWikiAPIError as error:
                if error.code != "maxlag" or attempt >= self.max_retries:
                    raise
                now = datetime.now(timezone.utc)
                retry_at = parse_retry_after(
                    response.headers.get("Retry-After"), now
                )
                delay = (
                    (retry_at - now).total_seconds()
                    if retry_at is not None
                    else DEFAULT_MAXLAG_WAIT_SECONDS
                )
                self._retry("maxlag", url, max(0.0, delay))
                continue
            edit = result.get("edit", {})
            if edit.get("result") != "Success":
                raise MediaWikiError(
                    "編集が保存されませんでした: {}".format(edit)
                )
            return edit
        raise MediaWikiError("サーバーの混雑が続いたため編集を中止しました")
