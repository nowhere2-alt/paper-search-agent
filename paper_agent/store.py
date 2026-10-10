"""用 SQLite 記錄處理過的論文，避免重複下載與重複分析。

- 紀錄依主題分開：同一篇論文在主題 A 處理過，主題 B 仍會評估。
- 以 DOI（或 arXiv ID）和正規化標題雙重比對：同一篇論文從有 DOI 和沒有 DOI 的來源進來，都認得出來。
- 處理失敗（AI 服務錯誤、逾時、拒答）的論文不標記為已處理，下次執行會重試；連續失敗 MAX_ATTEMPTS 次才放棄。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from .models import Paper

MAX_ATTEMPTS = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
    topic TEXT NOT NULL,
    uid TEXT NOT NULL,
    title_key TEXT,
    title TEXT,
    category TEXT,
    relevance INTEGER,
    tldr TEXT,
    pdf_path TEXT,
    analysis_path TEXT,
    meta TEXT,
    processed_at TEXT,
    PRIMARY KEY (topic, uid)
);
CREATE INDEX IF NOT EXISTS idx_papers_title ON papers (topic, title_key);
CREATE TABLE IF NOT EXISTS failures (
    topic TEXT NOT NULL,
    uid TEXT NOT NULL,
    title_key TEXT,
    attempts INTEGER NOT NULL,
    last_error TEXT,
    updated_at TEXT,
    PRIMARY KEY (topic, uid)
);
"""


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self._migrate()
        self.db.executescript(SCHEMA)

    def _migrate(self) -> None:
        """舊版資料庫（uid 為唯一主鍵、沒有分主題）轉成新格式，保留既有紀錄。"""
        cols = [r[1] for r in self.db.execute("PRAGMA table_info(papers)")]
        if not cols or "title_key" in cols:
            return
        self.db.execute("ALTER TABLE papers RENAME TO papers_v1")
        self.db.executescript(SCHEMA)
        for row in self.db.execute("SELECT uid, topic, title, category, relevance, tldr, pdf_path, "
                                   "analysis_path, meta, processed_at FROM papers_v1").fetchall():
            title_key = Paper(title=row[2] or "", abstract="", authors=[], year=None, source="", url="").title_key
            self.db.execute("INSERT OR IGNORE INTO papers VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            (row[1], row[0], title_key, *row[2:]))
        self.db.execute("DROP TABLE papers_v1")
        self.db.commit()

    def _match(self, table: str, topic: str, paper: Paper, extra: str = "") -> sqlite3.Row | None:
        sql = f"SELECT * FROM {table} WHERE topic = ? AND (uid = ? OR (title_key IS NOT NULL AND title_key = ?)){extra}"
        return self.db.execute(sql, (topic, paper.uid, paper.title_key)).fetchone()

    def seen(self, topic: str, paper: Paper) -> bool:
        return self._match("papers", topic, paper) is not None

    def gave_up(self, topic: str, paper: Paper) -> bool:
        return self._match("failures", topic, paper, f" AND attempts >= {MAX_ATTEMPTS}") is not None

    def record_failure(self, topic: str, paper: Paper, error: str) -> int:
        """記錄一次失敗，回傳累計次數。"""
        row = self._match("failures", topic, paper)
        attempts = (row[3] if row else 0) + 1
        self.db.execute("DELETE FROM failures WHERE topic = ? AND (uid = ? OR (title_key IS NOT NULL AND title_key = ?))",
                        (topic, paper.uid, paper.title_key))
        self.db.execute("INSERT INTO failures VALUES (?,?,?,?,?,?)",
                        (topic, paper.uid, paper.title_key, attempts, error[:500], datetime.now().isoformat()))
        self.db.commit()
        return attempts

    def save(self, topic: str, paper: Paper, category: str, relevance: int, tldr: str,
             pdf_path: str | None, analysis_path: str | None) -> None:
        meta = {k: getattr(paper, k) for k in ("url", "authors", "year", "venue", "source", "doi")}
        meta["tier"] = paper.extra.get("tier")
        self.db.execute(
            "INSERT OR REPLACE INTO papers VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (topic, paper.uid, paper.title_key, paper.title, category, relevance, tldr, pdf_path,
             analysis_path, json.dumps(meta, ensure_ascii=False), datetime.now().isoformat()),
        )
        self.db.execute("DELETE FROM failures WHERE topic = ? AND (uid = ? OR (title_key IS NOT NULL AND title_key = ?))",
                        (topic, paper.uid, paper.title_key))
        self.db.commit()
