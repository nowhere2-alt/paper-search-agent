"""主流程：搜尋 → 去重 → 分類評分 → 下載 → 深度解析 → 每日快報。"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from pathlib import Path

from .downloader import download_pdf
from .enrich import enrich
from .journals import Journal, load_tiers
from .llm import LLM
from .models import Paper
from .sources import SOURCES
from .store import Store

log = logging.getLogger(__name__)


MERGE_FIELDS = ("abstract", "pdf_url", "doi", "arxiv_id", "venue", "published")


def collect(topic: dict, since: date, limit: int, journals: list[Journal] | None = None) -> list[Paper]:
    """依 sources 的順序抓論文並跨資料庫去重。排越前面的來源是主要來源：
    同一篇論文以主要來源的書目資料為準，缺的欄位（摘要、PDF 連結等）才由後面的來源補上。
    journals 有值時，各來源只搜尋這些期刊。"""
    found: dict[str, Paper] = {}
    for name in topic.get("sources", list(SOURCES)):
        try:
            papers = SOURCES[name](topic["keywords"], since, limit, topic, journals)
        except Exception as e:  # 單一資料庫掛掉不影響其他來源
            log.error("[%s] 搜尋失敗：%s", name, e)
            continue
        log.info("[%s] 找到 %d 篇", name, len(papers))
        for p in papers:
            if not p.title:
                continue
            existing = found.get(p.uid)
            if existing is None:
                found[p.uid] = p
                continue
            for f in MERGE_FIELDS:
                if not getattr(existing, f) and getattr(p, f):
                    setattr(existing, f, getattr(p, f))
            existing.extra.setdefault("also_in", []).append(name)
    return list(found.values())


def search_passes(cfg: dict, topic: dict, root: Path) -> list[tuple[str, int, list[Journal] | None]]:
    """回傳依序要搜尋的範圍：(標籤, tier 編號, 期刊清單或 None=不限期刊)。"""
    path = Path(cfg.get("journal_list", "journals.yaml"))
    if not topic.get("use_journal_list", True) or not path.exists():
        return [("所有來源（不限期刊）", 0, None)]
    tiers = load_tiers(path, root / ".state" / "journals.json")
    wanted = topic.get("tiers")  # 例如 [1, 2]：只搜這幾個 tier
    passes = [(t.label, t.tier, t.journals) for t in tiers if not wanted or t.tier in wanted]
    if topic.get("open_search", False):
        passes.append(("開放搜尋（不限期刊）", 99, None))
    return passes


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
        min_rel = topic.get("min_relevance", cfg.get("min_relevance", 3))
        strategy = topic.get("tier_strategy", cfg.get("tier_strategy", "fallback"))
        enough = topic.get("tier_min_results", cfg.get("tier_min_results", 5))

        # 1) 依 tier 逐層搜尋 → 補資料 → 用摘要分類評分；fallback 策略下高相關論文夠多就停止往下一層
        scored: list[tuple[Paper, dict]] = []
        seen_run: set[str] = set()
        for label, tier, journals in search_passes(cfg, topic, root):
            log.info("--- %s ---", label)
            new = [p for p in collect(topic, since, limit, journals)
                   if p.uid not in seen_run and not store.seen(p.uid)]
            seen_run.update(p.uid for p in new)
            log.info("新論文 %d 篇", len(new))
            for p in new:
                enrich(p)  # WoS 等來源缺摘要／PDF 時，依 DOI 補齊
                p.extra.update(tier=tier, tier_label=label)
            if dry_run:
                for p in new:
                    print(f"- [T{tier}|{p.source}] {p.title} ({p.venue or ''} {p.published or ''}) "
                          f"摘要={'有' if p.abstract else '無'} PDF={'有' if p.pdf_url else '無'}")
                continue
            for p in new:
                result = llm.classify(p, topic)
                scored.append((p, result))
                log.info("  [T%s|%s|%s] %s", tier, result["category"], result["relevance"], p.title[:80])
            relevant = sum(r["relevance"] >= min_rel for _, r in scored)
            if strategy == "fallback" and relevant >= enough:
                log.info("已有 %d 篇高相關論文（門檻 %d），不再往下一層搜尋", relevant, enough)
                break
        if dry_run:
            continue

        # 2) 夠相關的才下載 + 深度解析（控制成本）；優先順序：tier 越前面越優先，同 tier 依相關性
        scored.sort(key=lambda x: (x[0].extra.get("tier", 0), -x[1]["relevance"]))
        items, analyzed = [], 0
        for p, r in scored:
            if r["relevance"] < min_rel:
                store.save(topic["name"], p, r["category"], r["relevance"], r["tldr"], None, None)
                continue
            cat_dir = topic_dir / r["category"]
            cat_dir.mkdir(parents=True, exist_ok=True)
            pdf = download_pdf(p, cat_dir)
            analysis_path = None
            if analyzed < max_analyze:
                text = llm.analyze(p, topic, pdf)
                analyzed += 1
                if text:
                    analysis_path = cat_dir / f"{p.slug}.md"
                    analysis_path.write_text(_header(p, r, pdf) + text, encoding="utf-8")
            store.save(topic["name"], p, r["category"], r["relevance"], r["tldr"],
                       str(pdf) if pdf else None, str(analysis_path) if analysis_path else None)
            items.append({"title": p.title, "url": p.url, "venue": p.venue or "",
                          "tier_label": p.extra.get("tier_label", ""), "category": r["category"],
                          "relevance": r["relevance"], "tldr": r["tldr"],
                          "analysis": analysis_path, "pdf": pdf})

        # 3) 每日快報
        digests.append(_write_digest(root, topic, items, llm))
    return digests


def _header(p: Paper, r: dict, pdf: Path | None) -> str:
    wos = ""
    if p.extra.get("wos_uid"):
        wos = f"- Web of Science：{p.extra['wos_uid']}　WoS 被引次數：{p.extra.get('times_cited') or 0}\n"
    return (
        f"# {p.title}\n\n"
        f"- 作者：{', '.join(p.authors[:10])}{' 等' if len(p.authors) > 10 else ''}\n"
        f"- 發表：{p.venue or ''} {p.published or p.year or ''}\n"
        f"- 連結：{p.url}\n"
        f"{wos}"
        f"{'- 期刊等級：' + p.extra['tier_label'] + chr(10) if p.extra.get('tier_label') else ''}"
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
        current = None
        for it in items:  # items 已依 tier 排序，按 tier 分段
            if it["tier_label"] != current:
                current = it["tier_label"]
                if current:
                    lines.append(f"\n### {current}\n")
            link = f"[解析]({Path('..') / it['analysis'].relative_to(root)})" if it["analysis"] else ""
            lines.append(f"- **[{it['title']}]({it['url']})**　*{it['venue']}*　`{it['category']}`　"
                         f"相關性 {it['relevance']}/5 {link}\n  {it['tldr']}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log.info("快報已寫入 %s", path)
    return path
