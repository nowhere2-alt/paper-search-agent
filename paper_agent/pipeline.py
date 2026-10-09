"""主流程：搜尋 → 去重 → 分類評分 → 下載 → 深度解析 → 每日快報。"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from pathlib import Path

from .downloader import download_pdf
from .llm import LLM
from .models import Paper
from .sources import SOURCES
from .store import Store

log = logging.getLogger(__name__)


def collect(topic: dict, since: date, limit: int) -> list[Paper]:
    """從每個指定資料庫抓論文，並跨資料庫去重。"""
    found: dict[str, Paper] = {}
    for name in topic.get("sources", list(SOURCES)):
        try:
            papers = SOURCES[name](topic["keywords"], since, limit, topic)
        except Exception as e:  # 單一資料庫掛掉不影響其他來源
            log.error("[%s] 搜尋失敗：%s", name, e)
            continue
        log.info("[%s] 找到 %d 篇", name, len(papers))
        for p in papers:
            if not p.title:
                continue
            existing = found.get(p.uid)
            # 同一篇出現在多個資料庫時，保留有 PDF 連結的版本
            if existing is None or (not existing.pdf_url and p.pdf_url):
                found[p.uid] = p
    return list(found.values())


def run(cfg: dict, days: int | None = None, dry_run: bool = False) -> list[Path]:
    root = Path(cfg.get("output_dir", "library"))
    store = Store(root / ".state" / "papers.db")
    llm = LLM(cfg.get("claude", {}))
    lookback = days or cfg.get("lookback_days", 3)
    since = date.today() - timedelta(days=lookback)
    limit = cfg.get("max_results_per_source", 50)
    max_analyze = cfg.get("max_analyses_per_topic", 10)
    digests: list[Path] = []

    for topic in cfg["topics"]:
        topic_dir = root / topic["folder"]
        log.info("=== 主題：%s ===", topic["name"])
        new = [p for p in collect(topic, since, limit) if not store.seen(p.uid)]
        log.info("新論文 %d 篇", len(new))
        if dry_run:
            for p in new:
                print(f"- {p.title} ({p.source}, {p.published}) PDF={'有' if p.pdf_url else '無'}")
            continue

        # 1) 用摘要分類與評分
        scored = []
        for p in new:
            result = llm.classify(p, topic)
            scored.append((p, result))
            log.info("  [%s|%s] %s", result["category"], result["relevance"], p.title[:80])
        scored.sort(key=lambda x: x[1]["relevance"], reverse=True)

        # 2) 夠相關的才下載 + 深度解析（控制成本）
        min_rel = topic.get("min_relevance", cfg.get("min_relevance", 3))
        items = []
        for i, (p, r) in enumerate(scored):
            if r["relevance"] < min_rel:
                store.save(topic["name"], p, r["category"], r["relevance"], r["tldr"], None, None)
                continue
            cat_dir = topic_dir / r["category"]
            pdf = download_pdf(p, cat_dir)
            analysis_path = None
            if i < max_analyze:
                text = llm.analyze(p, topic, pdf)
                if text:
                    analysis_path = cat_dir / f"{p.slug}.md"
                    analysis_path.write_text(_header(p, r, pdf) + text, encoding="utf-8")
            store.save(topic["name"], p, r["category"], r["relevance"], r["tldr"],
                       str(pdf) if pdf else None, str(analysis_path) if analysis_path else None)
            items.append({"title": p.title, "url": p.url, "category": r["category"],
                          "relevance": r["relevance"], "tldr": r["tldr"],
                          "analysis": analysis_path, "pdf": pdf})

        # 3) 每日快報
        digests.append(_write_digest(root, topic, items, llm))
    return digests


def _header(p: Paper, r: dict, pdf: Path | None) -> str:
    return (
        f"# {p.title}\n\n"
        f"- 作者：{', '.join(p.authors[:10])}{' 等' if len(p.authors) > 10 else ''}\n"
        f"- 發表：{p.venue or ''} {p.published or p.year or ''}\n"
        f"- 連結：{p.url}\n"
        f"- 分類：{r['category']}　相關性：{r['relevance']}/5\n"
        f"- 全文：{'已下載 ' + pdf.name if pdf else '未取得（僅依摘要）'}\n\n---\n\n"
    )


def _write_digest(root: Path, topic: dict, items: list[dict], llm: LLM) -> Path:
    today = date.today().isoformat()
    path = root / "digests" / f"{today}_{topic['folder']}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not items and path.exists():
        return path  # 同一天重跑且沒有新論文時，保留早先的快報
    lines = [f"# {topic['name']}｜每日論文快報 {today}\n"]
    if not items:
        lines.append("今天沒有新的高相關論文。\n")
    else:
        overview = llm.daily_overview(topic, items)
        if overview:
            lines += ["## 今日研究動態\n", overview, ""]
        lines.append("## 論文清單\n")
        for it in items:
            link = f"[解析]({Path('..') / it['analysis'].relative_to(root)})" if it["analysis"] else ""
            lines.append(f"- **[{it['title']}]({it['url']})**　`{it['category']}`　"
                         f"相關性 {it['relevance']}/5 {link}\n  {it['tldr']}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log.info("快報已寫入 %s", path)
    return path
