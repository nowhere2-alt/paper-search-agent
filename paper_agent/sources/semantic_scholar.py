"""Semantic Scholar Graph API。無金鑰也能用（共用速率限制），建議申請免費金鑰並設 S2_API_KEY。
文件：https://api.semanticscholar.org/api-docs/graph
"""

from __future__ import annotations

import os
import time
from datetime import date

import requests

from ..models import Paper

API = "https://api.semanticscholar.org/graph/v1/paper/search/bulk"
FIELDS = "title,abstract,authors,year,externalIds,url,openAccessPdf,publicationDate,venue"


def search(keywords: list[str], since: date, limit: int, cfg: dict) -> list[Paper]:
    headers = {}
    if key := os.environ.get("S2_API_KEY"):
        headers["x-api-key"] = key
    params = {
        # bulk search 支援布林語法：| 代表 OR，雙引號代表片語
        "query": " | ".join(f'"{kw}"' for kw in keywords),
        "fields": FIELDS,
        "publicationDateOrYear": f"{since.isoformat()}:",
        "sort": "publicationDate:desc",
    }
    for attempt in range(4):
        resp = requests.get(API, params=params, headers=headers, timeout=60)
        if resp.status_code != 429:
            break
        time.sleep(2 ** (attempt + 1))
    resp.raise_for_status()

    papers = []
    for item in (resp.json().get("data") or [])[:limit]:
        ext = item.get("externalIds") or {}
        pdf = (item.get("openAccessPdf") or {}).get("url") or None
        papers.append(
            Paper(
                title=item.get("title") or "",
                abstract=item.get("abstract") or "",
                authors=[a.get("name", "") for a in item.get("authors") or []],
                year=item.get("year"),
                source="semantic_scholar",
                url=item.get("url") or "",
                pdf_url=pdf,
                doi=ext.get("DOI"),
                arxiv_id=ext.get("ArXiv"),
                published=item.get("publicationDate"),
                venue=item.get("venue") or None,
            )
        )
    return papers
