"""OpenAlex API（免金鑰，涵蓋多數期刊論文）。設定 OPENALEX_EMAIL 可進入較快的 polite pool。
文件：https://docs.openalex.org/
"""

from __future__ import annotations

import os
from datetime import date

import requests

from ..models import Paper

API = "https://api.openalex.org/works"


def _abstract(inverted: dict | None) -> str:
    # OpenAlex 以倒排索引提供摘要，需還原成文字
    if not inverted:
        return ""
    pos = {i: w for w, idxs in inverted.items() for i in idxs}
    return " ".join(pos[i] for i in sorted(pos))


def search(keywords: list[str], since: date, limit: int, cfg: dict) -> list[Paper]:
    params = {
        "search": " OR ".join(f'"{kw}"' for kw in keywords),
        "filter": f"from_publication_date:{since.isoformat()}",
        "sort": "publication_date:desc",
        "per-page": min(limit, 200),
    }
    if email := os.environ.get("OPENALEX_EMAIL"):
        params["mailto"] = email
    resp = requests.get(API, params=params, timeout=60)
    resp.raise_for_status()

    papers = []
    for w in resp.json().get("results", []):
        doi = (w.get("doi") or "").replace("https://doi.org/", "") or None
        oa = w.get("best_oa_location") or {}
        venue = ((w.get("primary_location") or {}).get("source") or {}).get("display_name")
        papers.append(
            Paper(
                title=w.get("title") or "",
                abstract=_abstract(w.get("abstract_inverted_index")),
                authors=[a["author"]["display_name"] for a in w.get("authorships", [])],
                year=w.get("publication_year"),
                source="openalex",
                url=w.get("doi") or w.get("id"),
                pdf_url=oa.get("pdf_url"),
                doi=doi,
                published=w.get("publication_date"),
                venue=venue,
            )
        )
    return papers
