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
RETRY_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
# MediaWikiがページ名から取り除く方向制御文字（LRM・RLM・LRE〜RLO）。
_DIRECTION_MARKS = dict.fromkeys([0x200E, 0x200F, *range(0x202A, 0x202F)])


class MediaWikiError(RuntimeError):
    """MediaWiki API操作の失敗を表す。"""


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
        MediaWikiError: JSONがオブジェクトでないか、APIエラーを含む場合。
    """
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise MediaWikiError("MediaWiki APIの応答の形式が不正です")
    if "error" in data:
        raise MediaWikiError(
            "MediaWiki APIがエラーを返しました: {}".format(data["error"])
        )
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

        Raises:
            requests.ConnectionError: 再試行後も接続できない場合。
            requests.Timeout: 再試行後もタイムアウトした場合。
            requests.exceptions.ChunkedEncodingError: 再試行後も応答が
                途中で切れる場合。
        """
        kwargs.setdefault("timeout", self.timeout)
        method_name = (
            method.decode() if isinstance(method, bytes) else method
        ).upper()
        url_text = url.decode() if isinstance(url, bytes) else url
        host = urlparse(url_text).hostname or ""
        retryable = method_name in self.retry_methods
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
