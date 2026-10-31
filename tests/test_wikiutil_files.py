"""一時ファイル経由で安全に書き込む処理を検証する。"""

import tempfile
import unittest
from pathlib import Path

from wikiutil.files import atomic_path, atomic_write, write_json


class AtomicWriteTests(unittest.TestCase):
    """書き込みの完了時だけ置き換わり、失敗時は元のファイルが残ることを検証する。"""

    def setUp(self):
        """一時ディレクトリを用意する。"""
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def test_writes_text_with_lf_and_creates_parent(self):
        """親ディレクトリを作り、改行をLFにして書き込む。"""
        path = self.root / "nested" / "data.jsonl"

        with atomic_write(path) as stream:
            stream.write("一行目\n二行目\n")

        self.assertEqual(path.read_bytes(), "一行目\n二行目\n".encode("utf-8"))
        self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_failure_keeps_original_and_removes_temporary_file(self):
        """書き込み中に失敗すると、元の内容を残して一時ファイルを消す。"""
        path = self.root / "data.json"
        path.write_text("元の内容", encoding="utf-8")

        with self.assertRaises(RuntimeError):
            with atomic_write(path) as stream:
                stream.write("書きかけ")
                raise RuntimeError("中断")

        self.assertEqual(path.read_text(encoding="utf-8"), "元の内容")
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    def test_atomic_path_replaces_after_block(self):
        """一時ファイルのパスに書いた内容で、ブロックの終わりに置き換える。"""
        path = self.root / "cache.bin"

        with atomic_path(path) as temporary_path:
            self.assertEqual(temporary_path.parent, self.root)
            self.assertNotEqual(temporary_path, path)
            temporary_path.write_bytes(b"data")
            self.assertFalse(path.exists())

        self.assertEqual(path.read_bytes(), b"data")

    def test_write_json_uses_indented_utf8(self):
        """日本語をそのまま残した字下げ付きのJSONを書く。"""
        path = self.root / "value.json"

        write_json(path, {"記事": ["あ"]})

        self.assertEqual(
            path.read_text(encoding="utf-8"),
            '{\n  "記事": [\n    "あ"\n  ]\n}\n',
        )


if __name__ == "__main__":
    unittest.main()
