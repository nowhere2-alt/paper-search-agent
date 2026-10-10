"""瀏覽器備援：Playwright 驅動程式（真的 Chromium）與 Claude 版 agent 的完整呼叫流程。

需要 Chromium：pip install playwright && playwright install chromium。
找不到瀏覽器時自動略過；也可用環境變數 PAPER_AGENT_TEST_CHROMIUM 指定執行檔。
Claude API 以本機的假伺服器代替，不需要金鑰。
"""

from __future__ import annotations

import functools
import http.server
import json
import os
import threading
from datetime import date
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

SITE_DIR = Path(__file__).parent / "fixtures" / "site"


def chromium() -> str | None:
    if path := os.environ.get("PAPER_AGENT_TEST_CHROMIUM"):
        return path
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            path = pw.chromium.executable_path
        return path if os.path.exists(path) else None
    except Exception:
        return None


CHROMIUM = chromium()
needs_chromium = pytest.mark.skipif(CHROMIUM is None, reason="沒有可用的 Chromium")


def serve(handler) -> str:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_port}"


@pytest.fixture(scope="module")
def site() -> str:
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass
    return serve(functools.partial(Quiet, directory=str(SITE_DIR)))


@pytest.fixture
def browser(tmp_path):
    from paper_agent.browser.driver import PlaywrightBrowser
    b = PlaywrightBrowser(tmp_path / "profile", ["127.0.0.1"], executable_path=CHROMIUM)
    yield b
    b.close()


def ref_of(listing: str, text: str) -> str:
    line = next(line for line in listing.splitlines() if text in line)
    return line.split("]")[0].lstrip("[")


@needs_chromium
def test_driver_fills_form_and_reads_results(browser, site):
    ctx = NS()
    nav = browser.navigate(ctx, NS(url=f"{site}/index.html", tab_id=None))
    assert nav.title == "Mock Journal Search"
    listing = browser.read_page(ctx, NS(filter="interactive", depth=None, ref=None, tab_id=None))
    browser.form_input(ctx, NS(target=NS(type="ref", ref=ref_of(listing, "input type=text")),
                               value="generative AI", tab_id=None))
    browser.form_input(ctx, NS(target=NS(type="ref", ref=ref_of(listing, "select")),
                               value="Tourism Management", tab_id=None))
    browser.left_click(ctx, NS(target=NS(type="ref", ref=ref_of(listing, "button")), modifiers=None, tab_id=None))
    text = browser.get_page_text(ctx, NS(tab_id=None))
    assert "q=generative+AI&journal=Tourism+Management" in text
    assert "ChatGPT and hotel guest trust" in text
    shot = browser.screenshot(ctx, NS(tab_id=None))
    assert shot.media_type == "image/png" and len(shot.data) > 1000


def test_url_policy():
    from anthropic.tools import ToolError
    from paper_agent.browser.driver import make_url_policy
    is_allowed, policy = make_url_policy(["webofscience.com"])
    assert is_allowed("https://www.webofscience.com/wos") and is_allowed("webofscience.com/x")
    for url in ("https://evil.example.org", "https://webofscience.com.evil.org", "javascript:alert(1)",
                "file:///etc/passwd", "data:text/html,hi"):
        assert not is_allowed(url), url
    with pytest.raises(ToolError):
        policy(None, "https://evil.example.org/steal")


@needs_chromium
def test_driver_blocks_link_to_other_domain(browser, site):
    ctx = NS()
    browser.navigate(ctx, NS(url=f"{site}/results.html", tab_id=None))
    link = browser.find(ctx, NS(query="Untrusted", tab_id=None))
    browser.left_click(ctx, NS(target=NS(type="ref", ref=ref_of(link, "Untrusted")), modifiers=None, tab_id=None))
    state = browser._browser_state(ctx)
    assert "evil.example.org" not in browser._page(None).url
    assert any(type(c).__name__ == "BetaNavigationRefused" for c in state.state_changes)


def test_private_network_detection():
    from paper_agent.browser.driver import is_private
    assert is_private("http://169.254.169.254/latest") and is_private("http://localhost:9222")
    assert is_private("https://10.0.0.5/") and not is_private("https://cdn.sciencedirectassets.com/a.js")


def fake_claude_api(script: list[list[dict]], seen: list[dict]) -> str:
    """依序回傳腳本中的 tool_use，記錄收到的請求。"""
    class API(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            seen.append(body)
            content = script[min(len(seen) - 1, len(script) - 1)]
            msg = {"id": f"msg_{len(seen)}", "type": "message", "role": "assistant", "model": body["model"],
                   "content": content, "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1},
                   "stop_reason": "tool_use" if any(b["type"] == "tool_use" for b in content) else "end_turn"}
            data = json.dumps(msg).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
    return serve(API)


def tool(i, name, inp, toolset="browser"):
    block = {"type": "tool_use", "id": f"toolu_{i}", "name": name, "input": inp}
    if toolset:
        block["toolset_name"] = toolset
    return block


@needs_chromium
@pytest.mark.parametrize("blocked", [False, True])
def test_claude_browser_agent_end_to_end(site, tmp_path, monkeypatch, blocked):
    from paper_agent.browser import agent, sites
    from paper_agent.journals import Journal

    last = ([tool(5, "report_problem", {"kind": "captcha", "detail": "robot check page"}, None)] if blocked else
            [tool(5, "report_paper", {"title": "ChatGPT and hotel guest trust", "journal": "Tourism Management",
                                       "authors": "Lee, Ann; Wu, Kai", "year": 2026,
                                       "doi": "https://doi.org/10.1016/j.tourman.2026.100001"}, None)])
    script = [
        [tool(1, "navigate", {"url": f"{site}/results.html"})],
        [tool(2, "get_page_text", {}), tool(3, "navigate", {"url": "https://evil.example.org/steal"})],
        last,
        [{"type": "text", "text": "Done."}],
    ]
    seen: list[dict] = []
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", fake_claude_api(script, seen))
    monkeypatch.setitem(sites.SITES, "test", {"label": "Mock DB", "start_url": f"{site}/index.html",
                                              "allowed_hosts": ["127.0.0.1"], "needs_login": False, "tips": "x"})
    cfg = {"browser": {"profile_dir": str(tmp_path / "p"), "executable_path": CHROMIUM, "max_steps": 10}}
    result = agent.run_browser_search("test", ["generative AI"], [Journal("Tourism Management", "Elsevier")],
                                      date(2026, 10, 1), 10, cfg)

    assert result.blocked is blocked
    if not blocked:
        [p] = result.papers
        assert (p.doi, p.authors) == ("10.1016/j.tourman.2026.100001", ["Lee, Ann", "Wu, Kai"])
    assert [t.get("type") or t.get("name") for t in seen[0]["tools"]] == \
        ["browser_toolset_20260801", "report_paper", "report_problem"]
    results = {r["tool_use_id"]: r for body in seen[1:] for r in body["messages"][-1]["content"]
               if r.get("type") == "tool_result"}
    assert results["toolu_1"]["toolset_name"] == "browser" and not results["toolu_1"].get("is_error")
    assert "Search results" in json.dumps(results["toolu_2"]["content"])
    assert results["toolu_3"]["is_error"] and "not on an allowed host" in json.dumps(results["toolu_3"]["content"])
