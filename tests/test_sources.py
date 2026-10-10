"""各資料來源的查詢參數與回應解析。"""

from __future__ import annotations

from datetime import date

import pytest
from conftest import FakeResponse, Router

from paper_agent.journals import Journal, normalize
from paper_agent.models import Paper, SourceUnavailable
from paper_agent.sources import crossref, openalex, rss, sciencedirect, wos

TM = Journal("Tourism Management", "Elsevier", 1, issns=["0261-5177"])
JST = Journal("Journal of Sustainable Tourism", "Taylor & Francis", 1, issns=["0966-9582"],
              rss="https://www.tandfonline.com/feed/rss/rsus20")
KW = ["generative AI tourism", "ChatGPT hospitality", "AI chatbot hotel"]


def test_paper_slug_handles_surname_first_names():
    p = Paper(title="LLM Travel Agents!", abstract="", authors=["Lee, Ann"], year=2026, source="wos", url="")
    assert p.slug == "2026_Lee_LLM_Travel_Agents"
    q = Paper(title="X", abstract="", authors=["Ann Lee"], year=None, source="s", url="")
    assert q.slug == "nd_Lee_X"


def test_title_key_ignores_case_and_punctuation_but_not_short_titles():
    a = Paper(title="ChatGPT and Hotel Guest Trust!", abstract="", authors=[], year=None, source="", url="")
    b = Paper(title="chatgpt and hotel guest trust", abstract="", authors=[], year=None, source="", url="")
    assert a.title_key == b.title_key is not None
    assert Paper(title="Editorial", abstract="", authors=[], year=None, source="", url="").title_key is None


def test_wos_query_restricts_journals_and_years():
    no_issn = Journal("Tourism Economics", "SAGE", 1)
    q = wos.build_query(KW, date(2026, 10, 1), {}, [TM, JST, no_issn])
    assert q.startswith('TS=("generative AI tourism" OR "ChatGPT hospitality"')
    assert '(IS=(0261-5177 OR 0966-9582) OR SO=("TOURISM ECONOMICS"))' in q
    by_name = wos.build_query(KW, date(2026, 10, 1), {}, [TM, JST], use_issn=False)
    assert 'SO=("TOURISM MANAGEMENT" OR "JOURNAL OF SUSTAINABLE TOURISM")' in by_name
    assert "PY=(2026-" in q
    assert wos.build_query(KW, date(2026, 10, 1), {"wos_query": "TS=x"}) == "TS=x"


def test_wos_falls_back_to_journal_names_when_issn_rejected(monkeypatch):
    monkeypatch.setenv("WOS_API_KEY", "k")
    def respond(url, params):
        if "IS=(" in params["q"]:
            return FakeResponse(status=400)
        return FakeResponse({"metadata": {"total": 0}, "hits": []})
    router = Router({"wos-starter": respond})
    monkeypatch.setattr("requests.get", router)
    assert wos.search(KW, date(2026, 10, 1), 10, {}, [TM]) == []
    assert ["IS=(" in p["q"] for _, p in router.calls] == [True, False]


def test_wos_without_key_is_unavailable():
    with pytest.raises(SourceUnavailable):
        wos.search(KW, date(2026, 10, 1), 10, {})


def test_wos_parses_hits(monkeypatch):
    monkeypatch.setenv("WOS_API_KEY", "k")
    hit = {"uid": "WOS:1", "title": "ChatGPT hospitality trust",
           "source": {"sourceTitle": "TOURISM MANAGEMENT", "publishYear": 2026, "publishMonth": "OCT"},
           "names": {"authors": [{"displayName": "Lee, Ann"}]}, "identifiers": {"doi": "10.1/ABC"},
           "links": {"record": "https://wos/1"}, "citations": [{"db": "WOS", "count": 7}],
           "keywords": {"authorKeywords": ["trust"]}}
    router = Router({"wos-starter": FakeResponse({"metadata": {"total": 1}, "hits": [hit]})})
    monkeypatch.setattr("requests.get", router)
    [p] = wos.search(KW, date(2026, 10, 1), 10, {})
    assert (p.doi, p.venue, p.extra["times_cited"], p.extra["author_keywords"]) == ("10.1/ABC", "TOURISM MANAGEMENT", 7, ["trust"])
    assert router.calls[0][1]["sortField"] == "LD+D"


def test_openalex_rebuilds_abstract_and_filters_by_issn(monkeypatch):
    work = {"title": "T", "doi": "https://doi.org/10.1/x", "abstract_inverted_index": {"We": [0], "study": [1]},
            "authorships": [], "publication_year": 2026, "id": "W1", "best_oa_location": {"pdf_url": "https://p"},
            "primary_location": {}, "publication_date": "2026-10-01"}
    router = Router({"api.openalex.org/works": FakeResponse({"results": [work]})})
    monkeypatch.setattr("requests.get", router)
    [p] = openalex.search(KW, date(2026, 10, 1), 10, {}, [TM])
    assert p.abstract == "We study" and p.doi == "10.1/x" and p.pdf_url == "https://p"
    assert "primary_location.source.issn:0261-5177" in router.calls[0][1]["filter"]


def test_crossref_keyword_match_requires_all_words_of_one_keyword():
    assert crossref.matches("Generative AI and tourism experiences", ["generative AI tourism"])
    assert not crossref.matches("Wine tourism landscapes", ["generative AI tourism"])


def test_crossref_failure_reports_missing_journals(monkeypatch):
    def respond(url, params):
        return FakeResponse(status=503) if "0966-9582" in url else FakeResponse({"message": {"items": [
            {"DOI": "10.1/a", "title": ["ChatGPT hospitality service"], "author": [{"given": "Bo", "family": "Chen"}],
             "created": {"date-parts": [[2026, 10, 8]]}, "abstract": "<jats:title>Abstract</jats:title><jats:p>We study trust.</jats:p>"},
            {"DOI": "10.1/b", "title": ["Wine tourism"], "created": {"date-parts": [[2026, 10, 8]]}}]}})
    monkeypatch.setattr("requests.get", Router({"api.crossref.org": respond}))
    with pytest.raises(SourceUnavailable) as e:
        crossref.search(KW, date(2026, 10, 1), 10, {}, [TM, JST])
    assert [j.name for j in e.value.journals] == ["Journal of Sustainable Tourism"]
    assert [p.title for p in e.value.partial] == ["ChatGPT hospitality service"]
    assert e.value.partial[0].abstract == "We study trust."


def test_sciencedirect_only_for_elsevier_and_needs_key():
    assert sciencedirect.search(KW, date(2026, 10, 1), 10, {}, [JST]) == []
    with pytest.raises(SourceUnavailable) as e:
        sciencedirect.search(KW, date(2026, 10, 1), 10, {}, [TM, JST])
    assert e.value.journals == [TM]


RSS2 = """<?xml version="1.0"?><rss version="2.0"><channel><title>TM</title>
<item><title><![CDATA[Generative AI tourism and <i>trust</i>]]></title><link>https://sd/1</link>
<description><![CDATA[<p>Author(s): Ann Lee</p>]]></description><pubDate>Thu, 08 Oct 2026 00:00:00 GMT</pubDate></item>
<item><title>Old generative AI tourism paper</title><link>https://sd/old</link><pubDate>Mon, 01 Jan 2024 00:00:00 GMT</pubDate></item>
<item><title>Wine tourism landscapes</title><link>https://sd/wine</link><pubDate>Thu, 08 Oct 2026 00:00:00 GMT</pubDate></item>
</channel></rss>"""
RDF = """<?xml version="1.0"?><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns="http://purl.org/rss/1.0/"
 xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:prism="http://prismstandard.org/namespaces/basic/2.0/">
<item rdf:about="x"><title>ChatGPT hospitality service encounters</title><link>https://tf/doi/abs/10.1080/1</link>
<description>Abstract.</description><dc:creator>Bo Chen</dc:creator><dc:date>2026-10-07T00:00:00Z</dc:date>
<prism:doi>10.1080/09669582.2026.123</prism:doi></item></rdf:RDF>"""
ATOM = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry><title>AI chatbot hotel recovery</title>
<link href="https://doi.org/10.1234/atom1"/><summary>s</summary><updated>2026-10-06T10:00:00Z</updated></entry></feed>"""


@pytest.mark.parametrize("feed,expected", [
    (RSS2, ("Generative AI tourism and trust", None, "2026-10-08")),
    (RDF, ("ChatGPT hospitality service encounters", "10.1080/09669582.2026.123", "2026-10-07")),
    (ATOM, ("AI chatbot hotel recovery", "10.1234/atom1", "2026-10-06")),
])
def test_rss_formats(monkeypatch, feed, expected):
    monkeypatch.setattr("requests.get", Router({"": FakeResponse(text=feed)}))
    j = Journal("J", "Other", 1, rss="https://feed")
    [p] = rss.search(KW, date(2026, 10, 1), 10, {}, [j])  # 舊文章與不符關鍵字的都被濾掉
    assert (p.title, p.doi, p.published) == expected


def test_rss_feed_urls_and_cache(monkeypatch):
    assert rss.feed_urls(TM) == ["https://rss.sciencedirect.com/publication/science/02615177"]
    assert rss.feed_urls(Journal("IJTR", "Wiley", 2, issns=["1522-1970"])) == \
        ["https://onlinelibrary.wiley.com/feed/15221970/most-recent"]
    assert rss.feed_urls(Journal("TE", "SAGE", 2, issns=["1354-8166"])) == []
    router = Router({"": FakeResponse(text=RSS2)})
    monkeypatch.setattr("requests.get", router)
    rss.search(KW, date(2026, 10, 1), 10, {}, [TM])
    rss.search(KW, date(2026, 10, 1), 10, {}, [TM])
    assert len(router.calls) == 1, "同一次執行中同一個 RSS 只下載一次"


def test_rss_bad_feed_does_not_crash(monkeypatch):
    monkeypatch.setattr("requests.get", Router({"": FakeResponse(text="<html>not xml")}))
    assert rss.search(KW, date(2026, 10, 1), 10, {}, [Journal("J", "x", 1, rss="https://feed")]) == []


def test_journal_name_normalization():
    assert normalize("Journal of Hospitality & Tourism Research") == normalize("journal of hospitality and tourism research")
    assert normalize("The Leisure Studies") == "leisure studies"


def test_journal_list_dedupes_caches_and_respects_manual_issn(tmp_path, monkeypatch):
    from paper_agent.journals import load_tiers
    (tmp_path / "j.yaml").write_text("""tiers:
  - tier: 1
    label: T1
    journals:
      - {name: Tourism Management, publisher: Elsevier}
      - {name: Manual Journal, publisher: SAGE, issn: "1111-2222", rss: "https://feed"}
  - tier: 2
    label: T2
    journals:
      - {name: Tourism Management, publisher: Elsevier}
""", encoding="utf-8")
    router = Router({"api.openalex.org/sources": FakeResponse(
        {"results": [{"display_name": "Tourism Management", "issn": ["0261-5177"]}]})})
    monkeypatch.setattr("requests.get", router)
    cache = tmp_path / "cache.json"
    t1, t2 = load_tiers(tmp_path / "j.yaml", cache)
    assert [j.name for j in t1.journals] == ["Tourism Management", "Manual Journal"]
    assert t2.journals == [], "清單中重複的期刊只保留第一次出現"
    assert t1.journals[0].issns == ["0261-5177"] and t1.journals[1].issns == ["1111-2222"]
    assert t1.journals[1].rss == "https://feed"
    load_tiers(tmp_path / "j.yaml", cache)
    assert router.count("openalex") == 1, "第二次讀取用快取，不再查詢"
