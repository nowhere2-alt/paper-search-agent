"""API 無法使用時的備援：由 Claude 操作瀏覽器搜尋資料庫網站。"""

from __future__ import annotations

import logging
import os
from collections import defaultdict
from datetime import date

from ..journals import Journal
from ..models import Paper
from .sites import FALLBACK_FOR, SITES

log = logging.getLogger(__name__)


def browser_fallback(source: str, keywords: list[str], journals: list[Journal] | None,
                     since: date, limit: int, cfg: dict) -> list[Paper]:
    """依失敗的 API 來源決定要搜哪些網站；不適用時回傳空清單。"""
    bcfg = cfg.get("browser", {})
    if not bcfg.get("enabled", False) or os.environ.get("PAPER_AGENT_BROWSER") == "0":
        return []
    target = FALLBACK_FOR.get(source)
    if not target or not journals:
        return []  # 只在期刊清單模式下備援，避免漫無邊際地瀏覽
    if cfg.get("engine", "claude") == "codex":
        from ..codex_llm import CodexLLM
        from .codex_agent import run_browser_search_codex

        llm = CodexLLM(cfg.get("codex", {}), language=cfg.get("codex", {}).get("language", "繁體中文"))

        def search_site(site_key, js):
            return run_browser_search_codex(site_key, keywords, js, since, limit, cfg, llm)
    else:
        try:
            from . import agent
            import playwright  # noqa: F401
        except ImportError:
            log.warning("未安裝 Playwright，無法使用瀏覽器備援（pip install playwright && playwright install chromium）")
            return []

        def search_site(site_key, js):
            return agent.run_browser_search(site_key, keywords, js, since, limit, cfg)

    if target == "publisher":
        groups: dict[str, list[Journal]] = defaultdict(list)
        for j in journals:
            if j.publisher in SITES:
                groups[j.publisher].append(j)
    elif target == "sciencedirect":
        groups = {"sciencedirect": [j for j in journals if j.publisher.lower() == "elsevier"]}
    else:
        groups = {target: journals}

    papers: list[Paper] = []
    for site_key, js in groups.items():
        if not js:
            continue
        try:
            papers += search_site(site_key, js)
        except Exception as e:  # 瀏覽器啟動失敗、網站錯誤等，不影響其他來源
            log.error("[browser:%s] 瀏覽器搜尋失敗：%s: %s", site_key, type(e).__name__, e)
    return papers
