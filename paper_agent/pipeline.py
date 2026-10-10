"""主流程：搜尋 → 去重 → 分類評分 → 下載 → 深度解析 → 每日快報。"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import date, timedelta
from pathlib import Path

from . import notify, weekly
from .downloader import download_pdf
from .export import export
from .enrich import enrich
from .journals import Journal, load_tiers
from .llm import make_llm
from .browser import browser_fallback
from .models import Paper, SourceStatus, SourceUnavailable
from .sources import SOURCES
from .store import MAX_ATTEMPTS, Store

log = logging.getLogger(__name__)


MERGE_FIELDS = ("abstract", "pdf_url", "doi", "arxiv_id", "venue", "published")


def collect(topic: dict, since: date, limit: int, journals: list[Journal] | None = None,
            cfg: dict | None = None, use_browser: bool = True,
            status: dict[str, SourceStatus] | None = None) -> list[Paper]:
    """依 sources 的順序抓論文並跨資料庫去重。排越前面的來源是主要來源：
    同一篇論文以主要來源的書目資料為準，缺的欄位（摘要、PDF 連結等）才由後面的來源補上。
    journals 有值時，各來源只搜尋這些期刊。API 無法使用時，改由 Claude 操作瀏覽器到該資料庫網站搜尋。"""
    cfg = cfg or {}
    status = {} if status is None else status
    found: dict[str, Paper] = {}
    by_title: dict[str, Paper] = {}
    for name in topic.get("sources", list(SOURCES)):
        st = status.setdefault(name, SourceStatus())
        try:
            papers = SOURCES[name](topic["keywords"], since, limit, topic, journals)
        except Exception as e:  # 單一資料庫掛掉不影響其他來源
            missing = e.journals if isinstance(e, SourceUnavailable) and e.journals else journals
            papers = list(e.partial) if isinstance(e, SourceUnavailable) else []
            log.warning("[%s] API 無法使用：%s", name, e)
            st.add(st.errors, _short_error(e))
            # 備援：瀏覽器（dry-run 時不啟動）→ 被擋或無法使用時改讀期刊 RSS
            papers += browser_fallback(name, topic["keywords"], missing, since, limit, cfg,
                                       allow_browser=use_browser, notes=st.notes)
        st.found += len(papers)
        log.info("[%s] 找到 %d 篇", name, len(papers))
        for p in papers:
            if not p.title:
                continue
            # 先比 DOI／arXiv ID，再比標題：有的來源沒有 DOI（例如部分 RSS），靠標題認出同一篇
            existing = found.get(p.uid) or (by_title.get(p.title_key) if p.title_key else None)
            if existing is None:
                found[p.uid] = p
                if p.title_key:
                    by_title[p.title_key] = p
                continue
            for f in MERGE_FIELDS:
                if not getattr(existing, f) and getattr(p, f):
                    setattr(existing, f, getattr(p, f))
            existing.extra.setdefault("also_in", []).append(name)
    return list(found.values())


def _short_error(e: Exception) -> str:
    """快報用的簡短錯誤說明（不放網址與完整堆疊）。"""
    if isinstance(e, SourceUnavailable):
        return str(e)
    text = str(e)
    for marker, label in (("ProxyError", "連線被拒"), ("ConnectionError", "連線失敗"), ("Timeout", "逾時"),
                          ("401", "金鑰無效（401）"), ("403", "沒有權限（403）"), ("429", "超過額度或太頻繁（429）")):
        if marker in text or marker in type(e).__name__:
            return label
    return f"{type(e).__name__}"


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


class Breaker:
    """AI 服務連續失敗時（例如金鑰失效、額度用完、Codex 未登入）停止本次處理。
    這類失敗不是論文本身的問題，所以不計入各篇論文的失敗次數，下次執行全部重試。"""

    LIMIT = 3

    def __init__(self):
        self.streak = 0
        self.pending: list[tuple[Paper, str]] = []  # 尚未寫入的失敗紀錄

    @property
    def tripped(self) -> bool:
        return self.streak >= self.LIMIT

    def ok(self, store: Store, topic: str) -> None:
        self.streak = 0
        self.flush(store, topic)

    def fail(self, paper: Paper, error: str) -> None:
        self.streak += 1
        self.pending.append((paper, error))

    def flush(self, store: Store, topic: str) -> dict[str, int]:
        """把個別失敗寫入紀錄；斷路器跳開時，連續失敗的那幾筆視為系統問題而丟棄。回傳 uid → 累計次數。"""
        if self.tripped:
            self.pending = self.pending[:-self.streak]
        counts = {p.uid: store.record_failure(topic, p, err) for p, err in self.pending}
        self.pending = []
        return counts


class Digests(list):
    """run() 的結果：快報路徑清單；ai_failed 表示有主題因 AI 服務連續失敗而中止。"""

    ai_failed: bool = False
    weekly: list[Path] = []


def _try(fn, *args):
    """呼叫 AI；出錯時回傳 (None, 錯誤訊息)，不讓單篇論文中斷整次執行。"""
    try:
        return fn(*args), ""
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def _classify_iter(llm, papers: list[Paper], topic: dict):
    """逐篇（或以批次）分類，依序回傳 (論文, 結果或 None, 錯誤訊息)。"""
    if hasattr(llm, "classify_batch"):
        yield from llm.classify_batch(papers, topic)
    else:
        for p in papers:
            yield (p, *_try(llm.classify, p, topic))


def _analyze_iter(llm, jobs: list[tuple[Paper, Path | None]], topic: dict):
    if hasattr(llm, "analyze_batch"):
        yield from llm.analyze_batch(jobs, topic)
    else:
        for p, pdf in jobs:
            yield (p, *_try(llm.analyze, p, topic, pdf))


def tier_window(store: Store, topic: str, tier: int, today: date, lookback: int, cfg: dict,
                topic_cfg: dict) -> date | None:
    """這一層今天要不要搜、從哪天開始搜。回傳 None 表示今天跳過。

    - tier_every_days：每幾天搜一次（例如 Tier 3 每 7 天），省下較不重要期刊的分類費用。
    - 搜尋起點至少涵蓋 lookback_days，並往前補到上次搜尋這一層的日期：
      隔幾天才搜、電腦關機幾天、或 fallback 策略這幾天沒搜到這一層，都不會漏掉中間的論文。
    """
    every = {int(k): v for k, v in {**cfg.get("tier_every_days", {}),
                                    **topic_cfg.get("tier_every_days", {})}.items()}.get(tier, 1)
    last = store.last_tier_run(topic, tier)
    if every > 1 and last and (today - last).days < every:
        return None  # 每天搜的層級不跳過：同一天重跑（例如修好設定後）仍會照常搜尋
    since = today - timedelta(days=lookback)
    if last:
        since = min(since, last - timedelta(days=1))  # 與上次重疊一天，避免邊界漏掉
    return max(since, today - timedelta(days=cfg.get("max_lookback_days", 60)))


def effective_keywords(cfg: dict, topic: dict, root: Path, llm) -> list[str]:
    """keyword_expansion 開啟時，由 AI 把關鍵字擴充成同義詞清單（保留原本的關鍵字）。

    結果存在 library/.state/keywords/<folder>.json，可以直接編輯；只有修改 config 的
    keywords 或 description 時才會重新產生。dry-run 或 AI 失敗時用原本的關鍵字。"""
    original = topic["keywords"]
    if not topic.get("keyword_expansion", cfg.get("keyword_expansion", False)):
        return original
    path = root / ".state" / "keywords" / f"{topic['folder']}.json"
    source = hashlib.sha1(json.dumps([original, topic.get("description", "")], ensure_ascii=False)
                          .encode()).hexdigest()[:12]
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("source") == source:
            return data["keywords"]
    if llm is None:
        return original
    expanded, err = _try(llm.expand_keywords, topic)
    if not expanded:
        log.warning("關鍵字擴充失敗，改用原本的關鍵字：%s", err)
        return original
    keywords = list(dict.fromkeys([*original, *(k.strip() for k in expanded if k.strip())]))[:20]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "說明": "AI 擴充的檢索詞，可以直接編輯。修改 config 的 keywords 或 description 後會重新產生。",
        "source": source, "keywords": keywords}, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("關鍵字已擴充為 %d 個：%s", len(keywords), "、".join(keywords))
    return keywords


def run(cfg: dict, days: int | None = None, dry_run: bool = False) -> list[Path]:
    root = Path(cfg.get("output_dir", "library"))
    store = Store(root / ".state" / "papers.db")
    llm = None if dry_run else make_llm(cfg)  # engine: claude 或 codex；dry-run 不需要
    lookback = days or cfg.get("lookback_days", 3)
    today = date.today()
    limit = cfg.get("max_results_per_source", 50)
    max_analyze = cfg.get("max_analyses_per_topic", 10)
    digests = Digests()

    for topic in cfg["topics"]:
        name = topic["name"]
        topic_dir = root / topic["folder"]
        log.info("=== 主題：%s ===", name)
        min_rel = topic.get("min_relevance", cfg.get("min_relevance", 3))
        strategy = topic.get("tier_strategy", cfg.get("tier_strategy", "fallback"))
        enough = topic.get("tier_min_results", cfg.get("tier_min_results", 5))
        breaker = Breaker()
        status: dict[str, SourceStatus] = {}
        search_topic = {**topic, "keywords": effective_keywords(cfg, topic, root, llm)}

        # 1) 依 tier 逐層搜尋 → 補資料 → 用摘要分類評分；fallback 策略下高相關論文夠多就停止往下一層
        scored: list[tuple[Paper, dict]] = []
        seen_run: set[str] = set()
        for label, tier, journals in search_passes(cfg, topic, root):
            since = tier_window(store, name, tier, today, lookback, cfg, topic)
            if since is None:
                log.info("--- %s：未到搜尋週期，今天略過 ---", label)
                continue
            log.info("--- %s（%s 起）---", label, since.isoformat())
            new = []
            # dry-run 不呼叫 AI，所以也不啟動瀏覽器備援（RSS 仍會讀）
            for p in collect(search_topic, since, limit, journals, cfg, use_browser=not dry_run, status=status):
                keys = {p.uid, p.title_key} - {None}
                if keys & seen_run or store.seen(name, p) or store.gave_up(name, p):
                    continue
                seen_run |= keys
                new.append(p)
            log.info("新論文 %d 篇", len(new))
            for p in new:
                try:
                    enrich(p)  # WoS 等來源缺摘要／PDF 時，依 DOI 補齊
                except Exception as e:
                    log.warning("補資料失敗 %s：%s", p.title[:60], e)
                p.extra.update(tier=tier, tier_label=label)
            if dry_run:
                for p in new:
                    print(f"- [T{tier}|{p.source}] {p.title} ({p.venue or ''} {p.published or ''}) "
                          f"摘要={'有' if p.abstract else '無'} PDF={'有' if p.pdf_url else '無'}")
                continue
            for p, result, err in _classify_iter(llm, new, topic):
                if result is None or result.get("relevance", 0) < 1:
                    breaker.fail(p, err or "分類失敗（AI 拒答或沒有回應）")
                    log.warning("  分類失敗，下次重試：%s %s", p.title[:60], err)
                    if breaker.tripped:
                        break
                    continue
                breaker.ok(store, name)
                scored.append((p, result))
                log.info("  [T%s|%s|%s] %s", tier, result["category"], result["relevance"], p.title[:80])
            if breaker.tripped:
                break
            store.mark_tier_run(name, tier, today)  # 這一層已搜完；下次從今天往後接著搜
            relevant = sum(r["relevance"] >= min_rel for _, r in scored)
            if strategy == "fallback" and relevant >= enough:
                log.info("已有 %d 篇高相關論文（門檻 %d），不再往下一層搜尋", relevant, enough)
                break
        if dry_run:
            continue

        # 2) 夠相關的才下載 + 深度解析（控制成本）；優先順序：tier 越前面越優先，同 tier 依相關性
        scored.sort(key=lambda x: (x[0].extra.get("tier", 0), -x[1]["relevance"]))
        items, jobs = [], []
        for p, r in scored:
            if r["relevance"] < min_rel:
                store.save(name, p, r["category"], r["relevance"], r["tldr"], None, None)
                continue
            cat_dir = topic_dir / r["category"]
            cat_dir.mkdir(parents=True, exist_ok=True)
            pdf = download_pdf(p, cat_dir)
            if len(jobs) < max_analyze:
                if not breaker.tripped:  # AI 服務異常時，本來要解析的論文先不標記，下次重試
                    jobs.append((p, r, pdf))
                continue
            store.save(name, p, r["category"], r["relevance"], r["tldr"], str(pdf) if pdf else None, None)
            items.append(_item(p, r, None, pdf))

        by_uid = {p.uid: (r, pdf) for p, r, pdf in jobs}
        failed_analysis = []
        for p, text, err in _analyze_iter(llm, [(p, pdf) for p, _, pdf in jobs], topic):
            r, pdf = by_uid[p.uid]
            if not text:
                breaker.fail(p, err or "解析失敗（AI 拒答或沒有回應）")
                failed_analysis.append((p, r, pdf))
                log.warning("  解析失敗：%s %s", p.title[:60], err)
                if breaker.tripped:
                    break
                continue
            breaker.ok(store, name)
            analysis_path = topic_dir / r["category"] / f"{p.slug}.md"
            analysis_path.write_text(_header(p, r, pdf) + text, encoding="utf-8")
            store.save(name, p, r["category"], r["relevance"], r["tldr"],
                       str(pdf) if pdf else None, str(analysis_path))
            items.append(_item(p, r, analysis_path, pdf))

        # 解析失敗的論文：還沒到重試上限就留到下次（不寫入已處理）；到上限則不再解析，但仍列進快報
        counts = breaker.flush(store, name)
        for p, r, pdf in failed_analysis:
            if counts.get(p.uid, 0) >= MAX_ATTEMPTS:
                store.save(name, p, r["category"], r["relevance"], r["tldr"], str(pdf) if pdf else None, None)
                items.append(_item(p, r, None, pdf))
        if breaker.tripped:
            digests.ai_failed = True
            log.error("AI 服務連續 %d 次失敗，已停止本主題的處理；尚未處理的論文下次執行會重試", Breaker.LIMIT)

        # 3) 匯出（RIS／BibTeX、Zotero）→ 每日快報（依 tier、相關性排序）→ 推播
        items.sort(key=lambda it: (it["tier"], -it["relevance"]))
        export_notes = export(cfg, root, topic, [(it["paper"], it["result"], it["analysis"]) for it in items])
        digest = _write_digest(root, topic, items, llm, warning=breaker.tripped, status=status, extra=export_notes)
        digests.append(digest)
        notify.send(cfg, f"{name}｜每日論文快報 {today.isoformat()}", digest.read_text(encoding="utf-8"),
                    items, warning=breaker.tripped)

    # 4) 每週綜合報告（到了設定的星期幾才產生，每週一次）
    if not dry_run and weekly.is_due(cfg, today):
        digests.weekly = weekly.generate(cfg, llm, today)
    return digests


def _item(p: Paper, r: dict, analysis_path: Path | None, pdf: Path | None) -> dict:
    return {"paper": p, "result": r, "title": p.title, "url": p.url, "venue": p.venue or "", "tier": p.extra.get("tier", 0),
            "tier_label": p.extra.get("tier_label", ""), "category": r["category"],
            "relevance": r["relevance"], "tldr": r["tldr"], "analysis": analysis_path, "pdf": pdf}


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


def _status_lines(status: dict[str, SourceStatus], items: list[dict], extra: list[str] | None = None) -> list[str]:
    lines = ["\n## 資料來源狀態\n"]
    for name, st in status.items():
        if st.errors:
            lines.append(f"- ⚠ **{name}**：API 無法使用（{'；'.join(st.errors)}），共取得 {st.found} 篇")
            lines += [f"  - 備援 {n}" for n in st.notes]
        else:
            lines.append(f"- ✓ **{name}**：{st.found} 篇")
    if items:
        with_pdf = sum(1 for it in items if it["pdf"])
        lines.append(f"- 全文 PDF：{with_pdf}/{len(items)} 篇取得開放取用版本")
    lines += [f"- {note}" for note in extra or []]
    return lines


def _write_digest(root: Path, topic: dict, items: list[dict], llm, warning: bool = False,
                  status: dict[str, SourceStatus] | None = None, extra: list[str] | None = None) -> Path:
    today = date.today().isoformat()
    path = root / "digests" / f"{today}_{topic['folder']}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not items and not warning and path.exists():
        return path  # 同一天重跑且沒有新論文時，保留早先的快報
    lines = [f"# {topic['name']}｜每日論文快報 {today}\n"]
    if warning:
        lines.append("> ⚠ AI 服務連續失敗（可能是金鑰失效、額度用完或未登入），本次只處理了部分論文；"
                     "其餘論文下次執行會自動重試。\n")
    if not items:
        lines.append("今天沒有新的高相關論文。\n")
    else:
        overview, err = _try(llm.daily_overview, topic, items)
        if err:
            log.warning("每日總覽產生失敗：%s", err)
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
            venue = f"*{it['venue']}*　" if it["venue"] else ""
            lines.append(f"- **[{it['title']}]({it['url']})**　{venue}`{it['category']}`　"
                         f"相關性 {it['relevance']}/5 {link}\n  {it['tldr']}")
    if status:
        lines += _status_lines(status, items, extra)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log.info("快報已寫入 %s", path)
    return path
