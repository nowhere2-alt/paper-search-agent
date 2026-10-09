"""學術資料庫連接器。新增資料庫：寫一個 search(keywords, since, limit, cfg) -> list[Paper]，再登記到 SOURCES。"""

from . import arxiv, openalex, semantic_scholar, wos

SOURCES = {
    "wos": wos.search,
    "openalex": openalex.search,
    "semantic_scholar": semantic_scholar.search,
    "arxiv": arxiv.search,
}
