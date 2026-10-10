"""匯出高相關論文：RIS／BibTeX 檔（Zotero、EndNote、Mendeley 都能匯入），以及選用的 Zotero 直接同步。

- library/exports/<日期>_<folder>.ris / .bib：當天新增的論文，匯入一次即可，不會重複。
- library/exports/<folder>_all.ris / .bib：累積的全部論文。
- Zotero 同步（settings.yaml 的 zotero 區塊 + 環境變數 ZOTERO_API_KEY）：論文直接加進你的 Zotero，
  放在「<collection>/<主題>/<分類>」資料夾，深度解析附成子筆記。
"""

from __future__ import annotations

import html
import logging
import os
import re
from datetime import date
from pathlib import Path

import requests

from .models import Paper

log = logging.getLogger(__name__)
ZOTERO_API = "https://api.zotero.org"


def split_name(name: str) -> tuple[str, str]:
    """回傳 (姓, 名)。支援「姓, 名」（WoS）與「名 姓」兩種寫法。"""
    name = name.strip()
    if "," in name:
        last, first = name.split(",", 1)
        return last.strip(), first.strip()
    parts = name.split()
    return (parts[-1], " ".join(parts[:-1])) if parts else ("", "")


def _date(p: Paper) -> str:
    return p.published or (str(p.year) if p.year else "")


# ---------- RIS ----------

def to_ris(p: Paper, r: dict) -> str:
    lines = ["TY  - JOUR", f"TI  - {p.title}"]
    lines += [f"AU  - {last}, {first}".rstrip(", ") for last, first in map(split_name, p.authors)]
    if p.year:
        lines.append(f"PY  - {p.year}")
    if p.published and re.match(r"\d{4}-\d{2}-\d{2}", p.published):
        lines.append(f"DA  - {p.published.replace('-', '/')}")
    for tag, value in (("JO", p.venue), ("DO", p.doi), ("UR", p.url),
                       ("AB", " ".join(p.abstract.split()) if p.abstract else "")):
        if value:
            lines.append(f"{tag}  - {value}")
    lines += [f"KW  - {r['category']}", f"N1  - 相關性 {r['relevance']}/5：{r['tldr']}", "ER  - "]
    return "\n".join(lines) + "\n"


# ---------- BibTeX ----------

def _bib(value: str) -> str:
    value = " ".join(str(value).split())
    for ch in "\\{}":
        value = value.replace(ch, "")
    return re.sub(r"([&%$#_])", r"\\\1", value)


def to_bibtex(p: Paper, r: dict) -> str:
    fields = {
        "title": p.title,
        "author": " and ".join(f"{last}, {first}".rstrip(", ") for last, first in map(split_name, p.authors)),
        "journal": p.venue or "", "year": str(p.year or ""), "doi": p.doi or "", "url": p.url,
        "abstract": p.abstract, "keywords": r["category"], "note": f"相關性 {r['relevance']}/5：{r['tldr']}",
    }
    body = ",\n".join(f"  {k} = {{{_bib(v)}}}" for k, v in fields.items() if v)
    return f"@article{{{p.slug},\n{body}\n}}\n"


def write_files(root: Path, topic: dict, entries: list[tuple[Paper, dict, Path | None]]) -> list[Path]:
    if not entries:
        return []
    out = root / "exports"
    out.mkdir(parents=True, exist_ok=True)
    today, folder = date.today().isoformat(), topic["folder"]
    ris = "\n".join(to_ris(p, r) for p, r, _ in entries)
    bib = "\n".join(to_bibtex(p, r) for p, r, _ in entries)
    written = []
    for suffix, text in ((".ris", ris), (".bib", bib)):
        daily = out / f"{today}_{folder}{suffix}"
        with open(daily, "a", encoding="utf-8") as f:  # 同一天重跑時追加，不覆蓋早先的
            f.write(text)
        with open(out / f"{folder}_all{suffix}", "a", encoding="utf-8") as f:
            f.write(text)
        written.append(daily)
    return written


# ---------- Zotero Web API ----------

def markdown_to_html(text: str) -> str:
    """把解析文章轉成 Zotero 筆記可用的簡單 HTML。"""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        esc = html.escape(line)
        esc = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", esc)
        if line.startswith("## "):
            out.append(f"<h2>{esc[3:]}</h2>")
        elif line.startswith("# "):
            out.append(f"<h1>{esc[2:]}</h1>")
        elif line.startswith(("- ", "* ")):
            out.append(f"<p>• {esc[2:]}</p>")
        elif line == "---":
            out.append("<hr/>")
        else:
            out.append(f"<p>{esc}</p>")
    return "\n".join(out)


class Zotero:
    def __init__(self, zcfg: dict, api_key: str):
        kind = "groups" if zcfg.get("library_type", "user") == "group" else "users"
        self.base = f"{ZOTERO_API}/{kind}/{zcfg['library_id']}"
        self.headers = {"Zotero-API-Key": api_key, "Zotero-API-Version": "3"}
        self.root_name = zcfg.get("collection", "論文追蹤")
        self._collections: list[dict] | None = None

    def _get_collections(self) -> list[dict]:
        if self._collections is None:
            items, start = [], 0
            while True:
                resp = requests.get(f"{self.base}/collections", headers=self.headers,
                                    params={"limit": 100, "start": start}, timeout=30)
                resp.raise_for_status()
                page = resp.json()
                items += page
                if len(page) < 100:
                    break
                start += 100
            self._collections = items
        return self._collections

    def collection(self, name: str, parent: str | None) -> str:
        """找到或建立資料夾，回傳 key。"""
        for c in self._get_collections():
            data = c.get("data", {})
            if data.get("name") == name and (data.get("parentCollection") or None) == parent:
                return c["key"]
        resp = requests.post(f"{self.base}/collections", headers=self.headers, timeout=30,
                             json=[{"name": name, "parentCollection": parent or False}])
        resp.raise_for_status()
        created = resp.json()["successful"]["0"]
        self._collections.append(created)
        return created["key"]

    def push(self, topic: dict, entries: list[tuple[Paper, dict, Path | None]]) -> tuple[int, int]:
        """回傳 (成功筆數, 失敗筆數)。"""
        root = self.collection(self.root_name, None)
        topic_key = self.collection(topic["name"], root)
        ok = failed = 0
        for i in range(0, len(entries), 50):  # Zotero API 單次最多 50 筆
            chunk = entries[i:i + 50]
            items = []
            for p, r, _ in chunk:
                creators = [{"creatorType": "author", "lastName": last, "firstName": first}
                            for last, first in map(split_name, p.authors)]
                items.append({
                    "itemType": "journalArticle", "title": p.title, "creators": creators,
                    "abstractNote": p.abstract or "", "publicationTitle": p.venue or "", "date": _date(p),
                    "DOI": p.doi or "", "url": p.url or "",
                    "tags": [{"tag": r["category"]}, {"tag": "paper-agent"}],
                    "collections": [self.collection(r["category"], topic_key)],
                    "extra": f"相關性 {r['relevance']}/5：{r['tldr']}",
                })
            resp = requests.post(f"{self.base}/items", headers=self.headers, json=items, timeout=60)
            resp.raise_for_status()
            result = resp.json()
            failed += len(result.get("failed", {}))
            notes = []
            for idx, obj in result.get("successful", {}).items():
                ok += 1
                analysis = chunk[int(idx)][2]
                if analysis and analysis.exists():
                    notes.append({"itemType": "note", "parentItem": obj["key"], "tags": [],
                                  "note": markdown_to_html(analysis.read_text(encoding="utf-8"))})
            if notes:
                requests.post(f"{self.base}/items", headers=self.headers, json=notes, timeout=60).raise_for_status()
        return ok, failed


def export(cfg: dict, root: Path, topic: dict, entries: list[tuple[Paper, dict, Path | None]]) -> list[str]:
    """匯出並回傳要寫進快報的狀態說明。任何錯誤都不會中斷主流程。"""
    notes = []
    if not entries:
        return notes
    try:
        files = write_files(root, topic, entries)
        notes.append(f"已匯出 {len(entries)} 篇到 {', '.join(f.name for f in files)}（可匯入 Zotero／EndNote）")
    except OSError as e:
        log.warning("匯出 RIS／BibTeX 失敗：%s", e)
    zcfg = cfg.get("zotero", {})
    if zcfg.get("enabled"):
        key = os.environ.get("ZOTERO_API_KEY")
        if not key or not zcfg.get("library_id"):
            notes.append("Zotero 同步：未設定 ZOTERO_API_KEY 或 library_id，略過")
        else:
            try:
                ok, failed = Zotero(zcfg, key).push(topic, entries)
                notes.append(f"Zotero 同步：新增 {ok} 篇" + (f"，{failed} 篇失敗" if failed else ""))
            except requests.RequestException as e:
                log.warning("Zotero 同步失敗：%s", e)
                notes.append("Zotero 同步失敗（請檢查金鑰與 library_id），論文仍可用匯出檔手動匯入")
    return notes
