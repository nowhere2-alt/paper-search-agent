"""Claude 版：分類、深度解析、每日總覽（透過 Anthropic API）。

省錢設定（config.yaml 的 claude 區塊）：
- classify_model：分類用的模型，可以和解析分開（例如分類用 Haiku，解析用 Opus）。
- use_batch：分類與解析改用 Message Batches API，費用為一般呼叫的一半。批次在 batch_timeout_min
  內沒跑完時，會取消批次，把還沒完成的項目改用一般呼叫處理，不會讓每日排程卡住。
"""

from __future__ import annotations

import base64
import json
import logging
import time
from collections.abc import Iterator
from pathlib import Path

import anthropic

from . import prompts
from .models import Paper

log = logging.getLogger(__name__)

# PDF 直接以 base64 送進 Claude（單次請求上限 32 MB，保留一些餘裕）
MAX_PDF_BYTES = 24 * 1024 * 1024
# 單一批次的大小上限是 256 MB；含 PDF 的解析請求分批送出，每批不超過這個大小
MAX_BATCH_BYTES = 180 * 1024 * 1024
# 拒答時由伺服器自動改用 Anthropic 建議的備援模型重試
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# 只有這些模型支援伺服器端備援（Haiku 不支援，傳了會被拒絕或沒有作用）
FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}


def make_llm(cfg: dict):
    """依 config 的 engine 選擇 Claude 版或 Codex 版。"""
    if cfg.get("engine", "claude") == "codex":
        from .codex_llm import CodexLLM
        codex_cfg = cfg.get("codex", {})
        return CodexLLM(codex_cfg, language=codex_cfg.get("language", "繁體中文"))
    return LLM(cfg.get("claude", {}))


def page_marker(citations) -> str:
    """把 PDF 引用轉成頁碼標記，例如 [p.5] 或 [p.5–6, 9]。end_page_number 不含（exclusive）。"""
    spans = []
    for c in citations or []:
        start, end = getattr(c, "start_page_number", None), getattr(c, "end_page_number", None)
        if start is None:
            continue
        last = max(start, (end or start + 1) - 1)
        span = f"{start}" if last == start else f"{start}–{last}"
        if span not in spans:
            spans.append(span)
    return f" [p.{', '.join(spans)}]" if spans else ""


def _text(msg) -> str | None:
    if msg.stop_reason == "refusal":
        log.warning("Claude 拒絕處理此請求：%s", msg.stop_details)
        return None
    if msg.stop_reason == "max_tokens":
        log.warning("輸出達到 max_tokens 上限，內容可能被截斷")
    # 開啟引用時，回覆會被切成多個 text 區塊，有引用的區塊帶 citations；在該段文字後面加上頁碼
    parts = []
    for b in msg.content:
        if b.type != "text":
            continue
        marker = page_marker(getattr(b, "citations", None))
        text = b.text
        if marker:
            stripped = text.rstrip()
            text = stripped + marker + text[len(stripped):]
        parts.append(text)
    return "".join(parts)


class LLM:
    def __init__(self, cfg: dict):
        self.client = anthropic.Anthropic()  # 讀取環境變數 ANTHROPIC_API_KEY
        self.model = cfg.get("model", "claude-opus-5-5")
        self.classify_model = cfg.get("classify_model") or self.model
        self.language = cfg.get("language", "繁體中文")
        self.analysis_effort = cfg.get("analysis_effort", "high")
        self.use_batch = cfg.get("use_batch", False)
        self.batch_timeout = cfg.get("batch_timeout_min", 60) * 60
        self.batch_poll = cfg.get("batch_poll_sec", 30)

    # ---------- 請求內容（一般呼叫與批次共用） ----------
    def _classify_params(self, paper: Paper, topic: dict) -> dict:
        system, prompt, schema = prompts.classify(paper, topic, self.language)
        return {"model": self.classify_model, "max_tokens": 4000, "system": system,
                "messages": [{"role": "user", "content": prompt}],
                "output_config": {"effort": "low", "format": {"type": "json_schema", "schema": schema}}}

    def _analyze_params(self, paper: Paper, topic: dict, pdf_path: Path | None) -> dict:
        content: list = []
        if pdf_path and pdf_path.stat().st_size <= MAX_PDF_BYTES:
            data = base64.standard_b64encode(pdf_path.read_bytes()).decode()
            content.append({"type": "document",
                            "source": {"type": "base64", "media_type": "application/pdf", "data": data},
                            "title": paper.title,
                            # 引用功能：解析中的每個論點附上原文頁碼，方便核對、降低杜撰
                            "citations": {"enabled": True}})
            basis = "以下附上論文全文 PDF。引用關鍵數據與發現時，請以原文為依據。"
        else:
            basis = prompts.ABSTRACT_ONLY
        system, prompt = prompts.analyze(paper, topic, self.language, basis)
        content.append({"type": "text", "text": prompt})
        return {"model": self.model, "max_tokens": 32000, "system": system,
                "messages": [{"role": "user", "content": content}],
                "output_config": {"effort": self.analysis_effort}}

    # ---------- 一般呼叫 ----------
    def _call(self, params: dict) -> str | None:
        extra = {}
        if params["model"] in FALLBACK_MODELS:
            extra = {"betas": [FALLBACK_BETA], "fallbacks": "default"}
        with self.client.beta.messages.stream(**params, **extra) as stream:
            return _text(stream.get_final_message())

    def classify(self, paper: Paper, topic: dict) -> dict:
        text = self._call(self._classify_params(paper, topic))
        return json.loads(text) if text else dict(prompts.FAILED)

    def analyze(self, paper: Paper, topic: dict, pdf_path: Path | None) -> str | None:
        return self._call(self._analyze_params(paper, topic, pdf_path))

    def expand_keywords(self, topic: dict) -> list[str]:
        system, prompt, schema = prompts.expand_keywords(topic)
        text = self._call({"model": self.classify_model, "max_tokens": 4000, "system": system,
                           "messages": [{"role": "user", "content": prompt}],
                           "output_config": {"effort": "low", "format": {"type": "json_schema", "schema": schema}}})
        if not text:
            raise RuntimeError("Claude 拒絕或沒有回應")
        return json.loads(text)["keywords"]

    def daily_overview(self, topic: dict, items: list[dict]) -> str | None:
        system, prompt = prompts.overview(topic, items, self.language)
        return self._call({"model": self.model, "max_tokens": 8000, "system": system,
                           "messages": [{"role": "user", "content": prompt}],
                           "output_config": {"effort": "medium"}})

    # ---------- 批次（主流程有 classify_batch / analyze_batch 時會優先使用） ----------
    def classify_batch(self, papers: list[Paper], topic: dict) -> Iterator[tuple[Paper, dict | None, str]]:
        """依序回傳 (論文, 分類結果或 None, 錯誤訊息)。"""
        if not self.use_batch:
            yield from _sequential(self.classify, papers, topic)
            return
        jobs = [(p, self._classify_params(p, topic)) for p in papers]
        for p, text, err in self._run_batches(jobs, lambda p: self.classify(p, topic)):
            if isinstance(text, dict):  # 已由一般呼叫補做
                yield p, text, err
                continue
            try:
                yield p, (json.loads(text) if text else None), err
            except json.JSONDecodeError:
                yield p, None, "分類結果不是合法 JSON"

    def analyze_batch(self, jobs: list[tuple[Paper, Path | None]], topic: dict) -> Iterator[tuple[Paper, str | None, str]]:
        if not self.use_batch:
            for p, pdf in jobs:
                yield (p, *_attempt(self.analyze, p, topic, pdf))
            return
        pdfs = {p.uid: pdf for p, pdf in jobs}
        requests = [(p, self._analyze_params(p, topic, pdf)) for p, pdf in jobs]
        yield from self._run_batches(requests, lambda p: self.analyze(p, topic, pdfs[p.uid]))

    def _run_batches(self, jobs: list[tuple[Paper, dict]], fallback) -> Iterator[tuple[Paper, object, str]]:
        """依大小分批送出、等待完成、依原順序回傳。逾時未完成的項目改用 fallback（一般呼叫）處理。"""
        chunks, current, size = [], [], 0
        for i, (p, params) in enumerate(jobs):
            n = len(json.dumps(params))
            if current and size + n > MAX_BATCH_BYTES:
                chunks.append(current)
                current, size = [], 0
            current.append((f"r{i}", p, params))
            size += n
        if current:
            chunks.append(current)

        for chunk in chunks:
            batch = self.client.messages.batches.create(
                requests=[{"custom_id": cid, "params": params} for cid, _, params in chunk])
            log.info("已送出批次 %s（%d 筆，費用為一般呼叫的一半），等待完成…", batch.id, len(chunk))
            deadline = time.monotonic() + self.batch_timeout
            while batch.processing_status != "ended" and time.monotonic() < deadline:
                time.sleep(self.batch_poll)
                batch = self.client.messages.batches.retrieve(batch.id)
            if batch.processing_status != "ended":
                log.warning("批次 %s 逾時，取消並改用一般呼叫處理未完成的項目", batch.id)
                self.client.messages.batches.cancel(batch.id)
                while batch.processing_status != "ended":  # 取消後要等狀態變成 ended 才能讀結果
                    time.sleep(min(self.batch_poll, 10))
                    batch = self.client.messages.batches.retrieve(batch.id)

            results = {}
            for r in self.client.messages.batches.results(batch.id):
                if r.result.type == "succeeded":
                    results[r.custom_id] = (_text(r.result.message), "")
                elif r.result.type == "errored":
                    err = r.result.error
                    kind = getattr(getattr(err, "error", None), "type", None) or getattr(err, "type", "unknown")
                    results[r.custom_id] = (None, f"批次錯誤：{kind}")
                # canceled / expired：沒有結果，下面改用一般呼叫
            for cid, p, _ in chunk:
                if cid in results:
                    text, err = results[cid]
                    yield p, text, err or ("" if text else "AI 拒答或沒有回應")
                else:
                    yield (p, *_attempt(fallback, p))


def _attempt(fn, *args) -> tuple[object, str]:
    try:
        return fn(*args), ""
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def _sequential(fn, papers: list[Paper], topic: dict) -> Iterator[tuple[Paper, object, str]]:
    for p in papers:
        yield (p, *_attempt(fn, p, topic))
