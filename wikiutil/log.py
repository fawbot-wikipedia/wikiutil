"""各ツールで共通の、時刻付きの進捗ログ。"""

import sys
from datetime import datetime


def log(message: str) -> None:
    """タイムゾーン付きの時刻を付けて、標準エラー出力へログを書く。

    結果は標準出力、処理の進み具合は標準エラー出力に分けて、結果だけを
    ファイルへ保存したりほかのコマンドへ渡したりできるようにする。
    """
    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    print("[{}] {}".format(timestamp, message), file=sys.stderr, flush=True)
