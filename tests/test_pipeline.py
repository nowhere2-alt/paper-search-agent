"""主流程：去重、分主題紀錄、錯誤處理、重試與斷路器。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from conftest import FakeLLM, FakeResponse, Router, openalex_work, pdf_bytes

from paper_agent import pipeline
from paper_agent.models import Paper
from paper_agent.store import MAX_ATTEMPTS, Store


def router_with(works: list[dict]) -> Router:
    """OpenAlex 搜尋回傳 works；依 DOI 補資料時回傳空結果；PDF 連結回傳假 PDF。"""
    return Router({
        "api.openalex.org/works/doi:": FakeResponse({"abstract_inverted_index": None, "best_oa_location": None}),
        "api.openalex.org/works": FakeResponse({"results": works}),
        ".pdf": FakeResponse(content=pdf_bytes()),
    })


def test_full_run_writes_analyses_and_digest(cfg, run_pipeline):
    llm = FakeLLM()
    works = [openalex_work("ChatGPT hospitality and guest trust in hotels", "10.1/a", pdf="https://oa/a.pdf"),
             openalex_work("Generative AI tourism destination marketing", "10.1/b")]
    digest = run_pipeline(cfg, router_with(works), llm)[0]
    text = digest.read_text(encoding="utf-8")
    assert "今日動態" in text and "ChatGPT hospitality" in text and "Generative AI tourism" in text
    analyses = sorted(Path(cfg["output_dir"]).glob("genai/consumer_behavior/*.md"))
    assert len(analyses) == 2
    assert list(Path(cfg["output_dir"]).glob("genai/consumer_behavior/*.pdf"))


def test_second_run_skips_processed_papers(cfg, run_pipeline):
    works = [openalex_work("ChatGPT hospitality and guest trust in hotels", "10.1/a")]
    run_pipeline(cfg, router_with(works), FakeLLM())
    llm2 = FakeLLM()
    run_pipeline(cfg, router_with(works), llm2)
    assert llm2.classified == []


def test_same_paper_with_and_without_doi_is_deduplicated(cfg, run_pipeline):
    """第一天從有 DOI 的來源進來，第二天從沒有 DOI 的來源（例如 RSS）進來，不應再處理一次。"""
    title = "ChatGPT hospitality and guest trust in hotels"
    run_pipeline(cfg, router_with([openalex_work(title, "10.1/a")]), FakeLLM())
    llm2 = FakeLLM()
    run_pipeline(cfg, router_with([openalex_work(title.upper() + ".", None)]), llm2)
    assert llm2.classified == []


def test_dedup_within_one_run_across_sources(cfg, topic):
    """同一次搜尋中，一個來源有 DOI、另一個沒有，應合併成一篇並保留 DOI。"""
    title = "Generative AI tourism destination marketing"
    a = Paper(title=title, abstract="", authors=[], year=2026, source="s1", url="u1")
    b = Paper(title=title, abstract="abs", authors=[], year=2026, source="s2", url="u2", doi="10.1/x")
    sources = {"s1": lambda *a_, **k: [a], "s2": lambda *a_, **k: [b]}
    topic["sources"] = ["s1", "s2"]
    from unittest import mock
    with mock.patch.dict(pipeline.SOURCES, sources):
        papers = pipeline.collect(topic, None, 10, None, cfg)
    assert len(papers) == 1 and papers[0].doi == "10.1/x" and papers[0].abstract == "abs"


def test_records_are_per_topic(cfg, topic, run_pipeline):
    """同一篇論文在主題 A 處理過，主題 B 仍要評估。"""
    other = dict(topic, name="另一個主題", folder="other")
    cfg["topics"] = [topic, other]
    llm = FakeLLM()
    run_pipeline(cfg, router_with([openalex_work("ChatGPT hospitality and guest trust in hotels", "10.1/a")]), llm)
    assert len(llm.classified) == 2


def test_one_failing_paper_does_not_stop_the_run(cfg, run_pipeline):
    bad = "ChatGPT hospitality broken paper example"
    llm = FakeLLM(fail_classify={bad})
    works = [openalex_work(bad, "10.1/bad"), openalex_work("Generative AI tourism destination marketing", "10.1/ok")]
    digest = run_pipeline(cfg, router_with(works), llm)[0]
    text = digest.read_text(encoding="utf-8")
    assert "Generative AI tourism" in text and bad not in text


def test_failed_paper_is_retried_then_given_up(cfg, run_pipeline):
    bad = "ChatGPT hospitality broken paper example"
    good = "Generative AI tourism destination marketing"
    for day in range(MAX_ATTEMPTS + 1):
        llm = FakeLLM(fail_classify={bad})
        works = [openalex_work(bad, "10.1/bad"), openalex_work(f"{good} {day}", f"10.1/ok{day}")]
        run_pipeline(cfg, router_with(works), llm)
        if day < MAX_ATTEMPTS:
            assert bad in llm.classified, f"第 {day + 1} 次應該重試"
        else:
            assert bad not in llm.classified, "超過重試上限後應放棄"


def test_breaker_stops_on_systemic_failure_without_recording(cfg, run_pipeline):
    """AI 服務整個掛掉（例如金鑰失效）時：停止處理、快報加警告、不計入各篇論文的失敗次數。"""
    works = [openalex_work(f"Generative AI tourism paper number {i}", f"10.1/p{i}") for i in range(6)]
    llm = FakeLLM(raise_all=True)
    digest = run_pipeline(cfg, router_with(works), llm)[0]
    assert len(llm.classified) == 3, "連續 3 次失敗就該停止"
    assert "AI 服務連續失敗" in digest.read_text(encoding="utf-8")
    db = sqlite3.connect(Path(cfg["output_dir"]) / ".state" / "papers.db")
    assert db.execute("SELECT COUNT(*) FROM failures").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM papers").fetchone()[0] == 0
    llm2 = FakeLLM()
    run_pipeline(cfg, router_with(works), llm2)
    assert len(llm2.classified) == 6, "服務恢復後全部重試"


def test_analysis_failure_retried_then_listed_without_analysis(cfg, run_pipeline):
    title = "ChatGPT hospitality and guest trust in hotels"
    works = [openalex_work(title, "10.1/a"), openalex_work("Generative AI tourism destination marketing", "10.1/b")]
    for attempt in range(1, MAX_ATTEMPTS + 1):
        llm = FakeLLM(fail_analyze={title})
        digest = run_pipeline(cfg, router_with(works), llm)[0]
        assert title in llm.analyzed
        text = digest.read_text(encoding="utf-8")
        if attempt < MAX_ATTEMPTS:
            assert title not in text, "還會重試的論文不先列進快報"
        else:
            assert title in text, "到重試上限後，不附解析但仍列進快報"


def test_migrates_old_database(tmp_path):
    path = tmp_path / "papers.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE papers (uid TEXT PRIMARY KEY, topic TEXT, title TEXT, category TEXT, relevance INTEGER, "
               "tldr TEXT, pdf_path TEXT, analysis_path TEXT, meta TEXT, processed_at TEXT)")
    db.execute("INSERT INTO papers VALUES ('doi:10.1/a', 'T', 'ChatGPT hospitality and guest trust in hotels', "
               "'c', 5, 't', NULL, NULL, '{}', 'now')")
    db.commit()
    db.close()
    store = Store(path)
    same_by_title = Paper(title="ChatGPT Hospitality and Guest Trust in Hotels", abstract="", authors=[],
                          year=None, source="rss", url="")
    assert store.seen("T", same_by_title)
    assert not store.seen("其他主題", same_by_title)
