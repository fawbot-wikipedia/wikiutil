"""Wikimediaダンプのファイルの取得を、各ツールで共通化する。

ダンプ日の決定（`Last-Modified` またはRSS）、途中からの再開に対応した
ダウンロード、`dumpstatus.json` のsha1による照合を扱う。ダンプの中身の
読み込みは、ファイルの種類ごとに各ツールで行う。
"""

import email.utils
import hashlib
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import requests
from tqdm import tqdm

DUMP_BASE_URL = "https://dumps.wikimedia.org/jawiki"
DOWNLOAD_CHUNK_SIZE = 1024 * 1024
HEAD_TIMEOUT = (30, 60)
DOWNLOAD_TIMEOUT = (30, 120)
HASH_CHUNK_SIZE = 1024 * 1024


def latest_file_url(file_suffix: str) -> str:
    """`latest` ディレクトリにあるダンプファイルのURLを返す。

    Args:
        file_suffix: `jawiki-latest-` に続くファイル名（例: `page.sql.gz`）。
    """
    return "{}/latest/jawiki-latest-{}".format(DUMP_BASE_URL, file_suffix)


def dated_file_url(dump_date: str, file_suffix: str) -> str:
    """日付付きの実行ディレクトリにあるダンプファイルのURLを返す。

    Args:
        dump_date: ダンプの実行日（`YYYYMMDD`）。
        file_suffix: `jawiki-<日付>-` に続くファイル名。
    """
    return "{}/{}/jawiki-{}-{}".format(
        DUMP_BASE_URL, dump_date, dump_date, file_suffix
    )


def dated_file_name(dump_date: str, file_suffix: str) -> str:
    """日付付きのダンプファイル名を返す。"""
    return "jawiki-{}-{}".format(dump_date, file_suffix)


def fetch_last_modified_date(url: str, user_agent: str) -> str:
    """ファイルの `Last-Modified` から日付（`YYYYMMDD`）を返す。

    Raises:
        RuntimeError: 応答を取得できない、またはヘッダーがない・不正な場合。
    """
    try:
        response = requests.head(
            url,
            headers={"User-Agent": user_agent},
            allow_redirects=True,
            timeout=HEAD_TIMEOUT,
        )
    except requests.RequestException as error:
        raise RuntimeError(
            "最新ダンプの更新日時を取得できません: {}".format(error)
        ) from error
    try:
        response.raise_for_status()
        last_modified = response.headers.get("Last-Modified")
    finally:
        response.close()
    if not last_modified:
        raise RuntimeError(
            "最新ダンプの応答にLast-Modifiedヘッダーがありません"
        )
    try:
        modified_at = email.utils.parsedate_to_datetime(last_modified)
    except (TypeError, ValueError) as error:
        raise RuntimeError(
            "最新ダンプの更新日時を解釈できません: {}".format(last_modified)
        ) from error
    return modified_at.strftime("%Y%m%d")


def parse_rss_dump_date(rss_text: str) -> str:
    """`latest` のRSSから、ファイルを作った実行ディレクトリの日付を返す。

    RSSの `channel/link` は `.../jawiki/20261001` の形で実行ディレクトリを
    指す。ファイルの `Last-Modified` は実行の開始日より後になりうるため、
    ダンプの時点を知るにはこちらを使う。

    Raises:
        RuntimeError: RSSを解釈できない、または日付が見つからない場合。
    """
    try:
        root = ET.fromstring(rss_text)
    except ET.ParseError as error:
        raise RuntimeError(
            "ダンプのRSSを解釈できません: {}".format(error)
        ) from error
    link = root.findtext("channel/link") or ""
    match = re.search(r"/jawiki/(\d{8})/?$", link.strip())
    if match is None:
        raise RuntimeError(
            "ダンプのRSSに実行ディレクトリの日付がありません: {}".format(link)
        )
    return match.group(1)


def fetch_latest_dump_date(file_suffix: str, user_agent: str) -> str:
    """`latest` のファイルを作った実行の日付（`YYYYMMDD`）をRSSから返す。

    Raises:
        RuntimeError: RSSを取得・解釈できない場合。
    """
    url = latest_file_url(file_suffix) + "-rss.xml"
    try:
        response = requests.get(
            url, headers={"User-Agent": user_agent}, timeout=HEAD_TIMEOUT
        )
    except requests.RequestException as error:
        raise RuntimeError(
            "ダンプのRSSを取得できません: {}".format(error)
        ) from error
    try:
        response.raise_for_status()
        text = response.text
    except requests.RequestException as error:
        raise RuntimeError(
            "ダンプのRSSを取得できません: {}".format(error)
        ) from error
    finally:
        response.close()
    return parse_rss_dump_date(text)


def fetch_dump_sha1s(dump_date: str, user_agent: str) -> dict[str, str]:
    """実行ディレクトリの `dumpstatus.json` から、ファイル名ごとのsha1を返す。

    Raises:
        RuntimeError: 取得できない、または形式が不正な場合。
    """
    url = "{}/{}/dumpstatus.json".format(DUMP_BASE_URL, dump_date)
    try:
        response = requests.get(
            url, headers={"User-Agent": user_agent}, timeout=HEAD_TIMEOUT
        )
        try:
            response.raise_for_status()
            data = response.json()
        finally:
            response.close()
    except (requests.RequestException, ValueError) as error:
        raise RuntimeError(
            "dumpstatus.json を取得できません: {}".format(error)
        ) from error
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(jobs, dict):
        raise RuntimeError("dumpstatus.json に jobs がありません: " + url)
    sha1s = {}
    for job in jobs.values():
        files = job.get("files") if isinstance(job, dict) else None
        if not isinstance(files, dict):
            continue
        for name, info in files.items():
            if isinstance(info, dict) and isinstance(info.get("sha1"), str):
                sha1s[name] = info["sha1"]
    return sha1s


def file_sha1(path: Path) -> str:
    """ファイルのsha1を16進数の文字列で返す。"""
    digest = hashlib.sha1()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(HASH_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_sha1(path: Path, expected: str) -> None:
    """ファイルのsha1が期待値と一致することを確かめる。

    Raises:
        RuntimeError: 一致しない場合。
    """
    actual = file_sha1(path)
    if actual != expected.lower():
        raise RuntimeError(
            "ダンプのsha1が一致しません（期待: {}、取得: {}）: {}".format(
                expected, actual, path
            )
        )


def download_file(
    destination: Path,
    source_url: str,
    user_agent: str,
    description: str = "Wikipediaダンプ",
) -> Path:
    """ダンプファイルをストリーミング取得し、保存先へ配置する。

    取得中は `<保存先>.part` に書き、通信が途切れた場合は次回の呼び出しで
    HTTP Range により続きから取得する。サイズを照合してから保存先へ移す。

    Args:
        destination: 保存先のファイル。親ディレクトリがなければ作る。
        source_url: 取得元のURL。
        user_agent: 要求に付けるUser-Agent。
        description: 進捗表示に使う名前。

    Returns:
        保存先のパス。

    Raises:
        RuntimeError: 通信エラー、予期しない応答、サイズ不一致の場合。
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial_path = destination.with_name(destination.name + ".part")
    offset = partial_path.stat().st_size if partial_path.exists() else 0
    headers = {
        "User-Agent": user_agent,
        "Accept-Encoding": "identity",
    }
    if offset:
        headers["Range"] = "bytes={}-".format(offset)
    try:
        response = requests.get(
            source_url,
            headers=headers,
            stream=True,
            timeout=DOWNLOAD_TIMEOUT,
        )
    except requests.RequestException as error:
        raise RuntimeError(
            "Wikipediaダンプのダウンロード要求に失敗しました: {}".format(
                error
            )
        ) from error

    try:
        response.raise_for_status()
        content_range = response.headers.get("Content-Range", "")
        append_mode = False
        total_size = None
        if response.status_code == 206:
            range_match = re.fullmatch(
                r"bytes (\d+)-(\d+)/(\d+)", content_range
            )
            if range_match is None:
                raise RuntimeError(
                    "ダウンロード応答のContent-Rangeが不正です: {}".format(
                        content_range
                    )
                )
            range_start, _, total_value = map(int, range_match.groups())
            if range_start != offset:
                raise RuntimeError(
                    "再開位置が一致しません（要求: {}, 応答: {}）".format(
                        offset, range_start
                    )
                )
            append_mode = True
            total_size = total_value
        elif response.status_code == 200:
            offset = 0
            length = response.headers.get("Content-Length")
            total_size = int(length) if length and length.isdigit() else None
        else:
            raise RuntimeError(
                "ダンプサーバーから予期しないHTTPステータスが返りました: "
                "{}".format(response.status_code)
            )

        with partial_path.open("ab" if append_mode else "wb") as stream:
            with tqdm(
                total=total_size,
                initial=offset,
                unit="B",
                unit_scale=True,
                desc=description,
            ) as progress:
                for chunk in response.iter_content(
                    chunk_size=DOWNLOAD_CHUNK_SIZE
                ):
                    if chunk:
                        stream.write(chunk)
                        progress.update(len(chunk))
            stream.flush()
            os.fsync(stream.fileno())
    except requests.RequestException as error:
        raise RuntimeError(
            "Wikipediaダンプのダウンロード中に通信エラーが発生しました。"
            "次回実行時に途中から再開します: {}".format(error)
        ) from error
    finally:
        response.close()

    downloaded_size = partial_path.stat().st_size
    if total_size is not None and downloaded_size != total_size:
        raise RuntimeError(
            "ダウンロードサイズが一致しません（期待: {} bytes、"
            "取得: {} bytes）。次回実行時に途中から再開します。".format(
                total_size, downloaded_size
            )
        )
    partial_path.replace(destination)
    return destination
