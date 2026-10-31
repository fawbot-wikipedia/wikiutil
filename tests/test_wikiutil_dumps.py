"""Wikimediaダンプのファイル取得の共通処理を検証する。"""

import contextlib
import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from wikiutil.dumps import (
    dated_file_url,
    download_file,
    fetch_dump_sha1s,
    fetch_latest_dump_date,
    latest_file_url,
    parse_rss_dump_date,
    verify_sha1,
)

RSS = """<?xml version="1.0"?>
<rss version="2.0">
  <channel>
    <title>page.sql.gz</title>
    <link>http://download.wikimedia.org/jawiki/20261001</link>
    <item><pubDate>Thu, 01 Oct 2026 13:09:09 GMT</pubDate></item>
  </channel>
</rss>
"""


class DumpUrlTests(unittest.TestCase):
    """ダンプのURLとファイル名の組み立てを検証する。"""

    def test_builds_latest_and_dated_urls(self):
        """latestと日付付きの実行ディレクトリのURLを作る。"""
        self.assertEqual(
            latest_file_url("page.sql.gz"),
            "https://dumps.wikimedia.org/jawiki/latest/"
            "jawiki-latest-page.sql.gz",
        )
        self.assertEqual(
            dated_file_url("20261001", "redirect.sql.gz"),
            "https://dumps.wikimedia.org/jawiki/20261001/"
            "jawiki-20261001-redirect.sql.gz",
        )


class DumpDateTests(unittest.TestCase):
    """RSSからダンプの実行日を決める処理を検証する。"""

    def test_parses_run_directory_from_rss(self):
        """RSSのリンク先の実行ディレクトリから日付を取り出す。"""
        self.assertEqual(parse_rss_dump_date(RSS), "20261001")

    def test_rejects_rss_without_run_directory(self):
        """実行ディレクトリの日付がないRSSはエラーにする。"""
        with self.assertRaisesRegex(RuntimeError, "実行ディレクトリ"):
            parse_rss_dump_date(
                "<rss><channel><link>http://example.org/</link>"
                "</channel></rss>"
            )

    def test_rejects_broken_rss(self):
        """XMLとして読めないRSSはエラーにする。"""
        with self.assertRaisesRegex(RuntimeError, "RSSを解釈"):
            parse_rss_dump_date("<rss>")

    @patch("wikiutil.dumps.requests.get")
    def test_fetches_rss_of_latest_file(self, get):
        """latestのファイルに対応するRSSを取得して日付を返す。"""
        response = Mock()
        response.text = RSS
        get.return_value = response

        self.assertEqual(
            fetch_latest_dump_date("page.sql.gz", "agent"), "20261001"
        )
        self.assertEqual(
            get.call_args.args[0],
            "https://dumps.wikimedia.org/jawiki/latest/"
            "jawiki-latest-page.sql.gz-rss.xml",
        )
        response.close.assert_called_once_with()


class Sha1Tests(unittest.TestCase):
    """dumpstatus.json のsha1による照合を検証する。"""

    @patch("wikiutil.dumps.requests.get")
    def test_collects_sha1_of_all_jobs(self, get):
        """全ジョブのファイルからsha1を集める。"""
        response = Mock()
        response.json.return_value = {
            "jobs": {
                "tables": {
                    "files": {
                        "jawiki-20261001-page.sql.gz": {"sha1": "aa"},
                        "jawiki-20261001-redirect.sql.gz": {"sha1": "bb"},
                    }
                },
                "pending": {"status": "waiting"},
            }
        }
        get.return_value = response

        self.assertEqual(
            fetch_dump_sha1s("20261001", "agent"),
            {
                "jawiki-20261001-page.sql.gz": "aa",
                "jawiki-20261001-redirect.sql.gz": "bb",
            },
        )

    def test_verifies_file_sha1(self):
        """sha1が一致すれば通し、一致しなければエラーにする。"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dump.gz"
            path.write_bytes(b"abc")
            expected = hashlib.sha1(b"abc").hexdigest()

            verify_sha1(path, expected.upper())
            with self.assertRaisesRegex(RuntimeError, "sha1が一致しません"):
                verify_sha1(path, "0" * 40)


class DownloadTests(unittest.TestCase):
    """ダウンロードの完了判定を検証する。"""

    @patch("wikiutil.dumps.requests.get")
    def test_size_mismatch_keeps_partial_file(self, get):
        """サイズが合わなければpartファイルを残してエラーにする。"""
        response = Mock()
        response.status_code = 200
        response.headers = {"Content-Length": "6"}
        response.iter_content.return_value = iter([b"abc"])
        get.return_value = response
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "dump.gz"

            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, "サイズが一致"):
                    download_file(destination, "https://example.org/x", "a")

            self.assertFalse(destination.exists())
            self.assertEqual(
                destination.with_name("dump.gz.part").read_bytes(), b"abc"
            )


if __name__ == "__main__":
    unittest.main()
