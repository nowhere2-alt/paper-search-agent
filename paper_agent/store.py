"""用 SQLite 記錄處理過的論文，避免每天重複下載與重複分析。"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from .models import Paper

SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
    uid TEXT PRIMARY KEY,
    topic TEXT,
    title TEXT,
    category TEXT,
    relevance INTEGER,
    tldr TEXT,
    pdf_path TEXT,
    analysis_path TEXT,
    meta TEXT,
    processed_at TEXT
);
"""


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute(SCHEMA)

    def seen(self, uid: str) -> bool:
        return self.db.execute("SELECT 1 FROM papers WHERE uid = ?", (uid,)).fetchone() is not None

    def save(self, topic: str, paper: Paper, category: str, relevance: int, tldr: str,
             pdf_path: str | None, analysis_path: str | None) -> None:
        meta = {k: getattr(paper, k) for k in ("url", "authors", "year", "venue", "source", "doi")}
        meta["tier"] = paper.extra.get("tier")
        self.db.execute(
            "INSERT OR REPLACE INTO papers VALUES (?,?,?,?,?,?,?,?,?,?)",
            (paper.uid, topic, paper.title, category, relevance, tldr, pdf_path,
             analysis_path, json.dumps(meta, ensure_ascii=False), datetime.now().isoformat()),
        )
        self.db.commit()
