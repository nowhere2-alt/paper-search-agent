"""Codex 版瀏覽器備援：codex exec 透過 Playwright MCP 伺服器操作瀏覽器，到資料庫網站搜尋。

與 Claude 版（agent.py）的差異：
- Claude 版用 Anthropic 的 browser use toolset，網域限制由本專案的驅動程式（driver.py）強制執行。
- Codex 版用 Playwright MCP（@playwright/mcp），它的 --allowed-origins 官方說明「不是安全邊界」，
  所以網域限制主要靠提示詞，安全性較弱。建議第一次執行時設 headless: false 觀察。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date
from pathlib import Path

from ..journals import Journal
from ..models import Paper
from .agent import SYSTEM, _prompt
from .sites import SITES

log = logging.getLogger(__name__)
PLAYWRIGHT_MCP = "@playwright/mcp@0.0.83"  # 固定版本，避免每次抓到不同版本

SCHEMA = {
    "type": "object",
    "properties": {
        "papers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "journal": {"type": "string"},
                    "authors": {"type": "string", "description": "Semicolon-separated"},
                    "year": {"type": "integer"},
                    "publication_date": {"type": "string"},
                    "doi": {"type": "string"},
                    "url": {"type": "string"},
                    "abstract": {"type": "string"},
                },
                "required": ["title", "journal", "authors", "year", "publication_date", "doi", "url", "abstract"],
                "additionalProperties": False,
            },
        },
        "problem": {"type": "string", "description": "Empty if the search worked; otherwise kind: detail"},
    },
    "required": ["papers", "problem"],
    "additionalProperties": False,
}


def codex_profile_dir(bcfg: dict) -> Path:
    return Path(os.path.expanduser(bcfg.get("codex_profile_dir", "~/.paper-agent/codex-browser-profile")))


def chromium_path(bcfg: dict) -> str | None:
    """讓 Playwright MCP 與 --browser-login 用同一個 Chromium，登入狀態才能共用。"""
    if bcfg.get("executable_path"):
        return bcfg["executable_path"]
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            return pw.chromium.executable_path
    except Exception:
        return None


def mcp_overrides(bcfg: dict) -> list[str]:
    """以 -c 參數設定 Playwright MCP，不需要修改使用者的 ~/.codex/config.toml。"""
    args = [PLAYWRIGHT_MCP, "--user-data-dir", str(codex_profile_dir(bcfg)), "--block-service-workers"]
    if bcfg.get("headless", True):
        args.append("--headless")
    if exe := chromium_path(bcfg):
        args += ["--executable-path", exe]
    return ["-c", 'mcp_servers.playwright.command="npx"',
            "-c", f"mcp_servers.playwright.args={json.dumps(['-y'] + args)}",
            "-c", "mcp_servers.playwright.startup_timeout_sec=60"]


def run_browser_search_codex(site_key: str, keywords: list[str], journals: list[Journal],
                             since: date, limit: int, cfg: dict, llm) -> list[Paper]:
    site = SITES[site_key]
    bcfg = cfg.get("browser", {})
    hosts = ", ".join(site["allowed_hosts"] + bcfg.get("extra_allowed_hosts", []))
    prompt = f"""{SYSTEM}

Use the Playwright browser tools (playwright MCP server) to do this. Do not use shell commands.
Only open pages on these domains and their subdomains: {hosts}. Never follow links to other domains.
Instead of a report_paper tool, put every matching article in the "papers" array of your final JSON answer
(use "" or 0 for unknown fields). If you had to stop at a login page, CAPTCHA, robot check or access-denied page,
describe it in "problem" (for example "login_required: ...") and return the articles found so far.

{_prompt(site, keywords, journals, since, limit)}"""
    profile = codex_profile_dir(bcfg)
    profile.mkdir(parents=True, exist_ok=True)
    log.info("[codex-browser:%s] 開始以瀏覽器搜尋 %d 本期刊", site_key, len(journals))
    text = llm.run(prompt, schema=SCHEMA, effort=bcfg.get("effort", "medium"), sandbox="read-only",
                   extra_args=mcp_overrides(bcfg), timeout=bcfg.get("timeout_sec", 1200))
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        log.warning("[codex-browser:%s] 結果不是合法 JSON", site_key)
        return []
    if data.get("problem"):
        log.warning("[codex-browser:%s] 無法完成搜尋：%s", site_key, data["problem"])
        if data["problem"].startswith("login_required") and site.get("needs_login"):
            log.warning("  請先執行 python -m paper_agent --config config.codex.yaml --browser-login %s", site_key)
    papers = []
    for r in data.get("papers", [])[:limit]:
        doi = r["doi"].strip().removeprefix("https://doi.org/").removeprefix("doi:") or None
        papers.append(Paper(
            title=" ".join(r["title"].split()), abstract=r["abstract"].strip(),
            authors=[a.strip() for a in r["authors"].split(";") if a.strip()],
            year=r["year"] or None, source=f"codex-browser:{site_key}",
            url=r["url"] or (f"https://doi.org/{doi}" if doi else ""), doi=doi,
            published=r["publication_date"] or None, venue=r["journal"],
        ))
    log.info("[codex-browser:%s] 回報 %d 篇", site_key, len(papers))
    return papers
