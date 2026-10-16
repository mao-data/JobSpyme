# Job Hunter

每天在雲端（GitHub Actions）自動找美國職缺、排除不提供 H-1B 擔保的職缺，再用 Claude 依照你的履歷打分數，結果自動加進你的 Google Sheet。分數高的職缺還會附上客製化 cover letter。

評分用的是你的 **Claude Pro/Max 訂閱**（透過 Claude Code），不需要 API key。

```
每天 GitHub Actions 定時執行
  → JobSpy 抓職缺 (Indeed / LinkedIn / ZipRecruiter / Glassdoor)
  → 跳過 Google Sheet 裡已經有的職缺
  → 規則篩選：州別、職稱（排除 Senior/Manager…）、「不提供 sponsorship」
  → Claude 評分：契合度 1-10、再判斷一次 sponsorship、所需年資、缺少的技能
  → 高分職缺：產生 cover letter
  → 新增到 Google Sheet：「Jobs」分頁放通過的職缺，「Filtered out」分頁放被排除的職缺和原因
```

## Google Sheet

每次執行只會**新增**列，不會改動你已經填的內容。分頁有：

| 分頁 | 內容 |
|---|---|
| **今日精選** | 自動列出最近 3 天、7 分以上、`status` 還是空白的職缺，依分數排序。標了 status 就會從這頁消失。**每天只看這頁就好。** |
| **Jobs** | 所有通過篩選的職缺，每次執行後**自動排序：最新的在最上面**，同一天的再依分數由高到低。Claude 評 3 分（含）以下的不會放進來，會直接進 Filtered out。`status` 是下拉選單（想投 / 已投 / 面試 / Offer / 拒絕 / 不投），選了之後整列會上色；8 分以上的分數會變粗體綠字。 |
| **Filtered out** | 被排除的職缺和原因，可以用來檢查有沒有誤判。 |
| **待評分** | 這次沒評到分的職缺（超過評分上限，或 Claude 評分失敗）。下次執行會**優先**評這些；連續 3 次評分失敗才會放進 Jobs，標「未評分」。正常情況下這頁是空的。 |
| **Run log** | 每次執行一列：成功或失敗、各數量、哪些步驟重試後還是失敗。見下方「自動重試和執行紀錄」。 |
| **Archive** | 超過 14 天、`status` 還是空白的職缺，會自動從 Jobs 搬來這裡。**不想投的職缺不用標「不投」**，放著不管就好。 |

使用方式：
- 想投的職缺，在 **Jobs** 分頁把 `status` 改成「想投」，下次執行就會自動產生客製履歷（見下方「客製履歷」）。「今日精選」是公式產生的，不能在那裡改 status。
- 投完之後把 `status` 改成「已投」，之後依進度改成「面試」等等。標過 status 的列永遠不會被移動。
- 可以任意排序、篩選、加欄位，程式是依第 1 列的欄名寫入。
- **不要刪除列**：刪掉的職缺如果重新刊登，會再跑回來。不想看的就標「不投」或放著讓它自動歸檔。
- 精選的門檻（分數、天數）和歸檔天數，可以在 `config.yaml` 的 `sheet` 區段調整。「今日精選」分頁刪掉後，下次執行會重建。

## 客製履歷（status 設為「想投」）

在 Jobs 分頁把某個職缺的 `status` 改成**想投**，下一次執行時程式會：

1. 把職缺描述和你的**經歷資料庫**交給 Claude，挑出最相關的經歷、調整順序和措辭，產生一頁履歷和一封 cover letter。
2. **事實檢查**：每一條 bullet 都要標明根據哪一條經歷。只要出現經歷資料庫裡沒有的數字或工具（例如資料庫裡沒有 Snowflake，卻寫了 Snowflake），就退回重寫，最多 3 次。提到簽證或工作身分也會退回。
3. 用 [RenderCV](https://github.com/rendercv/rendercv) 排成**一頁 PDF**。超過一頁時，依序刪掉證照、TA，再刪最長那段經歷的最後一條，直到剩一頁。
4. 上傳到你的 Google Drive 的 `Job Hunter Resumes/年-月/` 資料夾：PDF、可以編輯的 Google Doc 版履歷、cover letter（Google Doc）。
5. 在同一列填入：

| 欄位 | 內容 |
|---|---|
| `resume_pdf` / `resume_doc` / `cover_letter_doc` | 點了就開的連結 |
| `resume_changes` | 這份履歷強調了什麼、為什麼（中文） |
| `keyword_coverage` | 職缺要求的關鍵字，原本的履歷有幾個 → 新履歷有幾個，以及還缺哪些。缺的關鍵字如果你其實會，把它加進經歷資料庫，下次就會用上 |

- **不想等隔天**：到 Actions → Job Hunter → Run workflow，勾選 `resumes_only`，幾分鐘內就會產生，不會抓新職缺。
- **產生失敗**時，`resume_changes` 會寫「產生失敗（第 n 次）」和原因，下次執行自動再試。連續 3 次失敗就停止重試；清空那一格會重新開始。
- **想重新產生**：清空 `resume_pdf` 那一格。
- 職缺描述存在隱藏的 `_descriptions` 分頁。這個功能上線前就在 Sheet 裡的職缺沒有描述，程式會試著從職缺網址抓；抓不到就只依職稱撰寫，`resume_changes` 會註明。
- 每份履歷用一次 Claude（事實檢查不過時會多幾次）。預設用 opus，寫得比較好；想省額度，可以在 `config.yaml` 把 `resume.cli_model` 改成 `sonnet`。

### 經歷資料庫（EXPERIENCE_BANK）

一個 YAML，列出你的聯絡資料、每段工作的「事實」（每條有 id 和標籤）、學歷、技能、證照，以及改寫規則（例如「不寫工作身分」「不寫 MBA」）。**履歷裡的每一句話都只能來自這裡。**有新的經歷或成果，就更新這個 Secret，之後的履歷就會用上。

它含有個人資料，所以放在 GitHub Secret，不會進 repo。在自己電腦上執行時，可以存成 `job_hunter/experience_bank.yaml`（已被 `.gitignore` 排除）。

## 自動重試和執行紀錄

每一次搜尋、每一次 Claude 呼叫、每一個 LinkedIn 職缺頁面、每一次寫入 Google Sheet，失敗時都會**自動重試，最多 3 次**，中間會等待一段時間再試（LinkedIn 等比較久）。

- 某個網站的搜尋**連續 3 次**重試後還是失敗，就判定它今天在擋我們，這次執行跳過它剩下的搜尋，不浪費時間。
- 重試後仍然失敗的步驟，會記錄在三個地方：
  1. **Sheet 的「Run log」分頁**：每次執行一列，包括狀態（OK / 部分失敗 / 失敗）、抓了幾筆、新增幾筆、評了幾筆、哪些步驟失敗和原因、各網站筆數，以及 GitHub 執行紀錄的連結。
  2. **GitHub Actions 執行頁面**的摘要區：失敗、重試成功、警告分開列出。
  3. 執行紀錄的最後面：`===== Run summary =====`。
- **寫入 Sheet 失敗時，這次執行會標成失敗，GitHub 會寄 email 通知你。** 這些職缺不會遺失：搜尋範圍是 48 小時，隔天的執行會再找到它們，因為它們還不在 Sheet 裡。
- 讀取 Sheet 失敗時，程式會直接停止，不會開始抓職缺，以免同一批職缺被重複寫入。

## 除錯：確認每一筆職缺都有下落

每次執行最後會做兩項檢查，結果寫在 Run log 的 `check`（OK / 不符）和 `accounting` 欄，也會顯示在 GitHub Actions 執行頁面的摘要：

1. **對帳**：抓到的每一筆職缺，都必須剛好落在其中一處：重複、Sheet 裡已經有、被規則排除、評分後留在 Jobs、被 Claude 排除、或排進「待評分」。四條等式只要有一條對不上，就會標成「不符」，並列為失敗。

   ```
   OK  抓到 = 重複 + 已在 Sheet + 被規則排除 + 通過規則
   OK  通過 + 待評分帶入 = 評分成功 + 評分失敗 + 超過上限
   OK  評分成功 = 留在 Jobs + 被 Claude 排除
   OK  評分失敗 + 超過上限 = 排進待評分 + 放棄（3 次失敗）
   ```

2. **回讀驗證**：寫入 Sheet 後再讀一次，確認每一筆該寫的職缺真的在 Jobs、Filtered out、待評分裡。少了任何一筆都會列為失敗。

另外，一批 8 筆評分失敗時，失敗的那幾筆會**一筆一筆再評一次**，避免一個怪職缺拖累整批。如果一次失敗超過 20 筆，代表 Claude 可能暫時出問題，就不逐筆重試，直接排進待評分，等下次再評。

## 雲端設定（只要做一次）

### 1. Claude 訂閱 token

在你自己的電腦上執行：

```bash
npm install -g @anthropic-ai/claude-code
claude setup-token
```

它會讓你用 Claude 帳號登入，然後印出一組長效 token。

### 2. Google 服務帳號（讓程式能寫入你的 Sheet）

1. 打開 [Google Cloud Console](https://console.cloud.google.com/)，建立一個專案，名字隨意。
2. 「API 和服務」→「程式庫」→ 搜尋 **Google Sheets API** → 啟用。
3. 「IAM 與管理」→「服務帳號」→「建立服務帳號」，名字隨意，不需要指派角色。
4. 點進剛建好的服務帳號 →「金鑰」→「新增金鑰」→ JSON。瀏覽器會下載一個 `.json` 檔。
5. 打開那個 JSON，複製 `client_email` 的值（像 `xxx@xxx.iam.gserviceaccount.com`）。
6. 打開 Google Sheet →「共用」→ 貼上這個 email，權限設為**編輯者**。

### 3. 在 GitHub 設定 Secrets

到 repo 的 **Settings → Secrets and variables → Actions → New repository secret**，新增：

| Secret 名稱 | 內容 |
|---|---|
| `CLAUDE_CODE_OAUTH_TOKEN` | 步驟 1 得到的 token |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | 步驟 2 下載的 JSON 檔**完整內容** |
| `RESUME_TEXT` | 你的履歷**純文字**（從 PDF 複製貼上即可） |
| `EXPERIENCE_BANK` | 經歷資料庫 YAML 的**完整內容**（客製履歷用） |
| `GOOGLE_OAUTH_TOKEN_JSON` | 有 Drive 權限的 OAuth token JSON（客製履歷上傳用，見下方步驟 5） |
| `JOBSPY_PROXIES`（選填） | proxy 清單，用逗號分隔，例如 `user:pass@host:port,user:pass@host2:port` |

履歷用 Secret 存放，不會出現在 repo 裡。

### 4. 第一次手動執行

1. 到 repo 的 **Actions** 分頁。如果看到提示要啟用 workflows，按啟用。
2. 左側點 **Job Hunter** → **Run workflow**。
3. 第一次建議勾選 `no_claude`，只測試抓職缺和寫入 Sheet。成功後再不勾選跑一次完整流程。

之後每天台灣時間晚上 10:17（美西早上約 7 點）會自動執行。要改時間，修改 `.github/workflows/job-hunter.yml` 的 `cron`（時間是 UTC）。

### 5. Google Drive 授權（客製履歷用）

服務帳號不能在個人 Drive 建立檔案，所以履歷用你自己的帳號上傳：
1. 在同一個 Google Cloud 專案啟用 **Google Drive API**。
2. 「OAuth 同意畫面」設為**外部**，並按「**發布應用程式**」（測試模式的 token 7 天就會失效）。
3. 建立「OAuth 用戶端 ID」（類型：電腦版應用程式），用它取得 `https://www.googleapis.com/auth/drive.file` 範圍的 refresh token。
4. 把 `{"client_id": ..., "client_secret": ..., "refresh_token": ..., "token_uri": "https://oauth2.googleapis.com/token"}` 存成 Secret `GOOGLE_OAUTH_TOKEN_JSON`。

`drive.file` 範圍只能看到程式自己建立的檔案，碰不到你 Drive 裡的其他東西。

## 設定（config.yaml）

| 設定 | 說明 |
|---|---|
| `search_terms` | 搜尋的職稱 |
| `locations` | 目標州 |
| `hours_old` | 只要多少小時內刊登的。設 48 讓每天的搜尋範圍互相重疊，漏跑一天也不會漏職缺 |
| `sites` | 每個網站抓幾筆、要不要抓完整職缺描述 |
| `filters.exclude_title_words` | 職稱含這些字就排除 |
| `google_sheet_id` | 要寫入的 Google Sheet（網址 `/d/` 和 `/edit` 之間那段） |
| `claude.backend` | `claude_code` 用訂閱；`api` 用 API key |
| `claude.cli_model` | `sonnet`（較省訂閱額度）或 `opus` |
| `claude.batch_size` | 一次送幾筆職缺給 Claude 評分 |
| `claude.max_jobs_scored` | 每次最多評幾筆（預設 300），超過的照樣寫進 Sheet，只是沒有分數 |
| `claude.drop_score_at_or_below` | 評分在這個分數（含）以下的職缺移到 Filtered out（預設 3） |
| `claude.tailor_min_score` | 幾分以上才產生 cover letter |
| `resume.cli_model` | 客製履歷用的模型：`opus`（預設）或 `sonnet` |
| `resume.max_per_run` | 每次最多產生幾份履歷（預設 10） |

改完 push 到 `main`，下一次執行就會套用。

## 要注意的事

- **LinkedIn 429**：LinkedIn 只要請求太快就會回 429。所以 LinkedIn 每個州只搜一次（把所有職稱用 OR 合併），搜尋時不抓職缺描述；等州別和職稱篩選完，才慢慢抓剩下職缺的頁面（每頁間隔 5 到 9 秒）。遇到 429 會等 70 秒再試，連續 3 次被擋就先停，其餘的明天再說。這些都可以在 `config.yaml` 的 `sites.linkedin` 調整。
- **網站封鎖**：GitHub Actions 的主機是資料中心 IP，LinkedIn 和 Glassdoor 可能會擋，Indeed 通常沒問題。Actions 的執行紀錄會顯示每個搜尋抓到幾筆。如果某個網站一直是 0 筆，可以在 `config.yaml` 拿掉它，或設定 `JOBSPY_PROXIES`。
- **訂閱額度**：每 8 筆職缺用一次 Claude，再加上每封 cover letter 各一次。每天 60 筆大約是 10 到 20 次呼叫，會用掉訂閱的額度。
- **Actions 執行時間**：一次可能要 30 到 60 分鐘，主要是在抓職缺描述。公開 repo 不限時數；私人 repo 每月有 2,000 分鐘免費額度。不夠用的話，減少 `search_terms` 或 `results_wanted`。
- **排程暫停**：公開 repo 如果 60 天沒有任何 commit，GitHub 會自動停用排程，到 Actions 頁面重新啟用即可。
- **H-1B 判斷**：先用規則找「不提供 sponsorship」等字眼，Claude 再判斷一次。多數職缺不會寫，所以 `unclear` 很常見，不代表不能投。想更準，可以下載 USCIS 的 [H-1B Employer Data Hub](https://www.uscis.gov/tools/reports-and-studies/h-1b-employer-data-hub) 資料，放進 `job_hunter/h1b/`，再設定 `h1b_sponsor_files`。這個資料夾預設被 `.gitignore` 排除，要在雲端使用，得把這行從 `.gitignore` 拿掉再 commit。

## 在自己電腦上執行（選用）

```bash
cd job_hunter
pip install -r requirements.txt
# 履歷放成 job_hunter/resume.pdf；要寫入 Sheet 的話，先設定 GOOGLE_SERVICE_ACCOUNT_JSON 環境變數
python hunt.py --no-claude   # 只抓和篩選
python hunt.py               # 完整流程（需要先執行過 `claude` 並登入）
```

本機執行也會輸出 `output/日期/jobs_日期.xlsx` 和 `letters/`。
