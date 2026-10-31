# wikiutil

ウィキペディア関連のツールで共有する処理です。MediaWiki APIへの要求、Wikimediaダンプの取得、ファイルの安全な保存、進捗ログ、仮リンクの解析をまとめています。

## モジュール

| モジュール | 内容 |
|---|---|
| `wikiutil.mediawiki` | MediaWiki APIへの要求。User-Agentの付与、ホストごとの要求間隔、429・5xx・接続障害の再試行（`Retry-After` に従う）、ページ名の正規化。BotPasswordでのログイン、利用者の情報の取得、編集 |
| `wikiutil.dumps` | Wikimediaダンプの取得。ダンプ日の決定（`Last-Modified` またはRSS）、途中から再開できるダウンロード、`dumpstatus.json` のsha1による照合 |
| `wikiutil.files` | 一時ファイル経由の安全な保存。書き込みの途中で止まっても、元のファイルが壊れず、書きかけの内容も残らない |
| `wikiutil.log` | 時刻付きの進捗ログ（標準エラー出力） |
| `wikiutil.ill` | 仮リンクテンプレートの引数の解析、日本語版の記事情報の取得、Wikidataとの照合 |

## 使い方

Python 3.15 を使います。[uv](https://docs.astral.sh/uv/) で管理しているプロジェクトからは、タグを指定して git 依存にします。

```toml
[project]
dependencies = [
    "wikiutil",
]

[tool.uv.sources]
wikiutil = { git = "https://github.com/fawbot-wikipedia/wikiutil", tag = "v0.2.0" }
```

`MediaWikiSession` は、使う側のツール名と連絡先を `user_agent=` で渡してください（[Wikimedia の User-Agent ポリシー](https://foundation.wikimedia.org/wiki/Policy:Wikimedia_Foundation_User-Agent_Policy)）。

```python
from wikiutil.mediawiki import MediaWikiSession, api_url

session = MediaWikiSession(user_agent="mytool/1.0 (https://example.org/mytool)")
data = session.get_json(api_url("ja"), params={"action": "query", "meta": "siteinfo", "format": "json"})
```

編集は、ログインしてから `edit` で送ります。編集の競合を検出するため、本文を取得した版のIDと時刻、取得を始めた時刻を渡します。ページがなければ作りません。HTTPの失敗では送り直しません（編集が反映されたか分からないため）。`maxlag` で断られた場合だけ、待って送り直します。APIエラーは `MediaWikiAPIError` になり、エラーコードを `code`（例：`editconflict`）に持ちます。

```python
session.login(api_url("ja"), "利用者名@BotPassword名", password)
groups = session.user_info(api_url("ja"))["groups"]
session.edit(
    api_url("ja"),
    title="記事名",
    text=new_text,
    summary="要約",
    token=session.csrf_token(api_url("ja")),
    base_revision_id=revid,
    base_timestamp=revision_timestamp,
    start_timestamp=fetched_at,
    bot="bot" in groups,
    assertion="bot" if "bot" in groups else "user",
)
```

## 開発

```powershell
uv sync
uv run python -m unittest discover -s tests
```

変更したら、タグ（`vX.Y.Z`）を付けて push し、使う側の `tag` を上げて `uv lock` します。
