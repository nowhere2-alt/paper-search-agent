"""Claude 相關的三個任務：分類、深度解析、每日總覽。"""

from __future__ import annotations

import base64
import json
import logging
from pathlib import Path

import anthropic

from .models import Paper

log = logging.getLogger(__name__)

# PDF 直接以 base64 送進 Claude（單次請求上限 32 MB，保留一些餘裕）
MAX_PDF_BYTES = 24 * 1024 * 1024
# 拒答時由伺服器自動改用 Anthropic 建議的備援模型重試
FALLBACK_BETA = "server-side-fallback-2026-07-01"


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
        categories = list(topic["categories"].keys())
        cat_desc = "\n".join(f"- {k}: {v}" for k, v in topic["categories"].items())
        schema = {
            "type": "object",
            "properties": {
                "category": {"type": "string", "enum": categories + ["other"]},
                "relevance": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
                "tldr": {"type": "string"},
            },
            "required": ["category", "relevance", "tldr"],
            "additionalProperties": False,
        }
        system = (
            "你是協助研究者篩選文獻的研究助理。根據論文標題與摘要，"
            "判斷它屬於哪個分類資料夾，並評估它與研究主題的相關性。"
        )
        prompt = f"""研究主題：{topic['name']}
主題說明：{topic.get('description', '')}

可用分類（資料夾）：
{cat_desc}
- other: 不屬於以上任何一類

相關性評分：5 = 直接回答本主題的核心問題；3 = 部分相關，值得一讀；1 = 只是關鍵字巧合。
tldr：用{self.language}寫一句話（50 字內）說明這篇論文做了什麼、發現了什麼。

論文標題：{paper.title}
作者：{', '.join(paper.authors[:8])}
發表：{paper.venue or ''} {paper.published or paper.year or ''}
作者關鍵字：{', '.join(paper.extra.get('author_keywords', [])) or '（無）'}
摘要：{paper.abstract or '（無摘要，請依標題與關鍵字判斷）'}"""
        text = self._call(system=system, content=prompt, effort="low",
                          max_tokens=4000, schema=schema)
        if text is None:
            return {"category": "other", "relevance": 0, "tldr": ""}
        return json.loads(text)

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
            basis = "無法取得全文，只能根據摘要分析，請在文章開頭註明「本解析僅依據摘要」。"

        system = (
            f"你是一位資深研究者，負責為研究團隊撰寫論文解析文章。請用{self.language}撰寫，"
            "專有名詞保留英文原文。內容須忠於論文，不要杜撰論文沒有的數據；"
            "若某部分論文未提供，就寫「論文未說明」。"
        )
        prompt = f"""{basis}

我的研究主題是「{topic['name']}」：{topic.get('description', '')}

論文：{paper.title}
作者：{', '.join(paper.authors)}
來源：{paper.venue or ''}，{paper.published or paper.year or ''}
連結：{paper.url}
摘要：{paper.abstract}

請寫一篇 Markdown 格式的論文解析文章，包含以下段落：
## 一句話總結
## 研究問題與動機
## 方法（研究設計、資料、模型或理論架構）
## 主要發現（附關鍵數據）
## 貢獻與創新之處
## 限制與可質疑之處
## 與我的研究主題的關聯（可如何引用、可借鏡的方法、可延伸的研究缺口）
## 關鍵字與值得追蹤的參考文獻"""
        content.append({"type": "text", "text": prompt})
        return self._call(system=system, content=content, effort=self.analysis_effort)

    # ---------- 3. 每日總覽 ----------
    def daily_overview(self, topic: dict, items: list[dict]) -> str | None:
        listing = "\n".join(
            f"- [{it['category']}] {it['title']}（{it.get('venue', '')}；{it.get('tier_label', '')}；"
            f"相關性 {it['relevance']}）：{it['tldr']}"
            for it in items
        )
        system = f"你是研究助理，負責撰寫每日文獻快報。用{self.language}撰寫，簡潔、具洞察力。"
        prompt = f"""研究主題：「{topic['name']}」
今天新收錄的論文：
{listing}

請寫 3–6 段的「今日研究動態」：歸納這批論文的共同趨勢、值得注意的新方法或反直覺發現，
以及建議我優先精讀哪幾篇、為什麼。不要逐篇重述，直接輸出 Markdown 內文（不需要大標題）。"""
        return self._call(system=system, content=prompt, effort="medium", max_tokens=8000)
