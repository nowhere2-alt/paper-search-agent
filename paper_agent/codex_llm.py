"""Codex 版：分類、深度解析、每日總覽改由 OpenAI Codex CLI（codex exec）完成。

使用你的 Codex 登入（codex login，ChatGPT 方案）或 CODEX_API_KEY，不需要 Anthropic API 金鑰。
每次呼叫都在一個空的暫存資料夾、唯讀沙箱（read-only，無網路）中執行，
論文內容只透過標準輸入傳給 Codex，Codex 無法執行有副作用的指令。
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import prompts
from .models import Paper

log = logging.getLogger(__name__)
# 全文擷取上限（字元）。超過時只送前段並在提示中註明，不會無聲截斷。
MAX_FULLTEXT_CHARS = 400_000


def pdf_text(path: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    return "\n\n".join((page.extract_text() or "") for page in reader.pages).strip()


class CodexLLM:
    def __init__(self, cfg: dict, language: str = "繁體中文"):
        self.bin = cfg.get("bin", "codex")
        if not shutil.which(self.bin):
            raise RuntimeError("找不到 codex 指令，請先安裝：npm install -g @openai/codex，並執行 codex login")
        self.model = cfg.get("model")  # 不指定就用 Codex 的預設模型
        self.language = language
        self.analysis_effort = cfg.get("analysis_effort", "high")
        self.timeout = cfg.get("timeout_sec", 900)

    def run(self, prompt: str, *, schema: dict | None = None, effort: str | None = None,
            sandbox: str = "read-only", extra_args: list[str] | None = None,
            timeout: int | None = None) -> str | None:
        """執行一次 codex exec，回傳最後一則訊息；失敗回傳 None。"""
        with tempfile.TemporaryDirectory(prefix="paper-agent-codex-") as tmp:
            out = Path(tmp) / "last_message.txt"
            # -C 指向空的暫存資料夾：避免讀到專案的 AGENTS.md 或檔案，影響論文分析
            cmd = [self.bin, "exec", "--skip-git-repo-check", "--ephemeral", "--color", "never",
                   "-s", sandbox, "-C", tmp, "-o", str(out),
                   # 連不上模型時直接失敗，不要無限重連到逾時（交給主流程的重試與斷路器處理）。
                   # 用 -c 而非 --disable：未來版本若移除這個功能名稱，-c 只會被忽略，不會報錯。
                   "-c", "features.unbounded_connection_retries=false"]
            if self.model:
                cmd += ["-m", self.model]
            if effort:
                cmd += ["-c", f'model_reasoning_effort="{effort}"']
            if schema:
                schema_path = Path(tmp) / "schema.json"
                schema_path.write_text(json.dumps(schema), encoding="utf-8")
                cmd += ["--output-schema", str(schema_path)]
            cmd += (extra_args or []) + ["-"]  # 提示詞從標準輸入傳入，避免命令列長度限制
            try:
                proc = subprocess.run(cmd, input=prompt, text=True, capture_output=True,
                                      timeout=timeout or self.timeout)
            except subprocess.TimeoutExpired:
                log.warning("codex exec 逾時")
                return None
            if proc.returncode != 0 or not out.exists():
                log.warning("codex exec 失敗（exit %s）：%s", proc.returncode, proc.stderr.strip()[-500:])
                return None
            return out.read_text(encoding="utf-8").strip()

    def classify(self, paper: Paper, topic: dict) -> dict:
        system, prompt, schema = prompts.classify(paper, topic, self.language)
        text = self.run(f"{system}\n\n{prompt}\n\n只輸出符合 JSON schema 的結果。", schema=schema, effort="low",
                        timeout=min(self.timeout, 180))  # 分類很快，不用等到長逾時
        try:
            return json.loads(text) if text else dict(prompts.FAILED)
        except json.JSONDecodeError:
            log.warning("Codex 分類結果不是合法 JSON：%s", text[:200])
            return dict(prompts.FAILED)

    def analyze(self, paper: Paper, topic: dict, pdf_path: Path | None) -> str | None:
        fulltext = ""
        if pdf_path:
            try:
                fulltext = pdf_text(pdf_path)
            except Exception as e:
                log.warning("PDF 文字擷取失敗 %s：%s", pdf_path.name, e)
        if len(fulltext) > 1000:
            note = ""
            if len(fulltext) > MAX_FULLTEXT_CHARS:
                log.warning("全文過長（%d 字元），只送前 %d 字元", len(fulltext), MAX_FULLTEXT_CHARS)
                fulltext = fulltext[:MAX_FULLTEXT_CHARS]
                note = "（全文過長，以下只包含前段內容，請在文章開頭註明）"
            basis = f"以下 <paper> 標籤內是從 PDF 擷取的論文全文{note}。\n<paper>\n{fulltext}\n</paper>"
        else:  # 沒有 PDF，或掃描檔擷取不到文字
            basis = prompts.ABSTRACT_ONLY
        system, prompt = prompts.analyze(paper, topic, self.language, basis)
        return self.run(f"{system}\n\n{prompt}\n\n直接輸出解析文章的 Markdown，不要加其他說明。",
                        effort=self.analysis_effort)

    def daily_overview(self, topic: dict, items: list[dict]) -> str | None:
        system, prompt = prompts.overview(topic, items, self.language)
        return self.run(f"{system}\n\n{prompt}", effort="medium")
