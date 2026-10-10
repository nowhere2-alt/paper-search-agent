"""每週綜合報告：把一週內處理過的高相關論文整理成研究趨勢、方法、研究缺口與精讀推薦。

自動產生：settings.yaml 的 weekly.enabled 為 true 時，每週在 weekly.weekday 那天的執行結束後產生。
手動產生：python -m paper_agent --weekly
報告存在 library/weekly/<年>-W<週>_<folder>.md，並依 notify 設定推播。
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from pathlib import Path

from . import notify
from .store import Store

log = logging.getLogger(__name__)
WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6}
HIGHLIGHT_SECTIONS = ("一句話總結", "主要發現", "與我的研究主題的關聯")
HIGHLIGHT_CHARS = 1200  # 每篇解析最多取多少字給週報，控制長度與費用


def is_due(cfg: dict, today: date) -> bool:
    wcfg = cfg.get("weekly", {})
    if not wcfg.get("enabled", False):
        return False
    day = wcfg.get("weekday", "monday")
    return today.weekday() == (WEEKDAYS[day.lower()] if isinstance(day, str) else int(day))


def report_path(root: Path, topic: dict, today: date) -> Path:
    year, week, _ = today.isocalendar()
    return root / "weekly" / f"{year}-W{week:02d}_{topic['folder']}.md"


def highlights(analysis_path: str | None) -> str:
    """從解析文章取出重點段落。"""
    if not analysis_path or not Path(analysis_path).exists():
        return ""
    text = Path(analysis_path).read_text(encoding="utf-8")
    parts = []
    for name in HIGHLIGHT_SECTIONS:
        m = re.search(rf"## {name}[^\n]*\n(.*?)(?=\n## |\Z)", text, re.S)
        if m and m.group(1).strip():
            parts.append(f"{name}：{' '.join(m.group(1).split())}")
    return "\n".join(parts)[:HIGHLIGHT_CHARS]


def generate(cfg: dict, llm, today: date | None = None, force: bool = False) -> list[Path]:
    today = today or date.today()
    root = Path(cfg.get("output_dir", "library"))
    store = Store(root / ".state" / "papers.db")
    since = datetime.combine(today - timedelta(days=7), datetime.min.time())
    written = []
    for topic in cfg["topics"]:
        path = report_path(root, topic, today)
        if path.exists() and not force:
            continue
        min_rel = topic.get("min_relevance", cfg.get("min_relevance", 3))
        entries = store.recent(topic["name"], since, min_rel)
        for e in entries:
            e["highlights"] = highlights(e.get("analysis_path"))
        title = f"{topic['name']}｜每週綜合報告（{since.date().isoformat()} ～ {today.isoformat()}）"
        lines = [f"# {title}\n"]
        if not entries:
            lines.append("本週沒有新收錄的高相關論文。")
        else:
            try:
                body = llm.weekly_report(topic, entries)
            except Exception as e:
                log.warning("每週報告產生失敗：%s", e)
                body = None
            if not body:
                log.warning("每週報告產生失敗，先不寫檔，下次執行再試：%s", topic["name"])
                continue
            lines += [body, "\n## 本週論文清單\n"]
            lines += [f"{i}. [{e['title']}]({e.get('url', '')})　*{e.get('venue') or ''}*　`{e['category']}`　"
                      f"相關性 {e['relevance']}/5\n   {e['tldr']}" for i, e in enumerate(entries, start=1)]
        path.parent.mkdir(parents=True, exist_ok=True)
        markdown = "\n".join(lines) + "\n"
        path.write_text(markdown, encoding="utf-8")
        log.info("每週報告已寫入 %s", path)
        notify.send(cfg, title, markdown, [{"title": e["title"], "venue": e.get("venue"), "url": e.get("url", ""),
                                            "relevance": e["relevance"], "tldr": e["tldr"]} for e in entries])
        written.append(path)
    return written
