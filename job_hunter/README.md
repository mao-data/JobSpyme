# Job Hunter

每天自動找美國職缺、排除不提供 H-1B 擔保的職缺，再用 Claude 依照你的履歷打分數，最後輸出一份 Excel 清單。分數高的職缺還會附上客製化履歷重點和 cover letter。

**它不會自動送出應徵**，送出前由你自己看過、自己按。

```
JobSpy 抓職缺 (Indeed / LinkedIn / ZipRecruiter / Glassdoor)
  → 去除重複、去掉之前看過的
  → 規則篩選：州別、職稱（排除 Senior/Manager…）、「不提供 sponsorship」
  → Claude 評分：契合度 1-10、再判斷一次 sponsorship、所需年資、缺少的技能
  → 高分職缺：產生客製化 resume bullets + cover letter
  → output/日期/jobs_日期.xlsx
```

## 安裝

需要 Python 3.10 以上。

```bash
git clone https://github.com/mao-data/JobSpyme
cd JobSpyme/job_hunter
pip install -r requirements.txt
```

1. 把你的履歷放到 `job_hunter/resume.pdf`。`.gitignore` 已經排除它，不會被推到 GitHub。
2. 設定 Claude API key（到 https://platform.claude.com 申請）：
   ```bash
   export ANTHROPIC_API_KEY=sk-ant-...        # macOS / Linux
   setx ANTHROPIC_API_KEY sk-ant-...          # Windows（設完要重開終端機）
   ```

## 使用

```bash
python hunt.py              # 完整流程
python hunt.py --no-claude  # 只抓和篩選，不呼叫 Claude（不花錢，適合先測試）
```

結果在 `output/YYYY-MM-DD/`：

- `jobs_YYYY-MM-DD.xlsx`
  - **Shortlist** 分頁：通過篩選的職缺，依分數排序，`job_url` 可以直接點
  - **Filtered out** 分頁：被排除的職缺和原因，可以檢查有沒有誤判
- `letters/`：分數 ≥ 8 的職缺各一份 `.md`，內有客製化 resume bullets 和 cover letter

看過的職缺會記在 `data/seen_jobs.csv`，下次執行只會出現新職缺。想重看全部就刪掉這個檔案。

## 設定（config.yaml）

| 設定 | 說明 |
|---|---|
| `search_terms` | 搜尋的職稱，預設依你的履歷挑了 7 個 |
| `locations` | 目標州：California、Arizona、Texas、New Jersey |
| `hours_old` | 只要多少小時內刊登的；每天跑就設 24 |
| `sites` | 每個網站抓幾筆、要不要抓完整職缺描述 |
| `filters.exclude_title_words` | 職稱含這些字就排除 |
| `filters.require_sponsorship` | 排除寫明不提供 sponsorship 的職缺 |
| `claude.tailor_min_score` | 幾分以上才產生 cover letter |
| `claude.max_jobs_scored` | 每次最多送幾筆給 Claude，用來控制費用 |

## H-1B 擔保怎麼判斷

會經過三層判斷：

1. **規則篩選**：職缺描述出現 "unable to sponsor"、"without the need for sponsorship"、"must be a U.S. citizen"、"security clearance" 等字眼就直接排除。
2. **Claude 再判斷**：Claude 會讀完整描述，回報 `sponsors` / `unclear` / `no_sponsorship`，並附上原文依據。`no_sponsorship` 的職缺會移到 Filtered out。
3. **過去的 H-1B 紀錄（選用）**：多數職缺不會寫要不要 sponsor，這時「公司過去有沒有申請過 H-1B」是最好的參考。
   - 到 USCIS 的 [H-1B Employer Data Hub](https://www.uscis.gov/tools/reports-and-studies/h-1b-employer-data-hub) 下載近一兩年的資料檔，放到 `job_hunter/h1b/`
   - 在 `config.yaml` 填上路徑：
     ```yaml
     h1b_sponsor_files: [h1b/fy2025.csv, h1b/fy2024.csv]
     ```
   - Excel 的 `h1b_approvals` 欄會顯示該公司過去的核准數。公司名稱是用模糊比對，少數公司可能對不到。

`unclear` 不代表不能投。沒寫的職缺很多，配合 `h1b_approvals` 一起看就好。

## 費用

預設用 Claude Opus 5.5。每筆評分大約 3,000 個輸入 token、幾百個輸出 token。以每次 150 筆加 10 封 cover letter 估算，一次大約 **2–5 美元**。實際金額每次執行結束會印出來。想省錢可以：

- 把 `max_jobs_scored` 調低
- 把 `claude.model` 改成 `claude-sonnet-5-5`，或更便宜的 `claude-haiku-5-5`

## 常見問題

- **一直抓到 0 筆、出現 429 或 403**：被網站擋了。可以減少 `search_terms` 或 `results_wanted`、拉長 `pause_seconds`、晚點再跑，或設定 `proxies`。LinkedIn 最容易被擋。
- **每天自動執行**：
  - macOS / Linux 用 `crontab -e`，加一行 `0 8 * * * cd /path/to/JobSpyme/job_hunter && python hunt.py`
  - Windows 用「工作排程器」
- **想加減職稱或州**：直接改 `config.yaml`，不用改程式。
