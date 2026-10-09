"""arXiv API（免金鑰）。文件：https://info.arxiv.org/help/api/user-manual.html"""

from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from datetime import date

import requests

from ..models import Paper

API = "https://export.arxiv.org/api/query"
NS = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}


def _query(keywords: list[str]) -> str:
    # 每個關鍵字在標題或摘要中出現，關鍵字之間用 OR
    parts = []
    for kw in keywords:
        q = f'"{kw}"' if " " in kw else kw
        parts.append(f"(ti:{q} OR abs:{q})")
    return " OR ".join(parts)


def search(keywords: list[str], since: date, limit: int, cfg: dict) -> list[Paper]:
    params = {
        "search_query": _query(keywords),
        "sortBy": "submittedDate",
        "sortOrder": "descending",
        "max_results": limit,
    }
    resp = requests.get(API, params=params, timeout=60)
    resp.raise_for_status()
    time.sleep(3)  # arXiv 要求每次請求間隔 3 秒

    root = ET.fromstring(resp.text)
    papers = []
    for entry in root.findall("a:entry", NS):
        published = entry.findtext("a:published", "", NS)[:10]
        if published and date.fromisoformat(published) < since:
            continue
        abs_url = entry.findtext("a:id", "", NS)
        arxiv_id = abs_url.rsplit("/abs/", 1)[-1]
        pdf_url = next(
            (l.get("href") for l in entry.findall("a:link", NS) if l.get("title") == "pdf"),
            f"https://arxiv.org/pdf/{arxiv_id}",
        )
        papers.append(
            Paper(
                title=" ".join(entry.findtext("a:title", "", NS).split()),
                abstract=" ".join(entry.findtext("a:summary", "", NS).split()),
                authors=[a.findtext("a:name", "", NS) for a in entry.findall("a:author", NS)],
                year=int(published[:4]) if published else None,
                source="arxiv",
                url=abs_url,
                pdf_url=pdf_url,
                doi=entry.findtext("arxiv:doi", None, NS),
                arxiv_id=arxiv_id,
                published=published,
                venue="arXiv",
            )
        )
    return papers
