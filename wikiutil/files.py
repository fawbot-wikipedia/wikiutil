"""ファイルを一時ファイル経由で安全に置き換える。

書き込みの途中で止まっても、元のファイルが壊れたり、書きかけの内容が
残ったりしないようにする。
"""

import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any, TextIO


@contextmanager
def atomic_path(path: Path) -> Iterator[Path]:
    """書き込み先と同じ場所の一時ファイルを渡し、完了後に path と置き換える。

    一時ファイルは同時に書き込む処理と重ならない名前で作る。with の中で
    例外が起きた場合は、一時ファイルを消して path を変更しない。

    Args:
        path: 最終的に書き込むファイル。親ディレクトリがなければ作る。

    Yields:
        書き込みに使う一時ファイルのパス。with を抜ける前に閉じておく。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        dir=path.parent, prefix=path.name + ".", suffix=".tmp"
    )
    os.close(descriptor)
    temporary_path = Path(name)
    try:
        yield temporary_path
        temporary_path.replace(path)
    except BaseException:
        with suppress(OSError):
            temporary_path.unlink(missing_ok=True)
        raise


@contextmanager
def atomic_write(path: Path, encoding: str = "utf-8") -> Iterator[TextIO]:
    """テキストを一時ファイルに書き、ディスクへ書き出してから置き換える。

    改行は環境によらず LF にする。
    """
    with atomic_path(path) as temporary_path:
        with temporary_path.open(
            "w", encoding=encoding, newline="\n"
        ) as stream:
            yield stream
            stream.flush()
            os.fsync(stream.fileno())


def write_json(path: Path, value: Any) -> None:
    """値を字下げ付きのJSONとして、一時ファイル経由で保存する。"""
    with atomic_write(path) as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
