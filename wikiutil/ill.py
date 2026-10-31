"""仮リンクの日本語版記事情報とWikidata照合を共通化する。"""

import re
import time
from collections.abc import Callable, Iterable

from wikiutil.mediawiki import (
    WIKIDATA_API_URL,
    MediaWikiSession,
    api_url,
)

PAGE_INFO_BATCH_SIZE = 50


def parse_ill_template(template: str) -> dict[str, str]:
    """仮リンクテンプレートの引数名と値を対応付ける。"""
    split_res = template[2:-2].split("|")
    attr_names = ["template_name", "ja_name", "lang", "base_name", "redirect"]
    attributes = {"template_name": split_res[0]}
    positional_attributes: dict[int, str] = {}
    next_position = 1
    for arg in split_res[1:]:
        if not arg:
            continue
        arg_split = arg.split("=", 1)
        if len(arg_split) == 1:
            while next_position in positional_attributes:
                next_position += 1
            if next_position < len(attr_names):
                positional_attributes[next_position] = arg
                next_position += 1
            continue

        arg_name, arg_value = arg_split
        if arg_name.isdigit():
            position = int(arg_name)
            if 0 < position < len(attr_names):
                positional_attributes[position] = arg_value
                continue
        attributes[arg_name] = arg_value

    for position, arg_value in positional_attributes.items():
        attributes[attr_names[position]] = arg_value
    return attributes


def fetch_ja_page_info(
    titles: Iterable[str],
    session: MediaWikiSession,
    on_progress: Callable[[str], None] | None = None,
) -> dict[str, tuple[bool, bool]]:
    """日本語版記事の作成状態と転送状態を一括取得する。"""
    unique_titles = list(dict.fromkeys(titles))
    page_info_by_title: dict[str, tuple[bool, bool]] = {}
    for start in range(0, len(unique_titles), PAGE_INFO_BATCH_SIZE):
        batch = unique_titles[start:start + PAGE_INFO_BATCH_SIZE]
        if on_progress is not None:
            on_progress(
                "ja Wikipedia ページ情報取得 開始: "
                f"{start + 1}-{start + len(batch)}/{len(unique_titles)}"
            )
        started = time.monotonic()
        data = session.post_json(
            api_url("ja"),
            {
                "action": "query",
                "prop": "info",
                "titles": "|".join(batch),
                "format": "json",
                "formatversion": 2,
            },
        )
        if on_progress is not None:
            on_progress(
                "ja Wikipedia ページ情報取得 完了: "
                f"{time.monotonic() - started:.2f}秒"
            )

        query = data.get("query", {})
        pages = query.get("pages", [])
        if not pages:
            raise RuntimeError(
                "Wikipedia APIの応答にページ情報がありません: "
                + ", ".join(batch)
            )

        transforms = query.get("normalized", []) + query.get("converted", [])
        requested_titles: dict[str, list[str]] = {}
        for title in batch:
            api_title = title
            for transform in transforms:
                if api_title == transform["from"]:
                    api_title = transform["to"]
            requested_titles.setdefault(api_title, []).append(title)

        for page in pages:
            page_title = page.get("title")
            if not page_title:
                raise RuntimeError(
                    "Wikipedia APIの応答にページ名がありません: "
                    + ", ".join(batch)
                )
            matching_titles = requested_titles.get(page_title, [page_title])
            if "missing" in page:
                page_info = (False, False)
            elif "length" not in page:
                raise RuntimeError(
                    "Wikipedia APIの応答にページの長さがありません: "
                    + matching_titles[0]
                )
            elif page["length"] == 0:
                page_info = (False, False)
            elif "redirect" in page:
                page_info = (False, True)
            else:
                page_info = (True, False)
            for requested_title in matching_titles:
                page_info_by_title[requested_title] = page_info

        unresolved_titles = [
            title for title in batch if title not in page_info_by_title
        ]
        if unresolved_titles:
            raise RuntimeError(
                "Wikipedia APIの応答にページ情報がありません: "
                + ", ".join(unresolved_titles)
            )
    return page_info_by_title


def get_wikidata_ja_titles(
    foreign_articles: Iterable[tuple[str, str]],
    session: MediaWikiSession,
    on_progress: Callable[[str], None] | None = None,
) -> tuple[
    dict[tuple[str, str], str],
    dict[tuple[str, str], bool],
    dict[tuple[str, str], bool],
]:
    """他言語版記事の日本語版記事名、曖昧さ回避、転送状態を取得する。"""
    articles_by_language: dict[str, list[str]] = {}
    for language, title in foreign_articles:
        if not re.fullmatch(r"[a-z][a-z0-9-]*", language):
            raise ValueError(f"Wikipediaの言語コードが不正です: {language}")
        articles_by_language.setdefault(language, [])
        if title not in articles_by_language[language]:
            articles_by_language[language].append(title)

    article_item_ids: dict[tuple[str, str], str] = {}
    article_disambiguation: dict[tuple[str, str], bool] = {}
    article_redirect: dict[tuple[str, str], bool] = {}
    for title in articles_by_language.pop("wikidata", []):
        if re.fullmatch(r"Q[1-9][0-9]*", title):
            article_item_ids[("wikidata", title)] = title

    for language, titles in articles_by_language.items():
        for start in range(0, len(titles), PAGE_INFO_BATCH_SIZE):
            batch = titles[start:start + PAGE_INFO_BATCH_SIZE]
            if on_progress is not None:
                on_progress(
                    f"{language} Wikipedia 項目ID取得: "
                    f"{start + 1}-{start + len(batch)}/{len(titles)}"
                )
            data = session.get_json(
                api_url(language),
                {
                    "action": "query",
                    "prop": "pageprops",
                    "titles": "|".join(batch),
                    "redirects": 1,
                    "format": "json",
                    "formatversion": 2,
                },
            )

            query = data.get("query", {})
            pages = query.get("pages", [])
            if not pages:
                raise RuntimeError(
                    f"{language}版Wikipedia APIの応答にページ情報がありません: "
                    f"{', '.join(batch)}"
                )
            transforms = [
                *query.get("normalized", []),
                *query.get("converted", []),
                *query.get("redirects", []),
            ]
            redirect_sources = {
                transform["from"] for transform in query.get("redirects", [])
            }
            requested_titles: dict[str, list[tuple[str, bool]]] = {}
            for title in batch:
                api_title = title
                is_redirect = False
                for _ in range(len(transforms)):
                    transform = next(
                        (
                            item for item in transforms
                            if api_title == item["from"]
                        ),
                        None,
                    )
                    if transform is None:
                        break
                    if api_title in redirect_sources:
                        is_redirect = True
                    api_title = transform["to"]
                requested_titles.setdefault(api_title, []).append(
                    (title, is_redirect)
                )

            for page in pages:
                page_title = page.get("title")
                if not page_title:
                    raise RuntimeError(
                        f"{language}版Wikipedia APIの応答に"
                        "記事名のないページがあります"
                    )
                matching_titles = requested_titles.get(
                    page_title, [(page_title, False)]
                )
                pageprops = page.get("pageprops", {})
                is_disambiguation = "disambiguation" in pageprops
                for requested_title, is_redirect in matching_titles:
                    key = (language, requested_title)
                    article_disambiguation[key] = is_disambiguation
                    article_redirect[key] = is_redirect
                item_id = pageprops.get("wikibase_item")
                if item_id:
                    for requested_title, _ in matching_titles:
                        article_item_ids[(language, requested_title)] = item_id

    item_ids = list(dict.fromkeys(article_item_ids.values()))
    item_ja_titles: dict[str, str] = {}
    for start in range(0, len(item_ids), PAGE_INFO_BATCH_SIZE):
        batch = item_ids[start:start + PAGE_INFO_BATCH_SIZE]
        if on_progress is not None:
            on_progress(
                "Wikidata sitelink取得: "
                f"{start + 1}-{start + len(batch)}/{len(item_ids)}"
            )
        data = session.get_json(
            WIKIDATA_API_URL,
            {
                "action": "wbgetentities",
                "ids": "|".join(batch),
                "props": "sitelinks",
                "sitefilter": "jawiki",
                "format": "json",
            },
        )
        entities = data.get("entities")
        if not isinstance(entities, dict):
            raise RuntimeError("Wikidata APIの応答に項目がありません")
        for item_id in batch:
            entity = entities.get(item_id)
            if entity is None:
                raise RuntimeError(
                    f"Wikidata APIの応答に項目がありません: {item_id}"
                )
            ja_title = entity.get("sitelinks", {}).get(
                "jawiki", {}
            ).get("title")
            if ja_title:
                item_ja_titles[item_id] = ja_title

    if on_progress is not None:
        on_progress("Wikidata照合 完了")
    return (
        {
            article: item_ja_titles[item_id]
            for article, item_id in article_item_ids.items()
            if item_id in item_ja_titles
        },
        article_disambiguation,
        article_redirect,
    )
