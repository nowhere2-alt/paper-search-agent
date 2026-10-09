# Paper Search Agent：每日論文追蹤助理

設定好研究主題與關鍵字後，它每天會自動完成以下工作：

1. **搜尋**：到你指定的學術資料庫（arXiv、Semantic Scholar、OpenAlex）抓新論文，並把跨資料庫的重複論文合併
2. **分類存檔**：由 Claude 讀標題與摘要，評估相關性（1–5 分），並分到你定義的子資料夾；夠相關的論文會下載開放取用（Open Access）的 PDF
3. **解析**：Claude 讀完全文 PDF，寫一篇結構化的中文解析文章，最後彙整成「每日論文快報」

## 架構

```
          config.yaml（主題、關鍵字、分類）
                    │
   ┌────────────────┼────────────────┐
 arXiv      Semantic Scholar      OpenAlex        ← sources/：每個資料庫一個連接器
   └────────────────┼────────────────┘
          去重（DOI / arXiv ID / 標題）＋ SQLite 記錄已處理
                    │
      Claude 分類＋相關性評分（只看摘要，effort=low，便宜）
                    │  相關性 ≥ min_relevance
          下載 PDF → library/<主題>/<分類>/
                    │
      Claude 讀全文 PDF 寫解析文章（effort=high）→ 同資料夾 .md
                    │
      Claude 寫「今日研究動態」→ library/digests/<日期>_<主題>.md
```

這是一個**固定流程的 workflow**，不是讓 AI 自由決定下一步的開放式 agent。每天的工作步驟固定，用程式控制流程比較可靠，也比較容易控制成本；Claude 只負責需要判斷與閱讀理解的步驟。

產出的資料夾結構：

```
library/
├── genai_hospitality/
│   ├── consumer_behavior/
│   │   ├── 2026_Lee_LLM_Travel_Agents.pdf
│   │   └── 2026_Lee_LLM_Travel_Agents.md    ← 解析文章
│   ├── service_operations/
│   └── other/
├── digests/
│   └── 2026-10-09_genai_hospitality.md      ← 每日快報
└── .state/papers.db                          ← 已處理論文紀錄
```

## 安裝

```bash
git clone <this repo> && cd paper-search-agent
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...                  # 到 https://console.anthropic.com 申請
# 選填（建議）：
export S2_API_KEY=...          # Semantic Scholar 免費金鑰，避免被限速
export OPENALEX_EMAIL=you@example.com
```

## 使用

1. 編輯 `config.yaml`：填入主題名稱、說明、關鍵字、要用的資料庫，以及分類資料夾。分類的說明寫得越具體，Claude 分得越準。
2. 先試跑，只搜尋、不花 API 費用：
   ```bash
   python -m paper_agent --dry-run --days 14
   ```
   看看抓到的論文對不對，再調整關鍵字。
3. 正式執行：
   ```bash
   python -m paper_agent            # 依 lookback_days 往回搜尋
   python -m paper_agent --days 30  # 第一次可以往回多抓一點
   ```

已處理過的論文會記在 `library/.state/papers.db`，重跑不會重複下載或重複分析。

## 每天自動執行

**方案 A：本機排程（PDF 留在自己電腦，推薦）**

- macOS / Linux：`crontab -e` 加入
  ```
  15 7 * * * cd /path/to/paper-search-agent && .venv/bin/python -m paper_agent >> agent.log 2>&1
  ```
- Windows：用「工作排程器」建立每日工作，程式填 `.venv\Scripts\python.exe`，引數填 `-m paper_agent`，起始位置填專案資料夾。

**方案 B：GitHub Actions（不用開電腦）**

`.github/workflows/daily.yml` 已設定好，每天台灣時間早上 6 點左右執行。到 repo 的 Settings → Secrets and variables → Actions 新增 `ANTHROPIC_API_KEY`（可另加 `S2_API_KEY`、`OPENALEX_EMAIL`）。解析文章與快報會自動 commit 回 repo；PDF 檔案較大，會存成 Actions artifact，保留 30 天。

## 成本控制

- 分類只送標題與摘要，使用 `effort: low`，每篇花費很少
- 相關性低於 `min_relevance` 的論文不下載、不做全文解析
- `max_analyses_per_topic` 限制每個主題每天最多解析幾篇（依相關性高低排序）
- 全文解析最花錢；想省錢可以把 `analysis_effort` 調成 `medium`

## 擴充

- **新增資料庫**：在 `paper_agent/sources/` 新增一個檔案，實作 `search(keywords, since, limit, cfg) -> list[Paper]`，再登記到 `sources/__init__.py` 的 `SOURCES`。PubMed（E-utilities）、CrossRef、IEEE Xplore 和 Scopus（需機構 API 金鑰）都可以用這個方式接上。
- **付費牆論文**：程式只下載開放取用的 PDF。拿不到全文時會改用摘要分析，並在文章開頭註明。若學校有 VPN 或 proxy，可以在 `downloader.py` 加上機構認證。
- **推播**：在 `pipeline.run()` 最後把快報寄到 Email、LINE Notify、Slack 或 Notion。
- **調整解析格式**：修改 `paper_agent/llm.py` 裡 `analyze()` 的段落要求，例如加入「理論基礎」或「量表題項」等段落。

## 注意事項

- Google Scholar 沒有官方 API，且禁止爬蟲，所以不支援。OpenAlex 與 Semantic Scholar 的收錄範圍和它相近。
- 請遵守各資料庫的使用條款與速率限制（程式已加入 arXiv 3 秒間隔、Semantic Scholar 429 重試）。
- AI 解析是為了幫你快速篩選文獻，引用前請回頭核對原文。
