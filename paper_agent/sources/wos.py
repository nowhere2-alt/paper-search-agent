"""Web of Science Starter API（Clarivate 官方 API）。

需要 API 金鑰：到 https://developer.clarivate.com 註冊並訂閱「Web of Science Starter API」，
取得金鑰後設定環境變數 WOS_API_KEY。
Starter API 不提供摘要，摘要與開放取用 PDF 由 enrich.py 依 DOI 從 OpenAlex / Semantic Scholar 補齊。
"""

from __future__ import annotations

import logging
import math
import os
import time
from datetime import date

import requests

from ..models import Paper

log = logging.getLogger(__name__)
API = "https://api.clarivate.com/apis/wos-starter/v1/documents"
PAGE_SIZE = 50  # API 單頁上限


def build_query(keywords: list[str], since: date, topic: dict) -> str:
    """預設：TS=（主題欄位：標題、摘要、關鍵字）任一關鍵字命中，並限制出版年。
    進階：在 config 用 wos_query 寫完整的 WoS 進階檢索式（可用 SO=期刊、WC=學科類別等欄位）。"""
    if custom := topic.get("wos_query"):
        return custom
    terms = " OR ".join(f'"{kw}"' if " " in kw else kw for kw in keywords)
    query = f"TS=({terms}) AND PY=({since.year}-{date.today().year})"
    if extra := topic.get("wos_filter"):
        query += f" AND {extra}"
    return query


def _to_paper(hit: dict) -> Paper:
    src = hit.get("source") or {}
    ids = hit.get("identifiers") or {}
    links = hit.get("links") or {}
    year = src.get("publishYear")
    month = src.get("publishMonth") or ""
    cites = {c.get("db"): c.get("count") for c in hit.get("citations") or []}
    return Paper(
        title=hit.get("title") or "",
        abstract="",
        authors=[a.get("displayName") or a.get("wosStandard") or ""
                 for a in (hit.get("names") or {}).get("authors") or []],
        year=int(year) if year else None,
        source="wos",
        url=links.get("record") or (f"https://doi.org/{ids['doi']}" if ids.get("doi") else ""),
        doi=ids.get("doi"),
        published=f"{year} {month}".strip() if year else None,
        venue=src.get("sourceTitle"),
        extra={
            "wos_uid": hit.get("uid"),
            "times_cited": cites.get("WOS"),
            "doc_types": hit.get("types") or [],
            "author_keywords": (hit.get("keywords") or {}).get("authorKeywords") or [],
        },
    )


def search(keywords: list[str], since: date, limit: int, cfg: dict) -> list[Paper]:
    key = os.environ.get("WOS_API_KEY")
    if not key:
        log.warning("[wos] 未設定 WOS_API_KEY，略過 Web of Science")
        return []
    query = build_query(keywords, since, cfg)
    log.info("[wos] 檢索式：%s", query)
    papers: list[Paper] = []
    for page in range(1, math.ceil(limit / PAGE_SIZE) + 1):
        params = {
            "q": query,
            "db": cfg.get("wos_db", "WOS"),  # WOS = 核心合集
            "limit": min(PAGE_SIZE, limit - len(papers)),
            "page": page,
            "sortField": "LD+D",  # 依收錄日期新到舊，最新收錄的論文排最前面
        }
        for attempt in range(4):
            resp = requests.get(API, params=params, headers={"X-ApiKey": key}, timeout=60)
            if resp.status_code != 429:
                break
            time.sleep(2 ** (attempt + 1))
        resp.raise_for_status()
        data = resp.json()
        hits = data.get("hits") or []
        papers += [_to_paper(h) for h in hits]
        total = (data.get("metadata") or {}).get("total", 0)
        if not hits or len(papers) >= min(limit, total):
            break
        time.sleep(1)
    return papers
