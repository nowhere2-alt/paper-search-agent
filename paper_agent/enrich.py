"""依 DOI 補齊摘要與開放取用 PDF 連結（Web of Science Starter API 沒有這兩項資料）。"""

from __future__ import annotations

import logging
import os

import requests

from .models import Paper
from .sources.openalex import _abstract

log = logging.getLogger(__name__)


def _email() -> str | None:
    return os.environ.get("CONTACT_EMAIL") or os.environ.get("OPENALEX_EMAIL")


def _from_openalex(doi: str) -> tuple[str, list[str]]:
    params = {"mailto": _email()} if _email() else {}
    resp = requests.get(f"https://api.openalex.org/works/doi:{doi}", params=params, timeout=30)
    if resp.status_code == 404:
        return "", []
    resp.raise_for_status()
    w = resp.json()
    pdfs = [(w.get("best_oa_location") or {}).get("pdf_url")]
    pdfs += [loc.get("pdf_url") for loc in w.get("locations") or []]
    return _abstract(w.get("abstract_inverted_index")), [u for u in pdfs if u]


def _from_unpaywall(doi: str) -> tuple[str, list[str]]:
    """Unpaywall：列出所有合法的開放取用版本（出版社 OA、作者自存、機構典藏）。需要 CONTACT_EMAIL。"""
    if not _email():
        return "", []
    resp = requests.get(f"https://api.unpaywall.org/v2/{doi}", params={"email": _email()}, timeout=30)
    if resp.status_code == 404:
        return "", []
    resp.raise_for_status()
    data = resp.json()
    locations = [data.get("best_oa_location") or {}] + (data.get("oa_locations") or [])
    # 優先順序：出版社正式版 → 接受稿 → 投稿版
    rank = {"publishedVersion": 0, "acceptedVersion": 1, "submittedVersion": 2}
    locations.sort(key=lambda loc: rank.get(loc.get("version"), 3))
    return "", [loc["url_for_pdf"] for loc in locations if loc.get("url_for_pdf")]


def _from_semantic_scholar(doi: str) -> tuple[str, list[str]]:
    headers = {"x-api-key": os.environ["S2_API_KEY"]} if os.environ.get("S2_API_KEY") else {}
    resp = requests.get(f"https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}",
                        params={"fields": "abstract,openAccessPdf"}, headers=headers, timeout=30)
    if resp.status_code == 404:
        return "", []
    resp.raise_for_status()
    item = resp.json()
    url = (item.get("openAccessPdf") or {}).get("url")
    return item.get("abstract") or "", [url] if url else []


def enrich(paper: Paper) -> None:
    """依 DOI 補上缺少的摘要，並收集所有開放取用 PDF 的候選連結（下載時逐一嘗試）。
    摘要與 PDF 候選都有了就停止，避免多餘的查詢。"""
    if not paper.doi:
        return
    candidates: list[str] = paper.extra.setdefault("pdf_candidates", [])
    if paper.pdf_url and paper.pdf_url not in candidates:
        candidates.insert(0, paper.pdf_url)
    for lookup in (_from_openalex, _from_unpaywall, _from_semantic_scholar):
        if paper.abstract and candidates:
            break
        try:
            abstract, pdfs = lookup(paper.doi)
        except requests.RequestException as e:
            log.debug("補資料失敗 %s (%s): %s", paper.doi, lookup.__name__, e)
            continue
        paper.abstract = paper.abstract or abstract
        candidates += [u for u in pdfs if u not in candidates]
    paper.pdf_url = paper.pdf_url or (candidates[0] if candidates else None)
