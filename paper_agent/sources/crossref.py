"""Crossref REST API（免金鑰）：依 ISSN 逐本期刊抓新註冊 DOI 的文章。

Taylor & Francis、SAGE、Emerald、Wiley、Springer 等出版社都會把新文章的書目資料直接登錄到 Crossref，
所以這是「直接取得出版社最新文章資料」最完整且合規的管道，通常也比 WoS / OpenAlex 更早出現新文章。
文件：https://api.crossref.org/swagger-ui/index.html
"""

from __future__ import annotations

import html
import logging
import re
import time
from datetime import date

import requests

from ..journals import Journal, _mailto
from ..models import Paper

log = logging.getLogger(__name__)
API = "https://api.crossref.org/journals/{issn}/works"
STOPWORDS = {"a", "an", "and", "of", "the", "in", "on", "for", "to", "with"}


def matches(text: str, keywords: list[str]) -> bool:
    """任一關鍵字的所有詞都出現在文字中就算命中（Crossref 本身不支援精準的關鍵字檢索）。"""
    words = set(re.findall(r"[a-z0-9]+", text.lower()))
    for kw in keywords:
        tokens = [t for t in re.findall(r"[a-z0-9]+", kw.lower()) if t not in STOPWORDS]
        if tokens and all(t in words for t in tokens):
            return True
    return False


def _clean_abstract(raw: str | None) -> str:
    if not raw:
        return ""
    text = re.sub(r"<[^>]+>", " ", raw)  # 去掉 JATS XML 標籤
    text = re.sub(r"^\s*(Abstract|ABSTRACT)\s*", "", " ".join(html.unescape(text).split()))
    return text


def _to_paper(item: dict, journal: Journal) -> Paper:
    parts = (item.get("published") or item.get("created") or {}).get("date-parts") or [[None]]
    dp = parts[0]
    published = "-".join(f"{x:02d}" if i else str(x) for i, x in enumerate(dp) if x) or None
    return Paper(
        title=" ".join(((item.get("title") or [""])[0]).split()),
        abstract=_clean_abstract(item.get("abstract")),
        authors=[f"{a.get('given', '')} {a.get('family', '')}".strip() or a.get("name", "")
                 for a in item.get("author") or []],
        year=dp[0] if dp and dp[0] else None,
        source="crossref",
        url=item.get("URL") or f"https://doi.org/{item.get('DOI')}",
        doi=item.get("DOI"),
        published=published,
        venue=(item.get("container-title") or [journal.name])[0],
        extra={"subjects": item.get("subject") or []},
    )


def search(keywords: list[str], since: date, limit: int, cfg: dict,
           journals: list[Journal] | None = None) -> list[Paper]:
    if not journals:
        return []  # Crossref 只用在期刊清單模式
    papers = []
    for j in journals:
        if not j.issns:
            continue
        params = {
            "filter": f"from-created-date:{since.isoformat()},type:journal-article",
            "sort": "created",
            "order": "desc",
            "rows": min(limit, 100),
            **_mailto(),
        }
        try:
            resp = requests.get(API.format(issn=j.issns[0]), params=params, timeout=60)
            resp.raise_for_status()
        except requests.RequestException as e:
            log.warning("[crossref] %s 查詢失敗：%s", j.name, e)
            continue
        for item in resp.json().get("message", {}).get("items", []):
            p = _to_paper(item, j)
            if p.title and matches(f"{p.title} {p.abstract}", keywords):
                papers.append(p)
        time.sleep(0.2)
    return papers
