"""Codex 版：真的執行 codex exec，但把模型換成本機的假 OpenAI Responses API。

需要 Codex CLI（npm install -g @openai/codex）；沒裝時自動略過。不需要 OpenAI 帳號或金鑰。
"""

from __future__ import annotations

import http.server
import json
import shutil
import threading
from pathlib import Path

import pytest

from paper_agent.codex_llm import CodexLLM
from paper_agent.models import Paper

pytestmark = pytest.mark.skipif(shutil.which("codex") is None, reason="沒有安裝 codex CLI")

TOPIC = {"name": "GenAI 觀光", "description": "d",
         "categories": {"consumer_behavior": "消費者", "service_operations": "營運"}}
PAPER = Paper(title="ChatGPT and hotel guest trust", abstract="We study trust.", authors=["Lee, Ann"],
              year=2026, source="t", url="u")


@pytest.fixture
def codex_home(tmp_path, monkeypatch):
    """啟動假的 Responses API，並讓 codex 透過自訂 model_provider 連到它。回傳收到的請求清單。"""
    requests: list[dict] = []

    class Responses(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))))
            user = [i for i in body["input"] if i.get("role") == "user"][-1]["content"][-1]["text"]
            fmt = body.get("text", {}).get("format", {})
            requests.append({"format": fmt, "effort": (body.get("reasoning") or {}).get("effort"), "user": user})
            if fmt.get("type") == "json_schema":
                text = json.dumps({"category": "consumer_behavior", "relevance": 4, "tldr": "測試"}, ensure_ascii=False)
            elif "<paper>" in user:
                text = "全文模式" + ("：含標記" if "TRUST-MARKER-42" in user else "")
            else:
                text = "摘要模式" if "本解析僅依據摘要" in user else "總覽"
            item = {"type": "message", "id": "m", "role": "assistant", "status": "completed",
                    "content": [{"type": "output_text", "text": text, "annotations": []}]}
            usage = {"input_tokens": 1, "input_tokens_details": {"cached_tokens": 0}, "output_tokens": 1,
                     "output_tokens_details": {"reasoning_tokens": 0}, "total_tokens": 2}
            events = [("response.created", {"type": "response.created", "response": {"id": "r"}}),
                      ("response.output_item.done", {"type": "response.output_item.done", "output_index": 0, "item": item}),
                      ("response.completed", {"type": "response.completed", "response": {"id": "r", "usage": usage}})]
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.end_headers()
            for name, data in events:
                self.wfile.write(f"event: {name}\ndata: {json.dumps(data)}\n\n".encode())

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Responses)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "config.toml").write_text(f'''model = "mock-model"
model_provider = "mock"
[model_providers.mock]
name = "Mock"
base_url = "http://127.0.0.1:{server.server_port}/v1"
wire_api = "responses"
env_key = "MOCK_API_KEY"
''')
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setenv("MOCK_API_KEY", "x")
    yield requests
    server.shutdown()


def test_classify_uses_strict_schema_and_low_effort(codex_home):
    result = CodexLLM({"timeout_sec": 90}).classify(PAPER, TOPIC)
    assert result == {"category": "consumer_behavior", "relevance": 4, "tldr": "測試"}
    [req] = codex_home
    assert req["format"]["type"] == "json_schema" and req["format"]["strict"] is True
    assert req["format"]["schema"]["properties"]["category"]["enum"] == ["consumer_behavior", "service_operations", "other"]
    assert req["effort"] == "low"
    assert "ChatGPT and hotel guest trust" in req["user"]


def test_analyze_sends_pdf_text(codex_home, tmp_path):
    pdf = tmp_path / "paper.pdf"
    try:
        from playwright.sync_api import sync_playwright
        import os
        exe = os.environ.get("PAPER_AGENT_TEST_CHROMIUM")
        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=exe)
            page = browser.new_page()
            page.set_content("<p>Hotel guests trust AI concierge chatbots. TRUST-MARKER-42.</p>" * 40)
            page.pdf(path=str(pdf))
            browser.close()
    except Exception:
        pytest.skip("無法產生測試用 PDF（需要 Chromium）")
    llm = CodexLLM({"timeout_sec": 90})
    assert llm.analyze(PAPER, TOPIC, pdf) == "全文模式：含標記"
    assert llm.analyze(PAPER, TOPIC, None) == "摘要模式"
    assert codex_home[0]["effort"] == "high"


def test_failure_returns_none(monkeypatch, tmp_path):
    """codex 連不上模型時回傳 None，交給主流程的重試機制處理。"""
    home = tmp_path / "bad-home"
    home.mkdir()
    (home / "config.toml").write_text('model = "m"\nmodel_provider = "mock"\n[model_providers.mock]\n'
                                      'name = "Mock"\nbase_url = "http://127.0.0.1:9/v1"\nwire_api = "responses"\n'
                                      'env_key = "MOCK_API_KEY"\nrequest_max_retries = 0\nstream_max_retries = 0\n')
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setenv("MOCK_API_KEY", "x")
    assert CodexLLM({"timeout_sec": 60}).run("hi") is None
