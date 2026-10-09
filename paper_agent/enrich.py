"""依 DOI 補齊摘要與開放取用 PDF 連結（Web of Science Starter API 沒有這兩項資料）。"""

from __future__ import annotations

import logging
import os

import requests

from .models import Paper
from .sources.openalex import _abstract

log = logging.getLogger(__name__)


def _from_openalex(doi: str) -> tuple[str, str | None]:
    email = os.environ.get("CONTACT_EMAIL") or os.environ.get("OPENALEX_EMAIL")
    params = {"mailto": email} if email else {}
    resp = requests.get(f"https://api.openalex.org/works/doi:{doi}", params=params, timeout=30)
    if resp.status_code == 404:
        return "", None
    resp.raise_for_status()
    w = resp.json()
    return _abstract(w.get("abstract_inverted_index")), (w.get("best_oa_location") or {}).get("pdf_url")


def _from_semantic_scholar(doi: str) -> tuple[str, str | None]:
    headers = {"x-api-key": os.environ["S2_API_KEY"]} if os.environ.get("S2_API_KEY") else {}
    resp = requests.get(f"https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}",
                        params={"fields": "abstract,openAccessPdf"}, headers=headers, timeout=30)
    if resp.status_code == 404:
        return "", None
    resp.raise_for_status()
    item = resp.json()
    return item.get("abstract") or "", (item.get("openAccessPdf") or {}).get("url") or None


def enrich(paper: Paper) -> None:
    """就地補上缺少的 abstract / pdf_url，任一來源補齊就停止。"""
    if not paper.doi or (paper.abstract and paper.pdf_url):
        return
    for lookup in (_from_openalex, _from_semantic_scholar):
        try:
            abstract, pdf_url = lookup(paper.doi)
        except requests.RequestException as e:
            log.debug("補資料失敗 %s (%s): %s", paper.doi, lookup.__name__, e)
            continue
        paper.abstract = paper.abstract or abstract
        paper.pdf_url = paper.pdf_url or pdf_url
        if paper.abstract and paper.pdf_url:
            return
