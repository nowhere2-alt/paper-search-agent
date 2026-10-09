# Paper Search Agent：每日論文追蹤助理

設定好研究主題與關鍵字後，它每天會自動完成以下工作：

1. **搜尋**：以你的**優先期刊清單**（`journals.yaml`，分 Tier 1–3）為搜尋範圍，從 **Web of Science**、Elsevier ScienceDirect、Crossref（各出版社登錄的最新文章）、OpenAlex、Semantic Scholar 抓新論文，並把跨資料庫的重複論文合併
2. **分類存檔**：由 Claude 讀標題與摘要，評估相關性（1–5 分），並分到你定義的子資料夾；夠相關的論文會下載開放取用（Open Access）的 PDF
3. **解析**：Claude 讀完全文 PDF，寫一篇結構化的中文解析文章，最後彙整成「每日論文快報」

## 架構

```
     config.yaml（主題、關鍵字、分類）＋ journals.yaml（Tier 1–3 優先期刊）
                    │
      ┌──── 依 Tier 逐層搜尋（fallback：高相關論文不夠才搜下一層）────┐
      │                                                              │
      │  Web of Science   ScienceDirect   Crossref   OpenAlex   S2    │ ← sources/
      │  SO=期刊名        Elsevier 期刊    逐本 ISSN   ISSN 篩選  venue │
      │  （主要來源，依 sources 順序決定優先權）                        │
      │                                                              │
      │  去重（DOI / 標題），書目資料以主要來源為準                       │
      │  依 DOI 補齊摘要與開放取用 PDF（OpenAlex / Semantic Scholar）     │
      │  Claude 分類＋相關性評分（只看摘要，effort=low，便宜）            │
      └──────────────────────────────────────────────────────────────┘
                    │  相關性 ≥ min_relevance，依 Tier → 相關性排序
          下載 PDF → library/<主題>/<分類>/
                    │
      Claude 讀全文 PDF 寫解析文章（effort=high）→ 同資料夾 .md
                    │
      Claude 寫「今日研究動態」→ library/digests/<日期>_<主題>.md（依 Tier 分段）
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
export WOS_API_KEY=...                               # Web of Science Starter API 金鑰，見下方說明
# 選填（建議）：
export ELSEVIER_API_KEY=...    # Elsevier ScienceDirect API 金鑰（https://dev.elsevier.com，學術用途免費）
export S2_API_KEY=...          # Semantic Scholar 免費金鑰，避免被限速
export CONTACT_EMAIL=you@example.com   # Crossref / OpenAlex 的 polite pool，速度較快較穩定
```

## 優先期刊清單（journals.yaml）

`journals.yaml` 是你的期刊清單，分成三層：

- **Tier 1**：觀光餐旅核心 SSCI 期刊，永遠最先搜尋
- **Tier 2**：相關觀光餐旅期刊
- **Tier 3**：行銷、管理、心理、資管等相關學科（備援）

搜尋策略在 `config.yaml` 設定：

| 設定 | 說明 |
|---|---|
| `tier_strategy: fallback`（預設） | 先搜 Tier 1，累計的高相關論文少於 `tier_min_results` 篇時才搜 Tier 2，仍不夠再搜 Tier 3 |
| `tier_strategy: all` | 每次都搜全部 Tier，快報依 Tier 分段 |
| `tiers: [1, 2]`（主題層級） | 只搜指定的 Tier |
| `open_search: true`（主題層級） | 期刊清單搜完後，再做一次不限期刊的搜尋 |
| `use_journal_list: false`（主題層級） | 不使用期刊清單，搜尋所有論文 |

深度解析的名額（`max_analyses_per_topic`）會優先給 Tier 1，同一層再依相關性排序。

每本期刊的 ISSN 會在第一次執行時自動向 OpenAlex / Crossref 查詢，並快取起來。建議先執行一次：

```bash
python -m paper_agent --list-journals
```

檢查每本期刊查到的 ISSN。顯示「找不到 ISSN」或查錯的期刊，在 `journals.yaml` 該行加上 `issn: ["xxxx-xxxx", "yyyy-yyyy"]` 手動指定。新增或刪除期刊，直接編輯 `journals.yaml` 即可。

### 各出版社的資料怎麼取得

| 出版社 | 取得方式 |
|---|---|
| Elsevier（ScienceDirect） | **Elsevier 官方 ScienceDirect Search API**（需 `ELSEVIER_API_KEY`），在標題、摘要、關鍵字中搜尋 |
| Taylor & Francis、SAGE、Emerald、Wiley、Springer 等 | **Crossref**：出版社會把每篇新文章的書目資料直接登錄到 Crossref，程式依 ISSN 逐本期刊抓新文章，再用關鍵字篩選 |
| 所有期刊 | Web of Science（`SO=` 期刊名稱）、OpenAlex（ISSN 篩選）、Semantic Scholar（venue 篩選） |

為什麼不直接爬 tandfonline.com、journals.sagepub.com、emerald.com 這些網站？這些出版社都沒有開放的搜尋 API，而且使用條款禁止自動化爬取，網站也有機器人防護。被偵測到時，通常會封鎖整個學校的 IP 存取權。Crossref 的資料是出版社自己登錄的，內容與官網相同，通常在文章上線當天就查得到，是合規又穩定的替代做法。

## 瀏覽器備援（API 無法使用時，由 Claude 操作瀏覽器搜尋）

申請不到 API 金鑰、額度用完或 API 連線失敗時，程式會改用 Claude 的 **browser use** 功能（`browser_toolset_20260801`）：在一個專用的 Chromium 瀏覽器中，由 Claude 看網頁、點選、輸入檢索條件，像人一樣在資料庫網站上搜尋，再把每篇符合的論文回報給程式。之後的補摘要、分類、解析流程都和 API 來源相同。

| 無法使用的 API | 改用的網站 |
|---|---|
| Web of Science（沒有 `WOS_API_KEY` 或額度用完） | webofscience.com 進階檢索（需要學校帳號，先手動登入一次） |
| ScienceDirect（沒有 `ELSEVIER_API_KEY`） | sciencedirect.com（只搜 Elsevier 期刊） |
| Crossref 查詢失敗、或期刊查不到 ISSN | 依期刊出版社：Taylor & Francis、SAGE、Emerald、Wiley、Springer 的網站 |

### 設定步驟

```bash
pip install playwright
playwright install chromium
python -m paper_agent --browser-login wos     # 開啟瀏覽器，用學校帳號登入 WoS，完成後按 Enter
```

登入狀態會保存在專用的瀏覽器設定檔（預設 `~/.paper-agent/browser-profile`），和你平常用的瀏覽器分開。如果登入時經過學校的 SSO 網域（例如 `sso.xxx.edu.tw`），請把它加到 `config.yaml` 的 `browser.extra_allowed_hosts`。出版社網站的搜尋不需要登入。

`config.yaml` 的 `browser` 區塊可以設定：

- `enabled`：開關
- `headless: false`：顯示瀏覽器視窗，看 Claude 怎麼操作
- `max_steps`：每個網站最多操作幾輪，用來控制花費

### 安全與使用規範

程式和給 Claude 的指示都加了以下限制：

- **只能開啟該資料庫的網域**：其他網址（包含網頁中的連結和網頁自己發出的請求）一律封鎖，防止網頁上的惡意內容把 Claude 引導到別的地方。
- **不碰帳號與付費**：Claude 不會輸入帳號密碼，不會接受條款、註冊或購買，也不會下載 PDF（瀏覽器的下載功能已關閉）。
- **遇到阻擋就停**：遇到登入頁、存取被拒、CAPTCHA 或機器人驗證時，Claude 會停下來回報，不會嘗試繞過。程式也沒有加任何規避網站偵測的設定。
- **用量很低**：每天每個網站只做少量搜尋，只讀取搜尋結果的書目資料。
- **網頁內容一律視為資料**：網頁上的文字不會被當成指令。

**請注意**：即使用量很低，自動化操作仍可能不符合部分資料庫或出版社的使用條款（例如 Clarivate 與學校的 WoS 授權合約）。建議先向學校圖書館確認，能申請到 API 金鑰時優先使用 API。這個功能只適合在你自己的電腦上執行；GitHub Actions 已預設關閉（`PAPER_AGENT_BROWSER=0`）。

**花費**：瀏覽器搜尋每一步都要呼叫一次 Claude（截圖約 1,000–1,800 tokens），一個網站通常需要 10–40 步，比 API 貴得多。在 fallback 策略下，每個 Tier 都可能各觸發一次。

## Web of Science 設定

本專案透過 Clarivate 官方的 **Web of Science Starter API** 取得 WoS 資料。

1. 到 [Clarivate Developer Portal](https://developer.clarivate.com/apis/wos-starter) 註冊，訂閱 Web of Science Starter API。如果學校有訂閱 WoS，可以選機構方案（請先問學校圖書館），額度較高；沒有的話可以先用免費方案試用。
2. 取得 API 金鑰後設定環境變數 `WOS_API_KEY`。沒有設定時，若已開啟瀏覽器備援，會改由 Claude 操作瀏覽器到 WoS 網站搜尋；沒開啟就略過 WoS，其他來源照常執行。
3. 在 `config.yaml` 把 `wos` 放在 `sources` 第一個，它就是主要來源。只想用 WoS 時寫 `sources: [wos]`。

檢索方式：

- 預設檢索式是 `TS=(關鍵字1 OR 關鍵字2 ...) AND PY=(年份範圍)`，依 WoS 收錄日期由新到舊排序。已處理過的論文會被略過，所以每天只會看到新收錄的論文。
- 使用期刊清單時，會自動加上 `SO=(該 Tier 的期刊名稱)`。
- 用 `wos_filter` 加上額外條件（以 AND 連接）。
- 用 `wos_query` 直接寫完整的 WoS 進階檢索式。建議先在 WoS 網站的進階檢索把檢索式調好，再貼進設定檔。
- 解析文章開頭會附上 WoS 編號（UT）與 WoS 被引次數。

限制：Starter API 不提供摘要，程式會依 DOI 改向 OpenAlex / Semantic Scholar 補摘要和開放取用 PDF。兩邊都查不到時，Claude 只能用標題和作者關鍵字分類。如果學校有 **Web of Science Expanded API**（含摘要），可以再改寫 `sources/wos.py` 接上。

> 參考專案 [cookjohn/wos-skills](https://github.com/cookjohn/wos-skills) 的做法是透過 Chrome 操作已登入的 WoS 網頁，呼叫網站內部的非公開 API。那種方式適合在 Claude Code 裡手動、互動式查詢，但不適合每天無人值守自動執行：它需要一直開著已登入的瀏覽器，網站改版就會失效，而且大量自動存取可能違反 Clarivate 與學校授權的使用條款，嚴重時可能導致學校的 WoS 存取權被暫停。所以本專案改用官方 API。

## 使用

1. 編輯 `config.yaml`：填入主題名稱、說明、關鍵字、要用的資料庫，以及分類資料夾。分類的說明寫得越具體，Claude 分得越準。必要時也調整 `journals.yaml`，並用 `--list-journals` 檢查 ISSN。
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

`.github/workflows/daily.yml` 已設定好，每天台灣時間早上 6 點左右執行。到 repo 的 Settings → Secrets and variables → Actions 新增 `ANTHROPIC_API_KEY`、`WOS_API_KEY`（可另加 `ELSEVIER_API_KEY`、`S2_API_KEY`、`CONTACT_EMAIL`）。解析文章與快報會自動 commit 回 repo；PDF 檔案較大，會存成 Actions artifact，保留 30 天。

## 成本控制

- 分類只送標題與摘要，使用 `effort: low`，每篇花費很少
- 相關性低於 `min_relevance` 的論文不下載、不做全文解析
- `max_analyses_per_topic` 限制每個主題每天最多解析幾篇（依相關性高低排序）
- 全文解析最花錢；想省錢可以把 `analysis_effort` 調成 `medium`

## 擴充

- **新增資料庫**：在 `paper_agent/sources/` 新增一個檔案，實作 `search(keywords, since, limit, cfg, journals=None) -> list[Paper]`（`journals` 有值時只搜這些期刊），再登記到 `sources/__init__.py` 的 `SOURCES`。Scopus（Elsevier，同一把金鑰）、Springer Nature Meta API、PubMed 都可以用這個方式接上。
- **付費牆論文**：程式只下載開放取用的 PDF。拿不到全文時會改用摘要分析，並在文章開頭註明。若學校有 VPN 或 proxy，可以在 `downloader.py` 加上機構認證。
- **推播**：在 `pipeline.run()` 最後把快報寄到 Email、LINE Notify、Slack 或 Notion。
- **調整解析格式**：修改 `paper_agent/llm.py` 裡 `analyze()` 的段落要求，例如加入「理論基礎」或「量表題項」等段落。

## 注意事項

- Google Scholar 沒有官方 API，且禁止爬蟲，所以不支援。OpenAlex 與 Semantic Scholar 的收錄範圍和它相近。
- WoS API 有每日請求額度（依方案而定）。每個主題每天大約只需要 1–2 次請求，一般用量不會超過。
- 請遵守各資料庫的使用條款與速率限制（程式已加入 arXiv 3 秒間隔、Semantic Scholar 與 WoS 429 重試、Crossref 與 ScienceDirect 逐本查詢間隔）。
- Crossref 只能用標題和摘要比對關鍵字，Elsevier 不提供摘要給 Crossref，所以 Elsevier 期刊建議設定 `ELSEVIER_API_KEY`。WoS 的 TS 欄位也會比對摘要，可以互補。
- AI 解析是為了幫你快速篩選文獻，引用前請回頭核對原文。
