"""MediaWiki API共通処理のテスト。"""

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import requests

from wikiutil.mediawiki import (
    MediaWikiAPIError,
    MediaWikiError,
    MediaWikiSession,
    api_url,
    json_from_response,
    parse_retry_after,
    strip_direction_marks,
    title_key,
)


def _response(status_code=200, data=None, headers=None):
    """テスト用のHTTP応答を作る。"""
    response = Mock()
    response.status_code = status_code
    response.headers = headers or {}
    response.json.return_value = {} if data is None else data
    return response


class HelperTests(unittest.TestCase):
    """URL・ページ名・Retry-Afterの補助関数を検証する。"""

    def test_api_url_uses_language_subdomain(self):
        """言語コードからWikipediaのAPIのURLを作る。"""
        self.assertEqual(api_url("en"), "https://en.wikipedia.org/w/api.php")

    def test_title_key_normalizes_spaces_and_case(self):
        """アンダースコアと大文字小文字の違いを無視する。"""
        self.assertEqual(title_key(" Foo_Bar "), title_key("foo bar"))

    def test_strip_direction_marks(self):
        """ページ名から LRM・RLM・LRE〜RLO を取り除き、ほかの文字は残す。"""
        self.assertEqual(strip_direction_marks(
            "‪記事名‎‏‮"
        ), "記事名")
        self.assertEqual(strip_direction_marks("A​B"), "A​B")

    def test_parses_delta_seconds(self):
        """秒数指定を基準時刻からの時刻に変換する。"""
        now = datetime(2026, 10, 21, 0, 0, tzinfo=timezone.utc)

        self.assertEqual(
            parse_retry_after("120", now), now + timedelta(seconds=120)
        )

    def test_parses_http_date(self):
        """HTTP日時をUTCの時刻に変換する。"""
        now = datetime(2026, 10, 21, 0, 0, tzinfo=timezone.utc)

        self.assertEqual(
            parse_retry_after("Wed, 21 Oct 2026 00:02:00 GMT", now),
            datetime(2026, 10, 21, 0, 2, tzinfo=timezone.utc),
        )

    def test_returns_none_for_missing_or_invalid_header(self):
        """値がないか解析できない場合はNoneを返す。"""
        now = datetime(2026, 10, 21, 0, 0, tzinfo=timezone.utc)

        self.assertIsNone(parse_retry_after(None, now))
        self.assertIsNone(parse_retry_after("invalid", now))

    def test_json_from_response_raises_api_error(self):
        """APIエラーを含む応答はMediaWikiErrorにする。"""
        response = _response(data={"error": {"code": "badtoken"}})

        with self.assertRaises(MediaWikiError):
            json_from_response(response)

    def test_api_error_keeps_error_code(self):
        """APIエラーのコードを `code` に持つ。"""
        response = _response(data={"error": {"code": "editconflict"}})

        with self.assertRaises(MediaWikiAPIError) as raised:
            json_from_response(response)
        self.assertEqual(raised.exception.code, "editconflict")


@patch("wikiutil.mediawiki.requests.Session.request")
class SessionTests(unittest.TestCase):
    """要求間隔と再試行の動作を検証する。"""

    def setUp(self):
        """待機を記録するSessionを用意する。"""
        self.sleeps = []
        self.session = MediaWikiSession(
            default_interval=0, sleep=self.sleeps.append
        )

    def test_retries_connection_error_with_backoff(self, request):
        """接続例外の後に指数バックオフで再試行する。"""
        request.side_effect = [
            requests.exceptions.ConnectionError("DNS failure"),
            _response(data={"query": {}}),
        ]

        result = self.session.get_json(api_url("en"), {})

        self.assertEqual(result, {"query": {}})
        self.assertEqual(request.call_count, 2)
        self.assertEqual(self.sleeps, [1])

    def test_retries_response_ended_prematurely(self, request):
        """応答の本文が途中で切れた場合も再試行する。"""
        request.side_effect = [
            requests.exceptions.ChunkedEncodingError("ended prematurely"),
            _response(data={"query": {}}),
        ]

        result = self.session.get_json(api_url("en"), {})

        self.assertEqual(result, {"query": {}})
        self.assertEqual(request.call_count, 2)

    def test_raises_after_retry_limit(self, request):
        """再試行の上限を超えたら元の例外を送出する。"""
        request.side_effect = requests.exceptions.Timeout("slow")
        session = MediaWikiSession(
            default_interval=0, max_retries=2, sleep=self.sleeps.append
        )

        with self.assertRaises(requests.exceptions.Timeout):
            session.get(api_url("en"))
        self.assertEqual(request.call_count, 3)

    def test_waits_for_retry_after_on_rate_limit(self, request):
        """429応答ではRetry-Afterの秒数だけ待って再試行する。"""
        request.side_effect = [
            _response(429, headers={"Retry-After": "7"}),
            _response(data={"query": {}}),
        ]

        self.session.get(api_url("ja"))

        self.assertEqual(request.call_count, 2)
        self.assertEqual(len(self.sleeps), 1)
        self.assertAlmostEqual(self.sleeps[0], 7, delta=1)

    def test_does_not_retry_post_by_default(self, request):
        """副作用のあるPOSTは既定では再試行しない。"""
        request.return_value = _response(503)

        response = self.session.post(api_url("ja"), data={})

        self.assertEqual(response.status_code, 503)
        self.assertEqual(request.call_count, 1)

    def test_sets_default_timeout_and_user_agent(self, request):
        """既定のタイムアウトとUser-Agentを付けて要求する。"""
        request.return_value = _response()

        self.session.get(api_url("ja"))

        self.assertEqual(request.call_args.kwargs["timeout"], 30)
        self.assertIn("wikipedia-util", self.session.headers["User-Agent"])

    def test_waits_between_requests_to_same_host(self, request):
        """同じホストへの連続要求の間に最小間隔を空ける。"""
        request.return_value = _response()
        session = MediaWikiSession(
            min_intervals={"ja.wikipedia.org": 5},
            sleep=self.sleeps.append,
        )

        session.get(api_url("ja"))
        session.get(api_url("en"))
        session.get(api_url("ja"))

        self.assertEqual(len(self.sleeps), 1)
        self.assertGreater(self.sleeps[0], 4)

    def test_does_not_retry_when_retry_is_false(self, request):
        """`retry=False` の要求は、再試行してよいメソッドでも再試行しない。"""
        request.return_value = _response(503)

        response = self.session.get(api_url("ja"), retry=False)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(request.call_count, 1)
        self.assertNotIn("retry", request.call_args.kwargs)


_EDIT_ARGUMENTS = {
    "title": "記事名",
    "text": "本文",
    "summary": "要約",
    "token": "csrf+\\",
    "base_revision_id": 100,
    "base_timestamp": "2026-10-31T00:00:00Z",
    "start_timestamp": "2026-10-31T01:00:00Z",
}


@patch("wikiutil.mediawiki.requests.Session.request")
class AccountTests(unittest.TestCase):
    """ログイン・トークン・利用者情報・編集を検証する。"""

    def setUp(self):
        """待機を記録し、POSTも再試行するSessionを用意する。"""
        self.sleeps = []
        self.session = MediaWikiSession(
            default_interval=0,
            retry_methods=("GET", "POST"),
            sleep=self.sleeps.append,
        )

    def test_login_sends_token_and_credentials(self, request):
        """ログイン用トークンを取得し、ログイン名・パスワードと送る。"""
        request.side_effect = [
            _response(data={"query": {"tokens": {"logintoken": "t+\\"}}}),
            _response(data={"login": {"result": "Success"}}),
        ]

        self.session.login(api_url("ja"), "Bot@task", "secret")

        sent = request.call_args.kwargs["data"]
        self.assertEqual(sent["lgname"], "Bot@task")
        self.assertEqual(sent["lgpassword"], "secret")
        self.assertEqual(sent["lgtoken"], "t+\\")

    def test_login_failure_hides_password(self, request):
        """ログインの失敗は理由を示し、パスワードを含めない。"""
        request.side_effect = [
            _response(data={"query": {"tokens": {"logintoken": "t"}}}),
            _response(
                data={"login": {"result": "Failed", "reason": "Incorrect"}}
            ),
        ]

        with self.assertRaises(MediaWikiError) as raised:
            self.session.login(api_url("ja"), "Bot@task", "secret")
        self.assertIn("Failed", str(raised.exception))
        self.assertNotIn("secret", str(raised.exception))

    def test_csrf_token(self, request):
        """編集用のトークンを返す。"""
        request.return_value = _response(
            data={"query": {"tokens": {"csrftoken": "c+\\"}}}
        )

        self.assertEqual(self.session.csrf_token(api_url("ja")), "c+\\")

    def test_user_info(self, request):
        """利用者の名前・グループ・新着メッセージの有無を返す。"""
        info = {"name": "Bot", "groups": ["user", "bot"], "messages": True}
        request.return_value = _response(
            data={"query": {"userinfo": info}}
        )

        self.assertEqual(self.session.user_info(api_url("ja")), info)
        sent = request.call_args.kwargs["params"]
        self.assertEqual(sent["uiprop"], "groups|hasmsg")

    def test_edit_sends_conflict_checks(self, request):
        """競合を検出する引数を付け、ページを作らない編集として送る。"""
        request.return_value = _response(
            data={"edit": {"result": "Success", "newrevid": 101}}
        )

        result = self.session.edit(
            api_url("ja"), bot=False, **_EDIT_ARGUMENTS
        )

        self.assertEqual(result["newrevid"], 101)
        sent = request.call_args.kwargs["data"]
        self.assertEqual(sent["baserevid"], 100)
        self.assertEqual(sent["basetimestamp"], "2026-10-31T00:00:00Z")
        self.assertEqual(sent["starttimestamp"], "2026-10-31T01:00:00Z")
        self.assertEqual(sent["nocreate"], 1)
        self.assertEqual(sent["assert"], "user")
        self.assertEqual(sent["maxlag"], 5)
        self.assertNotIn("bot", sent)

    def test_edit_as_bot(self, request):
        """ボットの編集では `bot` と `assert=bot` を付ける。"""
        request.return_value = _response(
            data={"edit": {"result": "Success", "newrevid": 101}}
        )

        self.session.edit(
            api_url("ja"), bot=True, assertion="bot", **_EDIT_ARGUMENTS
        )

        sent = request.call_args.kwargs["data"]
        self.assertEqual(sent["bot"], 1)
        self.assertEqual(sent["assert"], "bot")

    def test_edit_waits_and_resends_on_maxlag(self, request):
        """maxlag で断られたら、Retry-After だけ待って送り直す。"""
        request.side_effect = [
            _response(
                data={"error": {"code": "maxlag"}},
                headers={"Retry-After": "3"},
            ),
            _response(data={"edit": {"result": "Success", "newrevid": 101}}),
        ]

        self.session.edit(api_url("ja"), bot=False, **_EDIT_ARGUMENTS)

        self.assertEqual(request.call_count, 2)
        self.assertEqual(len(self.sleeps), 1)
        self.assertAlmostEqual(self.sleeps[0], 3, delta=1)

    def test_edit_does_not_resend_on_server_error(self, request):
        """HTTPの失敗では、POSTを再試行するSessionでも送り直さない。"""
        response = _response(503)
        response.raise_for_status.side_effect = requests.HTTPError("503")
        request.return_value = response

        with self.assertRaises(requests.HTTPError):
            self.session.edit(api_url("ja"), bot=False, **_EDIT_ARGUMENTS)
        self.assertEqual(request.call_count, 1)

    def test_edit_raises_on_conflict(self, request):
        """編集の競合は、コード付きのAPIエラーにする。"""
        request.return_value = _response(
            data={"error": {"code": "editconflict"}}
        )

        with self.assertRaises(MediaWikiAPIError) as raised:
            self.session.edit(api_url("ja"), bot=False, **_EDIT_ARGUMENTS)
        self.assertEqual(raised.exception.code, "editconflict")
        self.assertEqual(request.call_count, 1)

    def test_edit_raises_when_not_saved(self, request):
        """結果が Success でなければ、保存されなかったとして失敗にする。"""
        request.return_value = _response(
            data={"edit": {"result": "Failure", "captcha": {}}}
        )

        with self.assertRaises(MediaWikiError):
            self.session.edit(api_url("ja"), bot=False, **_EDIT_ARGUMENTS)


if __name__ == "__main__":
    unittest.main()
