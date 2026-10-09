"""共用資料結構。"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field


@dataclass
class Paper:
    title: str
    abstract: str
    authors: list[str]
    year: int | None
    source: str  # arxiv / semantic_scholar / openalex
    url: str  # 論文頁面
    pdf_url: str | None = None
    doi: str | None = None
    arxiv_id: str | None = None
    published: str | None = None  # ISO 日期
    venue: str | None = None
    extra: dict = field(default_factory=dict)

    @property
    def uid(self) -> str:
        """跨資料庫去重用的唯一鍵：優先 DOI，其次 arXiv ID，最後用正規化標題。"""
        if self.doi:
            return "doi:" + self.doi.lower().strip()
        if self.arxiv_id:
            return "arxiv:" + re.sub(r"v\d+$", "", self.arxiv_id)
        norm = re.sub(r"[^a-z0-9]", "", self.title.lower())
        return "title:" + hashlib.sha1(norm.encode()).hexdigest()[:16]

    @property
    def slug(self) -> str:
        """檔名：年份_第一作者姓氏_標題前幾個字。"""
        name = self.authors[0].strip() if self.authors and self.authors[0].strip() else "unknown"
        # WoS 格式為「姓, 名」；其他來源為「名 姓」
        first = name.split(",")[0].strip() if "," in name else name.split()[-1]
        words = re.sub(r"[^A-Za-z0-9 ]", " ", self.title).split()[:6]
        base = f"{self.year or 'nd'}_{first}_{'_'.join(words)}"
        return re.sub(r"[^A-Za-z0-9_\-]", "", base)[:120]
