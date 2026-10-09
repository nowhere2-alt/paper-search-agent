"""Claude 版：分類、深度解析、每日總覽（透過 Anthropic API）。"""

from __future__ import annotations

import base64
import json
import logging
from pathlib import Path

import anthropic

from . import prompts
from .models import Paper

log = logging.getLogger(__name__)

# PDF 直接以 base64 送進 Claude（單次請求上限 32 MB，保留一些餘裕）
MAX_PDF_BYTES = 24 * 1024 * 1024
# 拒答時由伺服器自動改用 Anthropic 建議的備援模型重試
FALLBACK_BETA = "server-side-fallback-2026-07-01"


def make_llm(cfg: dict):
    """依 config 的 engine 選擇 Claude 版或 Codex 版。"""
    if cfg.get("engine", "claude") == "codex":
        from .codex_llm import CodexLLM
        codex_cfg = cfg.get("codex", {})
        return CodexLLM(codex_cfg, language=codex_cfg.get("language", "繁體中文"))
    return LLM(cfg.get("claude", {}))


class LLM:
    def __init__(self, cfg: dict):
        self.client = anthropic.Anthropic()  # 讀取環境變數 ANTHROPIC_API_KEY
        self.model = cfg.get("model", "claude-opus-5-5")
        self.language = cfg.get("language", "繁體中文")
        self.analysis_effort = cfg.get("analysis_effort", "high")

    def _call(self, *, system: str, content: list | str, effort: str,
              max_tokens: int = 32000, schema: dict | None = None) -> str | None:
        output_config: dict = {"effort": effort}
        if schema:
            output_config["format"] = {"type": "json_schema", "schema": schema}
        with self.client.beta.messages.stream(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": content}],
            output_config=output_config,
            betas=[FALLBACK_BETA],
            fallbacks="default",
        ) as stream:
            msg = stream.get_final_message()
        if msg.stop_reason == "refusal":
            log.warning("Claude 拒絕處理此請求：%s", msg.stop_details)
            return None
        if msg.stop_reason == "max_tokens":
            log.warning("輸出達到 max_tokens 上限，內容可能被截斷")
        return "".join(b.text for b in msg.content if b.type == "text")

    # ---------- 1. 分類 + 相關性評分（只看標題摘要，便宜快速） ----------
    def classify(self, paper: Paper, topic: dict) -> dict:
        system, prompt, schema = prompts.classify(paper, topic, self.language)
        text = self._call(system=system, content=prompt, effort="low", max_tokens=4000, schema=schema)
        return json.loads(text) if text else dict(prompts.FAILED)

    # ---------- 2. 深度解析（讀全文 PDF） ----------
    def analyze(self, paper: Paper, topic: dict, pdf_path: Path | None) -> str | None:
        content: list = []
        if pdf_path and pdf_path.stat().st_size <= MAX_PDF_BYTES:
            data = base64.standard_b64encode(pdf_path.read_bytes()).decode()
            content.append({"type": "document",
                            "source": {"type": "base64", "media_type": "application/pdf", "data": data},
                            "title": paper.title})
            basis = "以下附上論文全文 PDF。"
        else:
            basis = prompts.ABSTRACT_ONLY
        system, prompt = prompts.analyze(paper, topic, self.language, basis)
        content.append({"type": "text", "text": prompt})
        return self._call(system=system, content=content, effort=self.analysis_effort)

    # ---------- 3. 每日總覽 ----------
    def daily_overview(self, topic: dict, items: list[dict]) -> str | None:
        system, prompt = prompts.overview(topic, items, self.language)
        return self._call(system=system, content=prompt, effort="medium", max_tokens=8000)
