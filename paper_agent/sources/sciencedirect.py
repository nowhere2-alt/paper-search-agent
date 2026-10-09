"""Elsevier ScienceDirect Search API v2（官方 API，非商業學術用途免費）。

到 https://dev.elsevier.com 申請 API 金鑰，設定環境變數 ELSEVIER_API_KEY。
只會搜尋 journals.yaml 中 publisher 為 Elsevier 的期刊。
Elsevier 沒有把摘要登錄到 Crossref，所以這個來源能補足 Crossref 對 Elsevier 期刊只能比對標題的缺口。
"""

from __future__ import annotations

import logging
import os
import time
from datetime import date

import requests

from ..journals import Journal
from ..models import Paper, SourceUnavailable

log = logging.getLogger(__name__)
API = "https://api.elsevier.com/content/search/sciencedirect"


def _to_paper(r: dict, journal: Journal) -> Paper:
    pub_date = r.get("publicationDate") or ""
    authors = sorted(r.get("authors") or [], key=lambda a: a.get("order", 0))
    return Paper(
        title=r.get("title") or "",
        abstract="",
        authors=[a.get("name", "") for a in authors],
        year=int(pub_date[:4]) if pub_date[:4].isdigit() else None,
        source="sciencedirect",
        url=r.get("uri") or (f"https://doi.org/{r['doi']}" if r.get("doi") else ""),
        doi=r.get("doi"),
        published=pub_date or None,
        venue=r.get("sourceTitle") or r.get("publicationName") or journal.name,
        extra={"open_access": bool(r.get("openAccess"))},
    )


def search(keywords: list[str], since: date, limit: int, cfg: dict,
           journals: list[Journal] | None = None) -> list[Paper]:
    key = os.environ.get("ELSEVIER_API_KEY")
    elsevier = [j for j in journals or [] if j.publisher.lower() == "elsevier"]
    if not elsevier:
        return []
    if not key:
        raise SourceUnavailable("未設定 ELSEVIER_API_KEY", journals=elsevier)
    headers = {"X-ELS-APIKey": key, "Accept": "application/json", "Content-Type": "application/json"}
    terms = " OR ".join(f'"{kw}"' for kw in keywords)
    papers, failed = [], []
    for j in elsevier:
        body = {
            "tak": terms,  # 搜尋標題、摘要、作者關鍵字
            "pub": j.name,
            "date": f"{since.year}-{date.today().year}",
            "display": {"show": min(limit, 100), "sortBy": "date"},
        }
        try:
            resp = requests.put(API, json=body, headers=headers, timeout=60)
            if resp.status_code == 400:  # 舊版帳號不支援 tak 欄位時，改用全文檢索 qs
                body["qs"] = body.pop("tak")
                resp = requests.put(API, json=body, headers=headers, timeout=60)
            resp.raise_for_status()
        except requests.RequestException as e:
            log.warning("[sciencedirect] %s 查詢失敗：%s", j.name, e)
            failed.append(j)
            continue
        for r in resp.json().get("results") or []:
            pub = (r.get("publicationDate") or "")[:10]
            if len(pub) == 10 and pub < since.isoformat():
                continue
            papers.append(_to_paper(r, j))
        time.sleep(0.5)
    if failed:
        raise SourceUnavailable(f"{len(failed)} 本期刊查詢失敗", journals=failed, partial=papers)
    return papers
