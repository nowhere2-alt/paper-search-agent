"""品質功能：快報的資料來源狀態、Unpaywall 與多個 PDF 候選、解析附頁碼、關鍵字擴充。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace as NS

from conftest import FakeLLM, FakeResponse, Router, openalex_work, pdf_bytes

from paper_agent import pipeline, prompts
from paper_agent.downloader import download_pdf
from paper_agent.enrich import enrich
from paper_agent.llm import _text, page_marker
from paper_agent.models import Paper

TOPIC = {"name": "T", "folder": "t", "description": "d", "keywords": ["generative AI tourism"],
         "categories": {"c": "x"}}


def paper(**kw) -> Paper:
    base = dict(title="Generative AI tourism destination marketing", abstract="", authors=["Ann Lee"],
                year=2026, source="s", url="u", doi="10.1/x")
    return Paper(**{**base, **kw})


# ---------- 快報的資料來源狀態 ----------

def test_digest_reports_source_status(cfg, run_pipeline, monkeypatch):
    from unittest import mock
    works = [openalex_work("Generative AI tourism destination marketing", "10.1/a")]
    router = Router({"api.openalex.org/works/doi:": FakeResponse({}),
                     "api.openalex.org/works": FakeResponse({"results": works})})
    cfg["topics"][0]["sources"] = ["wos", "openalex"]
    with mock.patch("paper_agent.pipeline.browser_fallback",
                    lambda *a, notes=None, **k: notes.append("期刊 RSS（Web of Science）：2 篇") or []):
        digest = run_pipeline(cfg, router, FakeLLM())[0]
    text = digest.read_text(encoding="utf-8")
    assert "## 資料來源狀態" in text
    assert "⚠ **wos**：API 無法使用（未設定 WOS_API_KEY）" in text
    assert "備援 期刊 RSS（Web of Science）：2 篇" in text
    assert "✓ **openalex**：1 篇" in text
    assert "全文 PDF：0/1 篇" in text


# ---------- Unpaywall 與 PDF 候選 ----------

def test_enrich_collects_pdf_candidates_from_unpaywall(monkeypatch):
    monkeypatch.setenv("CONTACT_EMAIL", "me@example.org")
    router = Router({
        "api.openalex.org/works/doi:": FakeResponse({"abstract_inverted_index": {"Hi": [0]}, "best_oa_location": None,
                                                     "locations": []}),
        "api.unpaywall.org": FakeResponse({"best_oa_location": None, "oa_locations": [
            {"url_for_pdf": "https://repo/submitted.pdf", "version": "submittedVersion"},
            {"url_for_pdf": "https://pub/published.pdf", "version": "publishedVersion"},
            {"url_for_pdf": None, "version": "acceptedVersion"}]}),
    })
    monkeypatch.setattr("requests.get", router)
    p = paper()
    enrich(p)
    assert p.abstract == "Hi"
    assert p.extra["pdf_candidates"] == ["https://pub/published.pdf", "https://repo/submitted.pdf"], "正式版優先"
    assert p.pdf_url == "https://pub/published.pdf"
    assert router.calls[1][1]["email"] == "me@example.org"
    assert router.count("semanticscholar") == 0, "摘要與 PDF 都有了就不再查"


def test_unpaywall_needs_email(monkeypatch):
    monkeypatch.delenv("CONTACT_EMAIL", raising=False)
    monkeypatch.delenv("OPENALEX_EMAIL", raising=False)
    router = Router()
    monkeypatch.setattr("requests.get", router)
    enrich(paper())
    assert router.count("unpaywall") == 0


def test_download_tries_next_candidate_when_link_is_not_pdf(tmp_path, monkeypatch):
    router = Router({"landing": FakeResponse(content=b"<html>login</html>"),
                     "broken": FakeResponse(status=503),
                     "good": FakeResponse(content=pdf_bytes())})
    monkeypatch.setattr("requests.get", router)
    p = paper(pdf_url="https://pub/landing")
    p.extra["pdf_candidates"] = ["https://pub/landing", "https://x/broken.pdf", "https://repo/good.pdf"]
    path = download_pdf(p, tmp_path)
    assert path and path.read_bytes() == pdf_bytes()
    assert p.pdf_url == "https://repo/good.pdf"
    assert len(router.calls) == 3


# ---------- 解析附頁碼 ----------

def cite(start, end):
    return NS(type="page_location", start_page_number=start, end_page_number=end)


def test_page_marker_uses_exclusive_end():
    assert page_marker([cite(5, 6)]) == " [p.5]"
    assert page_marker([cite(5, 7), cite(9, 10), cite(5, 6)]) == " [p.5–6, 9, 5]"
    assert page_marker([NS(type="char_location")]) == ""
    assert page_marker(None) == ""


def test_text_appends_page_markers_to_cited_blocks():
    msg = NS(stop_reason="end_turn", stop_details=None, content=[
        NS(type="text", text="## 主要發現\n樣本為 512 位旅客", citations=[cite(4, 5)]),
        NS(type="text", text="，信任顯著提高採用意願。\n", citations=[cite(7, 8)]),
        NS(type="text", text="## 限制\n論文未說明。", citations=None),
    ])
    assert _text(msg) == "## 主要發現\n樣本為 512 位旅客 [p.4]，信任顯著提高採用意願。 [p.7]\n## 限制\n論文未說明。"


def test_claude_analysis_request_enables_citations(tmp_path, monkeypatch):
    from paper_agent.llm import LLM
    monkeypatch.setattr("anthropic.Anthropic", lambda *a, **k: None)
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(pdf_bytes())
    params = LLM({})._analyze_params(paper(), TOPIC, pdf)
    doc = params["messages"][0]["content"][0]
    assert doc["type"] == "document" and doc["citations"] == {"enabled": True}
    assert "format" not in params["output_config"], "引用與結構化輸出不能同時使用"


def test_page_hint_only_for_text_fulltext():
    _, with_hint = prompts.analyze(paper(), TOPIC, "繁體中文", "全文", page_hint=True)
    _, without = prompts.analyze(paper(), TOPIC, "繁體中文", prompts.ABSTRACT_ONLY)
    assert prompts.PAGE_HINT in with_hint and prompts.PAGE_HINT not in without


# ---------- 關鍵字擴充 ----------

class ExpandingLLM(FakeLLM):
    def __init__(self, result=None, fail=False):
        super().__init__()
        self.result = result or ["ChatGPT hospitality", "large language model travel", "generative AI tourism"]
        self.fail = fail
        self.expand_calls = 0

    def expand_keywords(self, topic):
        self.expand_calls += 1
        if self.fail:
            raise RuntimeError("down")
        return self.result


def test_keyword_expansion_is_cached_and_regenerated_on_change(tmp_path):
    cfg = {"keyword_expansion": True}
    llm = ExpandingLLM()
    kws = pipeline.effective_keywords(cfg, TOPIC, tmp_path, llm)
    assert kws == ["generative AI tourism", "ChatGPT hospitality", "large language model travel"], "保留原詞、去重"
    assert pipeline.effective_keywords(cfg, TOPIC, tmp_path, llm) == kws and llm.expand_calls == 1, "用快取"
    saved = json.loads((tmp_path / ".state/keywords/t.json").read_text(encoding="utf-8"))
    assert saved["keywords"] == kws and "可以直接編輯" in saved["說明"]
    changed = {**TOPIC, "keywords": ["AI chatbot hotel"]}
    pipeline.effective_keywords(cfg, changed, tmp_path, llm)
    assert llm.expand_calls == 2, "關鍵字改了要重新產生"


def test_keyword_expansion_respects_manual_edits(tmp_path):
    cfg = {"keyword_expansion": True}
    pipeline.effective_keywords(cfg, TOPIC, tmp_path, ExpandingLLM())
    path = tmp_path / ".state/keywords/t.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["keywords"] = ["generative AI tourism", "my own term"]
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    assert pipeline.effective_keywords(cfg, TOPIC, tmp_path, ExpandingLLM()) == ["generative AI tourism", "my own term"]


def test_keyword_expansion_falls_back_to_original(tmp_path):
    cfg = {"keyword_expansion": True}
    assert pipeline.effective_keywords(cfg, TOPIC, tmp_path, None) == TOPIC["keywords"], "dry-run 沒有快取"
    assert pipeline.effective_keywords(cfg, TOPIC, tmp_path, ExpandingLLM(fail=True)) == TOPIC["keywords"]
    assert pipeline.effective_keywords({}, TOPIC, tmp_path, ExpandingLLM()) == TOPIC["keywords"], "預設關閉"


def test_expanded_keywords_are_used_for_search(cfg, run_pipeline):
    queries = []

    def respond(url, params):
        queries.append(params["search"])
        return FakeResponse({"results": []})
    cfg["keyword_expansion"] = True
    run_pipeline(cfg, Router({"api.openalex.org/works": respond}), ExpandingLLM(result=["LLM travel planning"]))
    assert '"LLM travel planning"' in queries[0] and '"generative AI tourism"' in queries[0]
