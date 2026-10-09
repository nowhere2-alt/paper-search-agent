"""瀏覽器備援可以使用的資料庫網站。

key 是對應的 API 來源名稱：該 API 無法使用（沒有金鑰、額度用完、連線失敗）時，就改用這裡的網站。
crossref 沒有單一網站，改依期刊的 publisher 分別到各出版社網站搜尋。
allowed_hosts 以外的網址一律封鎖（學校 SSO 登入網域請在 config.yaml 的 browser.extra_allowed_hosts 加上）。
"""

SITES = {
    "wos": {
        "label": "Web of Science",
        "start_url": "https://www.webofscience.com/wos/woscc/advanced-search",
        "allowed_hosts": ["webofscience.com", "clarivate.com"],
        "needs_login": True,
        "tips": (
            "Use Advanced Search. Build a query such as "
            'TS=("keyword one" OR "keyword two") AND SO=("JOURNAL NAME" OR ...) AND PY=(2025-2026), '
            "run it, then sort results by date (newest first)."
        ),
    },
    "sciencedirect": {
        "label": "ScienceDirect (Elsevier)",
        "start_url": "https://www.sciencedirect.com/search",
        "allowed_hosts": ["sciencedirect.com", "elsevier.com"],
        "needs_login": False,
        "tips": (
            "Use the advanced search form (Title, abstract or author-specified keywords; Journal or book title; "
            "Year). Search one journal at a time if the journal field takes a single title. Sort by date."
        ),
    },
    "Taylor & Francis": {
        "label": "Taylor & Francis Online",
        "start_url": "https://www.tandfonline.com/search/advanced",
        "allowed_hosts": ["tandfonline.com"],
        "needs_login": False,
        "tips": "Use Advanced Search, restrict to the journal title, sort by newest.",
    },
    "SAGE": {
        "label": "SAGE Journals",
        "start_url": "https://journals.sagepub.com/search/advanced",
        "allowed_hosts": ["sagepub.com"],
        "needs_login": False,
        "tips": "Use Advanced Search, restrict to the journal, sort by newest.",
    },
    "Emerald": {
        "label": "Emerald Insight",
        "start_url": "https://www.emerald.com/insight/advanced-search",
        "allowed_hosts": ["emerald.com"],
        "needs_login": False,
        "tips": "Use Advanced Search, restrict to the journal title, sort by newest.",
    },
    "Wiley": {
        "label": "Wiley Online Library",
        "start_url": "https://onlinelibrary.wiley.com/search/advanced",
        "allowed_hosts": ["wiley.com"],
        "needs_login": False,
        "tips": "Use Advanced Search, restrict to the journal (Published in), sort by newest.",
    },
    "Springer": {
        "label": "SpringerLink",
        "start_url": "https://link.springer.com/advanced-search",
        "allowed_hosts": ["springer.com"],
        "needs_login": False,
        "tips": "Use Advanced Search, sort by newest, then filter by the journal.",
    },
}

# 這些 API 來源失敗時，要用哪些網站備援
FALLBACK_FOR = {
    "wos": "wos",
    "sciencedirect": "sciencedirect",
    "crossref": "publisher",  # 依期刊出版社分別搜尋
}
