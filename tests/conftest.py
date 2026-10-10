"""共用的測試工具：假 HTTP 回應、假 AI、測試用設定。所有測試都不需要網路與 API 金鑰。"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

import pytest
import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from paper_agent.models import Paper  # noqa: E402
from paper_agent.sources import rss  # noqa: E402


class FakeResponse:
    def __init__(self, json_data=None, text="", content=b"", status=200):
        self._json = json_data if json_data is not None else {}
        self.text = text
        self.content = content
        self.status_code = status

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")


class Router:
    """依網址片段回傳假回應；calls 記錄所有請求。沒有對應的網址回傳空 JSON。"""

    def __init__(self, routes: dict | None = None):
        self.routes = routes or {}
        self.calls: list[tuple[str, dict | None]] = []

    def __call__(self, url, params=None, **kwargs):
        self.calls.append((url, params))
        for fragment, response in self.routes.items():
            if fragment in url:
                return response(url, params) if callable(response) else response
        return FakeResponse({"results": [], "data": [], "message": {"items": []}})

    def count(self, fragment: str) -> int:
        return sum(fragment in url for url, _ in self.calls)


class FakeLLM:
    """取代 Claude／Codex。可指定哪些論文分類或解析會失敗，用來測試錯誤處理。"""

    def __init__(self, relevance=5, fail_classify=(), fail_analyze=(), raise_all=False):
        self.relevance = relevance
        self.fail_classify = set(fail_classify)
        self.fail_analyze = set(fail_analyze)
        self.raise_all = raise_all
        self.classified: list[str] = []
        self.analyzed: list[str] = []

    def classify(self, paper: Paper, topic: dict) -> dict:
        self.classified.append(paper.title)
        if self.raise_all or paper.title in self.fail_classify:
            raise RuntimeError("API overloaded")
        return {"category": "consumer_behavior", "relevance": self.relevance, "tldr": f"摘要：{paper.title}"}

    def analyze(self, paper: Paper, topic: dict, pdf_path) -> str | None:
        self.analyzed.append(paper.title)
        if self.raise_all or paper.title in self.fail_analyze:
            return None
        return "## 一句話總結\n測試解析"

    def daily_overview(self, topic: dict, items: list[dict]) -> str:
        return "今日動態"


def openalex_work(title: str, doi: str | None, pdf: str | None = None, date: str = "2026-10-08") -> dict:
    return {"title": title, "doi": f"https://doi.org/{doi}" if doi else None,
            "abstract_inverted_index": {"Abstract": [0], "text.": [1]},
            "authorships": [{"author": {"display_name": "Ann Lee"}}], "publication_year": int(date[:4]),
            "id": f"W{abs(hash(title))}", "best_oa_location": {"pdf_url": pdf} if pdf else None,
            "primary_location": {"source": {"display_name": "Tourism Management"}}, "publication_date": date}


@pytest.fixture
def topic() -> dict:
    return {
        "name": "生成式 AI 觀光", "folder": "genai", "description": "測試主題",
        "keywords": ["generative AI tourism", "ChatGPT hospitality", "AI chatbot hotel"],
        "sources": ["openalex"], "use_journal_list": False,
        "categories": {"consumer_behavior": "消費者行為", "service_operations": "服務營運"},
    }


@pytest.fixture
def cfg(tmp_path, topic) -> dict:
    return {"output_dir": str(tmp_path / "library"), "lookback_days": 7, "max_results_per_source": 20,
            "min_relevance": 3, "max_analyses_per_topic": 10, "browser": {"enabled": False},
            "topics": [topic]}


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    """每個測試都清掉金鑰與 RSS 快取，並略過等待。"""
    for key in ("WOS_API_KEY", "ELSEVIER_API_KEY", "ANTHROPIC_API_KEY", "PAPER_AGENT_BROWSER"):
        monkeypatch.delenv(key, raising=False)
    rss._cache.clear()
    monkeypatch.setattr("time.sleep", lambda *_: None)


@pytest.fixture
def run_pipeline(monkeypatch):
    """以假 HTTP 與假 AI 執行完整流程，回傳快報路徑。"""
    from paper_agent import pipeline

    def _run(cfg, router: Router, llm: FakeLLM):
        monkeypatch.setattr("requests.get", router)
        monkeypatch.setattr(pipeline, "make_llm", lambda c: llm)
        return pipeline.run(cfg)

    return _run


def pdf_bytes() -> bytes:
    return b"%PDF-1.4 test"


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


__all__ = ["FakeResponse", "Router", "FakeLLM", "openalex_work", "pdf_bytes", "dumps", "mock"]
