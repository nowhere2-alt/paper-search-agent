"""省錢功能：分類用不同模型、Message Batches API、各層級的搜尋週期。"""

from __future__ import annotations

import json
from datetime import date, timedelta
from types import SimpleNamespace as NS

import pytest
from conftest import FakeLLM, FakeResponse, Router, openalex_work

from paper_agent import pipeline
from paper_agent.llm import LLM
from paper_agent.models import Paper
from paper_agent.store import Store

TOPIC = {"name": "T", "description": "d", "categories": {"consumer_behavior": "c"}}


def paper(i: int) -> Paper:
    return Paper(title=f"Generative AI tourism paper number {i}", abstract="a", authors=["Ann Lee"],
                 year=2026, source="s", url="u", doi=f"10.1/{i}")


def text_message(text: str, stop_reason: str = "end_turn"):
    return NS(content=[NS(type="text", text=text)], stop_reason=stop_reason, stop_details=None)


class FakeAnthropic:
    """模擬 Anthropic client 的 messages.batches 與 beta.messages.stream。"""

    def __init__(self, finish_after_polls=1, outcomes=None, shuffle=False):
        self.finish_after_polls = finish_after_polls  # None = 永遠不會完成（模擬逾時）
        self.outcomes = outcomes or {}  # custom_id -> "errored" / "refusal" / "canceled"
        self.shuffle = shuffle
        self.created, self.streams, self.cancelled = [], [], []
        self.polls = 0
        self.messages = NS(batches=NS(create=self._create, retrieve=self._retrieve,
                                      cancel=self._cancel, results=self._results))
        self.beta = NS(messages=NS(stream=self._stream))

    def _status(self):
        done = self.cancelled or (self.finish_after_polls is not None and self.polls >= self.finish_after_polls)
        return NS(id="batch_1", processing_status="ended" if done else "in_progress")

    def _create(self, requests):
        self.created.append(list(requests))
        return self._status()

    def _retrieve(self, batch_id):
        self.polls += 1
        return self._status()

    def _cancel(self, batch_id):
        self.cancelled.append(batch_id)

    def _results(self, batch_id):
        reqs = self.created[-1]
        if self.shuffle:
            reqs = list(reversed(reqs))
        for r in reqs:
            outcome = self.outcomes.get(r["custom_id"], "canceled" if self.cancelled else "succeeded")
            if outcome == "canceled":
                continue
            if outcome == "errored":
                yield NS(custom_id=r["custom_id"], result=NS(type="errored", error=NS(error=NS(type="overloaded_error"))))
                continue
            params = r["params"]
            if "format" in params["output_config"]:
                body = json.dumps({"category": "consumer_behavior", "relevance": 4, "tldr": params["messages"][0]["content"][-20:]})
            else:
                body = "batch analysis"
            yield NS(custom_id=r["custom_id"], result=NS(
                type="succeeded", message=text_message(body, "refusal" if outcome == "refusal" else "end_turn")))

    def _stream(self, **params):
        self.streams.append(params)
        body = (json.dumps({"category": "consumer_behavior", "relevance": 3, "tldr": "single"})
                if "format" in params["output_config"] else "single analysis")
        return NS(__enter__=lambda: NS(get_final_message=lambda: text_message(body)), __exit__=lambda *a: False)


class Ctx:
    """讓 SimpleNamespace 可以用在 with 陳述式。"""

    def __init__(self, ns):
        self.ns = ns

    def __enter__(self):
        return self.ns.__enter__()

    def __exit__(self, *a):
        return False


@pytest.fixture
def make_llm(monkeypatch):
    def _make(client, **cfg):
        monkeypatch.setattr("anthropic.Anthropic", lambda *a, **k: client)
        llm = LLM({"batch_poll_sec": 0, **cfg})
        orig = client._stream
        client.beta.messages.stream = lambda **p: Ctx(orig(**p))
        return llm
    return _make


def test_classify_model_and_fallbacks_only_for_supported_models(make_llm):
    client = FakeAnthropic()
    llm = make_llm(client, classify_model="claude-haiku-5-5")
    llm.classify(paper(1), TOPIC)
    llm.analyze(paper(1), TOPIC, None)
    classify_req, analyze_req = client.streams
    assert classify_req["model"] == "claude-haiku-5-5" and "fallbacks" not in classify_req
    assert analyze_req["model"] == "claude-opus-5-5" and analyze_req["fallbacks"] == "default"


def test_batch_classify_keeps_order_and_uses_half_price_endpoint(make_llm):
    client = FakeAnthropic(shuffle=True)
    llm = make_llm(client, use_batch=True)
    papers = [paper(i) for i in range(3)]
    results = list(llm.classify_batch(papers, TOPIC))
    assert [p.uid for p, _, _ in results] == [p.uid for p in papers]
    assert all(r["relevance"] == 4 for _, r, _ in results)
    assert len(client.created) == 1 and client.streams == []
    assert all("fallbacks" not in r["params"] for r in client.created[0]), "批次不支援 fallbacks"


def test_batch_errors_and_refusals_become_failures(make_llm):
    client = FakeAnthropic(outcomes={"r0": "errored", "r1": "refusal"})
    llm = make_llm(client, use_batch=True)
    results = list(llm.classify_batch([paper(0), paper(1), paper(2)], TOPIC))
    assert results[0][1] is None and "overloaded_error" in results[0][2]
    assert results[1][1] is None
    assert results[2][1]["relevance"] == 4


def test_batch_timeout_cancels_and_falls_back_to_normal_calls(make_llm):
    client = FakeAnthropic(finish_after_polls=None)
    llm = make_llm(client, use_batch=True, batch_timeout_min=0)
    results = list(llm.analyze_batch([(paper(0), None), (paper(1), None)], TOPIC))
    assert client.cancelled == ["batch_1"]
    assert [t for _, t, _ in results] == ["single analysis", "single analysis"]
    assert len(client.streams) == 2


def test_batch_off_uses_normal_calls(make_llm):
    client = FakeAnthropic()
    llm = make_llm(client)
    list(llm.classify_batch([paper(0)], TOPIC))
    assert client.created == [] and len(client.streams) == 1


# ---------- 各層級的搜尋週期 ----------

def test_tier_window(tmp_path):
    store = Store(tmp_path / "p.db")
    today = date(2026, 10, 10)
    cfg = {"tier_every_days": {3: 7}, "max_lookback_days": 30}
    assert pipeline.tier_window(store, "T", 1, today, 3, cfg, {}) == date(2026, 10, 7)
    store.mark_tier_run("T", 1, today)
    assert pipeline.tier_window(store, "T", 1, today, 3, cfg, {}) == date(2026, 10, 7), "每天搜的層級同一天重跑不跳過"
    store.mark_tier_run("T", 3, today - timedelta(days=3))
    assert pipeline.tier_window(store, "T", 3, today, 3, cfg, {}) is None, "Tier 3 每 7 天搜一次"
    store.mark_tier_run("T", 3, today - timedelta(days=8))
    assert pipeline.tier_window(store, "T", 3, today, 3, cfg, {}) == date(2026, 10, 1), "補到上次搜尋的前一天"
    store.mark_tier_run("T", 2, today - timedelta(days=100))
    assert pipeline.tier_window(store, "T", 2, today, 3, cfg, {}) == date(2026, 9, 10), "最多往回 max_lookback_days"
    assert pipeline.tier_window(store, "T", 3, today, 3, cfg, {"tier_every_days": {"3": 1}}) is not None, "主題設定可覆寫"


def test_gap_after_downtime_is_backfilled(cfg, run_pipeline, monkeypatch):
    """電腦關機幾天後，下次執行要往回補搜，不能只看 lookback_days。"""
    calls = []

    def respond(url, params):
        calls.append(params["filter"])
        return FakeResponse({"results": []})
    router = Router({"api.openalex.org/works": respond})
    cfg["lookback_days"] = 2
    run_pipeline(cfg, router, FakeLLM())
    store = Store(__import__("pathlib").Path(cfg["output_dir"]) / ".state" / "papers.db")
    store.mark_tier_run("生成式 AI 觀光", 0, date.today() - timedelta(days=9))
    calls.clear()
    run_pipeline(cfg, router, FakeLLM())
    expected = (date.today() - timedelta(days=10)).isoformat()
    assert f"from_publication_date:{expected}" in calls[0]


def test_weekly_tier_skipped_between_runs(cfg, run_pipeline):
    router = Router({"api.openalex.org/works": FakeResponse({"results": [
        openalex_work("Generative AI tourism destination marketing", "10.1/b")]})})
    cfg["tier_every_days"] = {0: 7}
    run_pipeline(cfg, router, FakeLLM())
    searched = router.count("api.openalex.org/works")
    run_pipeline(cfg, router, FakeLLM())
    assert router.count("api.openalex.org/works") == searched, "7 天內不再搜尋"


class BatchFakeLLM(FakeLLM):
    """提供 classify_batch / analyze_batch 的假 AI，確認主流程走批次介面。"""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.batch_calls = []

    def classify_batch(self, papers, topic):
        self.batch_calls.append(("classify", len(papers)))
        for p in papers:
            try:
                yield p, self.classify(p, topic), ""
            except Exception as e:
                yield p, None, str(e)

    def analyze_batch(self, jobs, topic):
        self.batch_calls.append(("analyze", len(jobs)))
        for p, pdf in jobs:
            text = self.analyze(p, topic, pdf)
            yield p, text, "" if text else "failed"


def test_pipeline_uses_batch_interface(cfg, run_pipeline):
    works = [openalex_work(f"Generative AI tourism paper number {i}", f"10.1/p{i}") for i in range(3)]
    router = Router({"api.openalex.org/works/doi:": FakeResponse({}),
                     "api.openalex.org/works": FakeResponse({"results": works})})
    llm = BatchFakeLLM()
    digest = run_pipeline(cfg, router, llm)[0]
    assert llm.batch_calls == [("classify", 3), ("analyze", 3)]
    assert digest.read_text(encoding="utf-8").count("- **[Generative AI tourism paper number") == 3


def test_breaker_stops_consuming_batch_results(cfg, run_pipeline):
    works = [openalex_work(f"Generative AI tourism paper number {i}", f"10.1/p{i}") for i in range(6)]
    router = Router({"api.openalex.org/works/doi:": FakeResponse({}),
                     "api.openalex.org/works": FakeResponse({"results": works})})
    llm = BatchFakeLLM(raise_all=True)
    result = run_pipeline(cfg, router, llm)
    assert result.ai_failed and len(llm.classified) == 3
