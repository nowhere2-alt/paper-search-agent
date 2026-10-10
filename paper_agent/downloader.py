"""下載開放取用（Open Access）的 PDF。付費牆後的論文不會下載，只會用摘要做分析。"""

from __future__ import annotations

import logging
from pathlib import Path

import requests

from .models import Paper

log = logging.getLogger(__name__)
HEADERS = {"User-Agent": "paper-search-agent/0.1 (academic research tool)"}


def download_pdf(paper: Paper, dest_dir: Path) -> Path | None:
    """依序嘗試所有候選連結（來源提供的、OpenAlex、Unpaywall、Semantic Scholar），第一個真的是 PDF 的就用。"""
    urls = [u for u in [paper.pdf_url, *paper.extra.get("pdf_candidates", [])] if u]
    urls = list(dict.fromkeys(urls))
    if not urls:
        return None
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{paper.slug}.pdf"
    if dest.exists():
        return dest
    for url in urls:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=120, allow_redirects=True)
            resp.raise_for_status()
        except requests.RequestException as e:
            log.info("下載失敗，改試下一個連結 %s: %s", url, e)
            continue
        # 有些「PDF 連結」其實回傳 HTML 登入頁或文章頁，檢查檔頭
        if not resp.content.startswith(b"%PDF"):
            log.info("不是 PDF（可能需要訂閱權限），改試下一個連結：%s", url)
            continue
        dest.write_bytes(resp.content)
        paper.pdf_url = url
        return dest
    log.warning("找不到可下載的開放取用 PDF：%s", paper.title[:80])
    return None
