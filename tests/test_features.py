"""第四部分：共用設定（include）、RIS／BibTeX 匯出與 Zotero 同步、推播、每週綜合報告。"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from conftest import FakeLLM, FakeResponse, Router, openalex_work

from paper_agent import notify, weekly
from paper_agent.config import deep_merge, load_config
from paper_agent.export import Zotero, export, markdown_to_html, split_name, to_bibtex, to_ris
from paper_agent.models import Paper
from paper_agent.store import Store

ROOT = Path(__file__).resolve().parents[1]
TOPIC = {"name": "生成式 AI 觀光", "folder": "genai", "description": "d", "keywords": ["x"],
         "categories": {"consumer_behavior": "c"}}
R = {"category": "consumer_behavior", "relevance": 5, "tldr": "旅客信任 AI 客服"}


def paper(**kw) -> Paper:
    base = dict(title="ChatGPT & hotel guest trust: 100% {new}", abstract="We study trust.",
                authors=["Lee, Ann", "Kai Wu"], year=2026, source="s", url="https://doi.org/10.1/a",
                doi="10.1/a", published="2026-10-08", venue="Tourism Management")
    return Paper(**{**base, **kw})


# ---------- 共用設定 ----------

def test_repo_configs_share_settings():
    claude, codex = load_config(ROOT / "config.yaml"), load_config(ROOT / "config.codex.yaml")
    assert claude["engine"] == "claude" and codex["engine"] == "codex"
    assert claude["topics"] == codex["topics"] and claude["topics"]
    assert claude["browser"]["enabled"] and claude["browser"]["profile_dir"].endswith("browser-profile")
    assert codex["browser"]["codex_profile_dir"].endswith("codex-browser-profile")
    assert "include" not in claude


def test_include_merges_dicts_and_detects_cycles(tmp_path):
    (tmp_path / "base.yaml").write_text("a: 1\nb: {x: 1, y: 2}\nl: [1, 2]\n", encoding="utf-8")
    (tmp_path / "top.yaml").write_text("include: base.yaml\nb: {y: 3}\nl: [9]\n", encoding="utf-8")
    assert load_config(tmp_path / "top.yaml") == {"a": 1, "b": {"x": 1, "y": 3}, "l": [9]}
    (tmp_path / "loop.yaml").write_text("include: loop.yaml\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(tmp_path / "loop.yaml")
    assert deep_merge({"a": {"b": 1}}, {"a": 2}) == {"a": 2}


# ---------- RIS／BibTeX ----------

def test_split_name():
    assert split_name("Lee, Ann") == ("Lee", "Ann")
    assert split_name("Kai Wu") == ("Wu", "Kai")
    assert split_name("Plato") == ("Plato", "")


def test_ris_and_bibtex_content():
    ris = to_ris(paper(), R)
    assert ris.startswith("TY  - JOUR\nTI  - ChatGPT")
    assert "AU  - Lee, Ann\nAU  - Wu, Kai" in ris and "DA  - 2026/10/08" in ris and "DO  - 10.1/a" in ris
    assert "KW  - consumer_behavior" in ris and ris.endswith("ER  - \n")
    bib = to_bibtex(paper(), R)
    assert bib.startswith("@article{2026_Lee_ChatGPT_hotel_guest_trust_100_new,")
    assert "author = {Lee, Ann and Wu, Kai}" in bib
    assert r"title = {ChatGPT \& hotel guest trust: 100\% new}" in bib, "BibTeX 特殊字元要跳脫"


def test_export_writes_daily_and_cumulative_files(tmp_path):
    notes = export({}, tmp_path, TOPIC, [(paper(), R, None)])
    export({}, tmp_path, TOPIC, [(paper(doi="10.1/b", title="Second paper about tourism"), R, None)])
    daily = tmp_path / "exports" / f"{date.today().isoformat()}_genai.ris"
    assert daily.read_text(encoding="utf-8").count("TY  - JOUR") == 2, "同一天重跑時追加"
    assert (tmp_path / "exports" / "genai_all.bib").read_text(encoding="utf-8").count("@article") == 2
    assert "已匯出 1 篇" in notes[0]
    assert export({}, tmp_path, TOPIC, []) == []


# ---------- Zotero ----------

@pytest.fixture
def zotero_api(monkeypatch):
    posted = Router()
    created = iter(f"COLL{i}" for i in range(10))

    def post(url, json=None, **kw):
        posted(url, json=json)
        if url.endswith("/collections"):
            return FakeResponse({"successful": {"0": {"key": next(created), "data": json[0]}}})
        if json and json[0].get("itemType") == "note":
            return FakeResponse({"successful": {str(i): {"key": f"N{i}"} for i in range(len(json))}, "failed": {}})
        return FakeResponse({"successful": {str(i): {"key": f"ITEM{i}"} for i in range(len(json))}, "failed": {}})
    existing = [{"key": "ROOT", "data": {"name": "論文追蹤", "parentCollection": False}}]
    monkeypatch.setattr("requests.get", Router({"/collections": FakeResponse(existing)}))
    monkeypatch.setattr("requests.post", post)
    monkeypatch.setenv("ZOTERO_API_KEY", "zk")
    return posted


def test_zotero_push_creates_collections_items_and_notes(zotero_api, tmp_path):
    analysis = tmp_path / "a.md"
    analysis.write_text("# 標題\n## 一句話總結\n信任 <很> 重要 [p.5]\n- 重點", encoding="utf-8")
    notes = export({"zotero": {"enabled": True, "library_id": "123"}}, tmp_path, TOPIC,
                   [(paper(), R, analysis), (paper(doi="10.1/b", title="Another tourism paper"), R, None)])
    urls = [u for u, _ in zotero_api.calls]
    assert urls[0] == "https://api.zotero.org/users/123/collections", "已有的根資料夾沿用，只建主題與分類"
    assert zotero_api.payloads[0][0] == {"name": "生成式 AI 觀光", "parentCollection": "ROOT"}
    assert zotero_api.payloads[1][0] == {"name": "consumer_behavior", "parentCollection": "COLL0"}
    items = zotero_api.payloads[2]
    assert len(items) == 2 and items[0]["itemType"] == "journalArticle" and items[0]["DOI"] == "10.1/a"
    assert items[0]["creators"][0] == {"creatorType": "author", "lastName": "Lee", "firstName": "Ann"}
    assert items[0]["collections"] == ["COLL1"]
    [note] = zotero_api.payloads[3]
    assert note["parentItem"] == "ITEM0" and "&lt;很&gt;" in note["note"] and "<h2>一句話總結</h2>" in note["note"]
    assert "Zotero 同步：新增 2 篇" in notes[1]


def test_zotero_requires_key(monkeypatch, tmp_path):
    monkeypatch.delenv("ZOTERO_API_KEY", raising=False)
    notes = export({"zotero": {"enabled": True, "library_id": "1"}}, tmp_path, TOPIC, [(paper(), R, None)])
    assert "未設定 ZOTERO_API_KEY" in notes[1]


def test_zotero_group_library_and_failure_is_reported(monkeypatch, tmp_path):
    monkeypatch.setenv("ZOTERO_API_KEY", "zk")
    monkeypatch.setattr("requests.get", Router({"": FakeResponse(status=403)}))
    notes = export({"zotero": {"enabled": True, "library_type": "group", "library_id": "9"}}, tmp_path, TOPIC,
                   [(paper(), R, None)])
    assert "Zotero 同步失敗" in notes[1]
    assert Zotero({"library_type": "group", "library_id": "9"}, "k").base.endswith("/groups/9")


def test_markdown_to_html():
    assert markdown_to_html("**粗體** <x>") == "<p><b>粗體</b> &lt;x&gt;</p>"


# ---------- 推播 ----------

ITEMS = [{"title": "Paper A", "venue": "TM", "relevance": 5, "tldr": "摘要 A", "url": "https://a"}]


def test_summary_text():
    text = notify.summary("快報", ITEMS, warning=True)
    assert "⚠ AI 服務連續失敗" in text and "1. Paper A（TM） 相關性 5/5" in text and "https://a" in text


def test_line_and_webhook_payloads(monkeypatch):
    posts = Router()
    monkeypatch.setattr("requests.post", posts)
    monkeypatch.setenv("LINE_CHANNEL_TOKEN", "t")
    monkeypatch.setenv("LINE_USER_ID", "U1")
    monkeypatch.setenv("NOTIFY_WEBHOOK_URL", "https://hooks/x")
    long_items = ITEMS * 200
    cfg = {"notify": {"line": {"enabled": True}, "webhook": {"enabled": True, "kind": "discord"}}}
    assert notify.send(cfg, "快報", "# md", long_items) == ["line", "webhook"]
    line, hook = posts.payloads
    assert line["to"] == "U1" and len(line["messages"][0]["text"]) <= notify.LINE_LIMIT
    assert len(hook["content"]) <= notify.DISCORD_LIMIT


def test_email(monkeypatch):
    sent = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            sent["server"] = (host, port)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self):
            sent["tls"] = True

        def login(self, user, password):
            sent["login"] = user

        def send_message(self, msg):
            sent["msg"] = msg
    monkeypatch.setattr("smtplib.SMTP", FakeSMTP)
    for k, v in {"SMTP_HOST": "smtp.x", "SMTP_USER": "me@x", "SMTP_PASSWORD": "p", "SMTP_PORT": ""}.items():
        monkeypatch.setenv(k, v)
    assert notify.send({"notify": {"email": {"enabled": True, "to": ["a@b"]}}}, "主旨", "內容", ITEMS) == ["email"]
    assert sent["server"] == ("smtp.x", 587) and sent["tls"] and sent["msg"]["To"] == "a@b"
    assert sent["msg"].get_content().strip() == "內容"


def test_notion_splits_blocks(monkeypatch):
    posts, patches = Router({"": FakeResponse({"id": "PAGE"})}), Router()
    monkeypatch.setattr("requests.post", posts)
    monkeypatch.setattr("requests.patch", patches)
    monkeypatch.setenv("NOTION_TOKEN", "n")
    md = "# 標題\n## 段落\n" + "\n".join(f"- 第 {i} 項" for i in range(150))
    cfg = {"notify": {"notion": {"enabled": True, "database_id": "DB", "title_property": "名稱"}}}
    assert notify.send(cfg, "快報", md, ITEMS) == ["notion"]
    page = posts.payloads[0]
    assert page["parent"] == {"database_id": "DB"} and "名稱" in page["properties"]
    assert len(page["children"]) == 100 and page["children"][0]["type"] == "heading_2"
    assert patches.calls[0][0].endswith("/blocks/PAGE/children") and len(patches.payloads[0]["children"]) == 51


def test_failed_channel_does_not_block_others(monkeypatch):
    monkeypatch.delenv("LINE_CHANNEL_TOKEN", raising=False)
    monkeypatch.setenv("NOTIFY_WEBHOOK_URL", "https://hooks/x")
    monkeypatch.setattr("requests.post", Router())
    cfg = {"notify": {"line": {"enabled": True}, "webhook": {"enabled": True}}}
    assert notify.send(cfg, "快報", "md", ITEMS) == ["webhook"]


def test_no_push_when_empty_unless_configured(monkeypatch):
    monkeypatch.setenv("NOTIFY_WEBHOOK_URL", "https://hooks/x")
    monkeypatch.setattr("requests.post", Router())
    assert notify.send({"notify": {"webhook": {"enabled": True}}}, "快報", "md", []) == []
    assert notify.send({"notify": {"on_empty": True, "webhook": {"enabled": True}}}, "快報", "md", []) == ["webhook"]


# ---------- 每週綜合報告 ----------

class WeeklyLLM(FakeLLM):
    def __init__(self, fail=False):
        super().__init__()
        self.fail = fail
        self.weekly_inputs = []

    def weekly_report(self, topic, entries):
        self.weekly_inputs.append(entries)
        return None if self.fail else "## 本週重點\n- 信任是關鍵"


def test_is_due():
    monday, tuesday = date(2026, 10, 12), date(2026, 10, 13)
    assert weekly.is_due({"weekly": {"enabled": True}}, monday)
    assert not weekly.is_due({"weekly": {"enabled": True}}, tuesday)
    assert weekly.is_due({"weekly": {"enabled": True, "weekday": "Tuesday"}}, tuesday)
    assert not weekly.is_due({}, monday)


def test_weekly_report_from_processed_papers(cfg, run_pipeline, monkeypatch):
    works = [openalex_work("ChatGPT hospitality and guest trust in hotels", "10.1/a"),
             openalex_work("Generative AI tourism destination marketing", "10.1/b")]
    router = Router({"api.openalex.org/works/doi:": FakeResponse({}),
                     "api.openalex.org/works": FakeResponse({"results": works})})
    run_pipeline(cfg, router, FakeLLM())
    llm = WeeklyLLM()
    [path] = weekly.generate(cfg, llm, force=True)
    text = path.read_text(encoding="utf-8")
    assert "每週綜合報告" in text and "## 本週重點" in text and "## 本週論文清單" in text
    entries = llm.weekly_inputs[0]
    assert len(entries) == 2 and "一句話總結：測試解析" in entries[0]["highlights"], "週報要用到解析重點"
    assert weekly.generate(cfg, llm) == [], "同一週不重複產生"


def test_weekly_failure_writes_nothing_and_empty_week_skips_llm(cfg, tmp_path):
    llm = WeeklyLLM()
    [path] = weekly.generate(cfg, llm)
    assert "本週沒有新收錄" in path.read_text(encoding="utf-8") and llm.weekly_inputs == []
    store = Store(Path(cfg["output_dir"]) / ".state" / "papers.db")
    store.save(TOPIC["name"], paper(), "c", 5, "t", None, None)
    cfg["topics"] = [TOPIC]
    assert weekly.generate(cfg, WeeklyLLM(fail=True), force=True) == []


def test_pipeline_generates_weekly_on_due_day(cfg, run_pipeline, monkeypatch):
    monkeypatch.setattr(weekly, "is_due", lambda c, d: True)
    cfg["weekly"] = {"enabled": True}
    router = Router({"api.openalex.org/works": FakeResponse({"results": []})})
    result = run_pipeline(cfg, router, WeeklyLLM())
    assert len(result.weekly) == 1 and result.weekly[0].parent.name == "weekly"
