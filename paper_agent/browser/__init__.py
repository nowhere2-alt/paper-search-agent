"""API 無法使用時的備援。

順序：由 AI 操作瀏覽器到資料庫網站搜尋 →（瀏覽器被擋、失敗或沒開啟時）改讀期刊 RSS。
被判定為機器人時不嘗試繞過：網站擋下自動化操作是它的存取規則，改走出版社提供給程式讀取的 RSS。
"""

from __future__ import annotations

import logging
import os
from collections import defaultdict
from datetime import date

from ..journals import Journal
from ..models import Paper
from ..sources import rss
from .sites import FALLBACK_FOR, SITES

log = logging.getLogger(__name__)
NO_SITE = "_no_site"  # 沒有對應網站的期刊：只能讀 RSS


def _site_searcher(cfg: dict, keywords: list[str], since: date, limit: int):
    """回傳 search_site(site_key, journals) -> BrowserResult；無法使用瀏覽器時回傳 None。"""
    bcfg = cfg.get("browser", {})
    if not bcfg.get("enabled", False) or os.environ.get("PAPER_AGENT_BROWSER") == "0":
        return None
    if cfg.get("engine", "claude") == "codex":
        from ..codex_llm import CodexLLM
        from .codex_agent import run_browser_search_codex

        llm = CodexLLM(cfg.get("codex", {}), language=cfg.get("codex", {}).get("language", "繁體中文"))
        return lambda site_key, js: run_browser_search_codex(site_key, keywords, js, since, limit, cfg, llm)
    try:
        from . import agent
        import playwright  # noqa: F401
    except ImportError:
        log.warning("未安裝 Playwright，無法使用瀏覽器備援（pip install playwright && playwright install chromium）")
        return None
    return lambda site_key, js: agent.run_browser_search(site_key, keywords, js, since, limit, cfg)


def browser_fallback(source: str, keywords: list[str], journals: list[Journal] | None,
                     since: date, limit: int, cfg: dict, allow_browser: bool = True,
                     notes: list[str] | None = None) -> list[Paper]:
    """依失敗的 API 來源決定要搜哪些網站；不適用時回傳空清單。
    allow_browser=False（dry-run）時不啟動瀏覽器，只讀 RSS。"""
    target = FALLBACK_FOR.get(source)
    if not target or not journals:
        return []  # 只在期刊清單模式下備援，避免漫無邊際地瀏覽

    if target == "publisher":
        groups: dict[str, list[Journal]] = defaultdict(list)
        for j in journals:
            groups[j.publisher if j.publisher in SITES else NO_SITE].append(j)
    elif target == "sciencedirect":
        groups = {"sciencedirect": [j for j in journals if j.publisher.lower() == "elsevier"]}
    else:
        groups = {target: journals}

    search_site = _site_searcher(cfg, keywords, since, limit) if allow_browser else None
    notes = [] if notes is None else notes

    def note(text: str) -> None:
        if text not in notes:
            notes.append(text)

    papers: list[Paper] = []
    for site_key, js in groups.items():
        if not js:
            continue
        label = SITES[site_key]["label"] if site_key in SITES else "無對應網站的期刊"
        done = False
        if search_site and site_key != NO_SITE:
            try:
                result = search_site(site_key, js)
                papers += result.papers
                done = not result.blocked
                note(f"瀏覽器（{label}）：{'被網站擋下，改讀 RSS' if result.blocked else f'找到 {len(result.papers)} 篇'}")
            except Exception as e:  # 瀏覽器啟動失敗、網站錯誤等，不影響其他來源
                log.error("[browser:%s] 瀏覽器搜尋失敗：%s: %s", site_key, type(e).__name__, e)
                note(f"瀏覽器（{label}）：啟動或搜尋失敗，改讀 RSS")
        if not done:
            found = rss.search(keywords, since, limit, cfg, js)
            log.info("[rss] 以期刊 RSS 補抓 %s：%d 篇", site_key, len(found))
            note(f"期刊 RSS（{label}）：{len(found)} 篇")
            papers += found
    return papers
