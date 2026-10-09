"""期刊 RSS（最新文章）來源。

出版社為每本期刊提供 RSS，本來就是給程式自動讀取的，所以當瀏覽器備援被網站擋下、
或沒有開啟瀏覽器備援時，改用 RSS 取得各期刊的最新文章，再用關鍵字篩選。

RSS 網址來源：
- journals.yaml 中該期刊的 rss 欄位（手動指定，從期刊首頁的 RSS 圖示複製）
- Elsevier、Wiley 期刊可依 ISSN 自動產生
"""

from __future__ import annotations

import html
import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import date
from email.utils import parsedate_to_datetime

import requests

from ..journals import Journal
from ..models import Paper
from .crossref import matches

log = logging.getLogger(__name__)
HEADERS = {"User-Agent": "paper-search-agent/0.1 (RSS reader for academic research)"}
NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "dc": "http://purl.org/dc/elements/1.1/",
    "prism": "http://prismstandard.org/namespaces/basic/2.0/",
    "rss1": "http://purl.org/rss/1.0/",
}
DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"<>&]+")
_cache: dict[str, str] = {}  # 同一次執行中，同一個 RSS 只下載一次


def _fetch(url: str) -> str:
    if url not in _cache:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        _cache[url] = resp.text
        time.sleep(0.5)
    return _cache[url]


def feed_urls(j: Journal) -> list[str]:
    if j.rss:
        return [j.rss]
    digits = [i.replace("-", "") for i in j.issns]
    publisher = j.publisher.lower()
    if publisher == "elsevier":
        return [f"https://rss.sciencedirect.com/publication/science/{d}" for d in digits]
    if publisher == "wiley":
        return [f"https://onlinelibrary.wiley.com/feed/{d}/most-recent" for d in digits]
    return []


def _text(el, *paths: str) -> str:
    for p in paths:
        found = el.find(p, NS)
        if found is not None:
            value = found.text or found.get("href") or ""
            if value.strip():
                return value.strip()
    return ""


def _date(raw: str) -> date | None:
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw).date()  # RSS 2.0：RFC 822
    except (TypeError, ValueError):
        pass
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", raw)  # Atom / Dublin Core：ISO 8601
    return date(int(m[1]), int(m[2]), int(m[3])) if m else None


def _clean(raw: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", raw)).split())


def parse_feed(xml_text: str, journal: Journal) -> list[Paper]:
    root = ET.fromstring(xml_text)
    items = (root.findall(".//item") or root.findall(".//rss1:item", NS)
             or root.findall(".//atom:entry", NS))
    papers = []
    for it in items:
        title = _clean(_text(it, "title", "rss1:title", "atom:title"))
        link = _text(it, "link", "rss1:link", "atom:link")
        desc = _text(it, "description", "rss1:description", "atom:summary", "atom:content")
        raw_date = _text(it, "pubDate", "dc:date", "prism:publicationDate", "atom:published", "atom:updated")
        doi = _text(it, "prism:doi", "dc:identifier").removeprefix("doi:").strip()
        if not doi.startswith("10."):
            m = DOI_RE.search(f"{link} {desc}")
            doi = m.group(0).rstrip(".") if m else ""
        authors = [a.text.strip() for a in it.findall("dc:creator", NS) if a.text and a.text.strip()]
        d = _date(raw_date)
        papers.append(Paper(
            title=title, abstract=_clean(desc), authors=authors, year=d.year if d else None,
            source="rss", url=link or (f"https://doi.org/{doi}" if doi else ""), doi=doi or None,
            published=d.isoformat() if d else None, venue=journal.name,
            extra={"pub_date": d},
        ))
    return papers


def search(keywords: list[str], since: date, limit: int, cfg: dict,
           journals: list[Journal] | None = None) -> list[Paper]:
    papers: list[Paper] = []
    for j in journals or []:
        for url in feed_urls(j):
            try:
                items = parse_feed(_fetch(url), j)
            except Exception as e:  # 單一 RSS 格式異常或連線失敗，不影響其他期刊
                log.debug("[rss] %s 讀取失敗（%s）：%s: %s", j.name, url, type(e).__name__, e)
                continue
            for p in items:
                d = p.extra.pop("pub_date")
                if d and d < since:
                    continue  # 沒有日期的項目保留，交給去重紀錄判斷是否看過
                if p.title and matches(f"{p.title} {p.abstract}", keywords):
                    papers.append(p)
            break  # 一個 RSS 讀到就好
        else:
            if feed_urls(j):
                log.warning("[rss] %s 的 RSS 都無法讀取", j.name)
    return papers[:limit * max(1, len(journals or []))]
