"""學術資料庫連接器。新增資料庫：寫一個 search(keywords, since, limit, cfg, journals=None) -> list[Paper]，
再登記到 SOURCES。journals 有值時代表「只搜這些期刊」（期刊清單模式），不支援的來源回傳空清單即可。"""

from . import arxiv, crossref, openalex, sciencedirect, semantic_scholar, wos

SOURCES = {
    "wos": wos.search,
    "sciencedirect": sciencedirect.search,
    "crossref": crossref.search,
    "openalex": openalex.search,
    "semantic_scholar": semantic_scholar.search,
    "arxiv": arxiv.search,
}
