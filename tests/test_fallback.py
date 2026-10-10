"""API 失敗時的備援順序：瀏覽器 →（被擋、失敗、沒開啟）期刊 RSS。"""

from __future__ import annotations

from datetime import date

import pytest
from conftest import FakeResponse, Router

from paper_agent import pipeline
from paper_agent.browser.result import BrowserResult
from paper_agent.journals import Journal
from paper_agent.models import Paper

TM = Journal("Tourism Management", "Elsevier", 1, issns=["0000-0001"])
JST = Journal("Journal of Sustainable Tourism", "Taylor & Francis", 1, issns=["0000-0002"])
TE = Journal("Tourism Economics", "SAGE", 1, issns=[])
MISQ = Journal("MIS Quarterly", "MIS Research Center", 1, issns=[], rss="https://example.org/misq.rss")
JOURNALS = [TM, JST, TE, MISQ]
TOPIC = {"keywords": ["generative AI tourism"], "sources": ["wos", "sciencedirect", "crossref"]}
FEED = """<?xml version="1.0"?><rss version="2.0"><channel><item><title>Generative AI tourism from RSS</title>
<link>https://x/1</link><pubDate>Thu, 08 Oct 2026 00:00:00 GMT</pubDate></item></channel></rss>"""


@pytest.fixture
def browser_calls(monkeypatch):
    calls = []

    def install(blocked=False):
        def fake(site_key, keywords, journals, since, limit, cfg):
            calls.append((site_key, [j.name for j in journals]))
            return BrowserResult([Paper(title=f"From {site_key}", abstract="", authors=[], year=2026,
                                        source=f"browser:{site_key}", url="u", doi=f"10.9/{site_key}")],
                                 blocked=blocked)
        monkeypatch.setattr("paper_agent.browser.agent.run_browser_search", fake)
        return calls
    return install


@pytest.fixture
def http(monkeypatch):
    def respond(url, params):
        if "crossref" in url:
            return FakeResponse(status=503)  # Crossref 全部失敗
        return FakeResponse(text=FEED)
    router = Router({"": respond})
    monkeypatch.setattr("requests.get", router)
    return router


def collect(cfg, use_browser=True):
    return {p.title for p in pipeline.collect(TOPIC, date(2026, 10, 1), 20, JOURNALS, cfg, use_browser)}


def test_browser_fallback_routes_by_source_and_publisher(browser_calls, http):
    calls = browser_calls()
    titles = collect({"browser": {"enabled": True}})
    assert calls == [
        ("wos", ["Tourism Management", "Journal of Sustainable Tourism", "Tourism Economics", "MIS Quarterly"]),
        ("sciencedirect", ["Tourism Management"]),
        ("Taylor & Francis", ["Journal of Sustainable Tourism"]),
        ("SAGE", ["Tourism Economics"]),
    ]
    assert {"From wos", "From sciencedirect", "From Taylor & Francis", "From SAGE"} <= titles


def test_blocked_browser_falls_back_to_rss(browser_calls, http):
    browser_calls(blocked=True)
    titles = collect({"browser": {"enabled": True}})
    assert "Generative AI tourism from RSS" in titles
    assert http.count("example.org/misq.rss") == 1, "同一個 RSS 只讀一次"


def test_successful_browser_skips_rss_except_for_journals_without_site(browser_calls, http):
    browser_calls(blocked=False)
    collect({"browser": {"enabled": True}})
    assert http.count("rss.sciencedirect.com") == 1  # TM 在 Crossref 失敗，且 Elsevier 沒有對應的出版社網站群組
    assert http.count("example.org/misq.rss") == 1  # MISQ 沒有對應網站，只能讀 RSS


@pytest.mark.parametrize("cfg,use_browser,env", [
    ({"browser": {"enabled": True}}, False, None),   # dry-run
    ({"browser": {"enabled": False}}, True, None),   # 關閉瀏覽器備援
    ({"browser": {"enabled": True}}, True, "0"),     # GitHub Actions
])
def test_no_browser_still_reads_rss(browser_calls, http, monkeypatch, cfg, use_browser, env):
    if env:
        monkeypatch.setenv("PAPER_AGENT_BROWSER", env)
    calls = browser_calls()
    titles = collect(cfg, use_browser)
    assert calls == []
    assert "Generative AI tourism from RSS" in titles


def test_no_fallback_without_journal_list(browser_calls, http):
    calls = browser_calls()
    pipeline.collect(TOPIC, date(2026, 10, 1), 20, None, {"browser": {"enabled": True}})
    assert calls == []
