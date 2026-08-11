# Librovia・自由書閣

Librovia 是一套自架的個人電子書管理工具，將 Readmoo 讀墨與
Rakuten Kobo 的已購書櫃、待購清單及書籍資訊集中在同一個介面。

> 此專案仍在開發中，目前以前端內建的單一測試使用者運作，尚未提供完整的
> 多使用者登入與權限系統。請勿直接暴露在公開網路。

## 功能

- 整合 Readmoo 與 Kobo 已購書櫃
- 匯入、搜尋及依平台／分類篩選藏書
- 集中管理兩個平台的待購清單
- 顯示平台登入憑證及同步狀態
- 透過 Playwright 開啟可互動的登入瀏覽器並保存 session
- 從博客來、Readmoo、國家圖書館、Google Books 與 Open Library
  交叉補齊書名、作者、封面及分類
- 匯入時重新嘗試補齊「未知作者」、「未分類」或缺少封面的既有書籍
- metadata 來源失敗時提供重試、冷卻與候選資料評分
- 使用 SQLite 儲存藏書、購買紀錄與待購清單

## 技術架構

| 元件 | 技術 |
| --- | --- |
| 前端 | React、Vite、Tailwind CSS、Axios |
| 後端 | FastAPI、SQLModel、APScheduler |
| 瀏覽器自動化 | Playwright |
| 資料庫 | SQLite（WAL 模式） |
| Metadata | Google Books、Open Library、國家圖書館及書店資料 |

```text
Browser
   │
   ├── React / Vite (:3000)
   │
   └── FastAPI (:8000)
          ├── SQLite
          ├── Metadata providers
          └── Playwright ── Readmoo / Kobo
```

## 開始使用

### 系統需求

- Python 3.11 或更新版本
- Node.js 20.19 或更新版本（亦支援 Node.js 22.13+／24+）
- npm
- macOS、Linux，或其他 Playwright 支援的環境

### 1. 取得專案

```bash
git clone https://github.com/uni-crys/Librovia.git
cd Librovia
```

### 2. 設定後端

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt
playwright install chromium
cp backend/.env.example backend/.env
```

編輯 `backend/.env`：

```dotenv
# 選填；未設定時仍會嘗試其他 metadata 來源
GOOGLE_BOOKS_API_KEY=

# 本機同步代理與後端共用的長隨機字串
READMOO_SYNC_TOKEN=

# 選填：使用已安裝的正式版 Chrome
READMOO_BROWSER_CHANNEL=chrome

# 選填：只套用於 Readmoo 瀏覽器，例如家用網路的 SOCKS proxy
READMOO_BROWSER_PROXY=
```

啟動 API：

```bash
cd backend
uvicorn main:app --reload --host 127.0.0.1 --port 8000
```

API 啟動後可開啟：

- Process liveness：<http://127.0.0.1:8000/health>
- SQLite readiness：<http://127.0.0.1:8000/ready>
- Swagger API 文件：<http://127.0.0.1:8000/docs>

### 3. 設定前端

另開一個終端機：

```bash
cd fronted
npm ci
npm run dev
```

前往 <http://localhost:3000>。

如後端不在 `http://localhost:8000`，請在啟動前端時指定：

```bash
VITE_API_BASE_URL=https://your-private-api.example npm run dev
```

> 專案目前的前端目錄名稱是 `fronted/`，請依照實際名稱輸入指令。

## 基本操作

1. 進入「同步狀態」。
2. 為 Readmoo 與 Kobo 分別執行登入。
3. 在 Playwright 開啟的瀏覽器中，於三分鐘內完成平台登入。
4. 回到「藏書間」執行書櫃匯入。
5. 系統會加入新書，並重新補抓既有但資訊不完整的書籍。

登入資訊會保存在 `backend/user_profiles/`，資料庫位於
`backend/data/ebooks.db`。兩者均已被 Git 忽略，請勿手動提交。

## 測試

執行後端測試：

```bash
cd backend
python -m unittest discover -s tests
```

驗證前端可建置：

```bash
cd fronted
npm ci
npm run lint
npm run build
```

## Production readiness

### 健康檢查

- `GET /health` 是 liveness probe，只表示 API process 可以回應。它不查詢外部
  平台或資料庫，因此適合用來判斷是否需要重啟 process。
- `GET /ready` 是 readiness probe，目前會執行 SQLite `SELECT 1`。資料庫不可用時
  回傳 HTTP 503，可用於停止將流量導向尚未就緒的 instance。

### 結構化 log

後端以每行一筆 JSON 輸出 log，HTTP request 包含 `request_id`、`method`、
`path`、`status_code` 與 `duration_ms`；同步工作另包含 `job_id`、`operation`、
`platform`、`duration_ms` 與 `result`。若 client 傳入格式安全的
`X-Request-ID`，回應會沿用並回傳該值，否則由伺服器產生 UUID。

log 刻意不記錄 query string、request/response body、Cookie、Authorization、
同步 token、使用者 ID 或書籍內容。新增 log 時也應維持這項界線；錯誤訊息不可
直接帶入憑證或遠端回應全文。

### CI

`.github/workflows/ci.yml` 在每次 push 與 pull request 執行：

- Python 3.11 安裝 pip cache 後執行後端 `unittest`
- Node.js 24 安裝 npm cache 後執行 `npm ci`、安全稽核、ESLint 與 Vite build

同一 branch/ref 的舊 CI run 會由 concurrency 設定取消，避免重複消耗資源。
Dependabot 每週檢查 Python/npm 依賴，每月檢查 GitHub Actions。

### 正式環境設定

部署時將 `ENVIRONMENT=production`。後端會在啟動時要求至少 32 字元的
`READMOO_SYNC_TOKEN`，並拒絕 wildcard、localhost 或空白的 `CORS_ORIGINS`；
設定不安全時會 fail fast。資料庫預設固定在 `backend/data/ebooks.db`，可透過
`LIBROVIA_DATABASE_PATH` 指定 persistent volume 上的絕對路徑。

### SQLite migration 與備份

API 啟動時會執行 Alembic migration，不再以 `create_all()` 取代 schema 版本管理。
手動操作需在 API 停止或確認沒有長時間寫入工作時，於 `backend/` 執行：

```bash
# 升級至最新版 schema
python scripts/manage_database.py migrate

# 使用 SQLite online backup API 建立一致性備份，保留最近 14 份
python scripts/manage_database.py backup \
  --output-dir /secure/librovia-backups \
  --keep 14

# 驗證任一備份
python scripts/manage_database.py verify \
  /secure/librovia-backups/librovia-YYYYMMDDTHHMMSSZ.sqlite3

# 還原前先停止 API；工具會先保留一份 pre-restore database
python scripts/manage_database.py restore \
  /secure/librovia-backups/librovia-YYYYMMDDTHHMMSSZ.sqlite3 \
  --confirm
```

備份會先執行 `PRAGMA integrity_check`、以暫存檔原子完成並設定為 owner-only
權限，輸出 SHA-256 供異地複製後驗證。備份目錄不可放在 Git repository，且應
加密並定期執行實際還原演練。Alembic baseline 不提供 destructive downgrade；
需要回退時應還原已驗證的備份。

### 工作佇列評估

書櫃匯入採兩階段流程：先抓完 Readmoo/Kobo 書櫃並將原始書名、封面與購買關聯
寫入 SQLite，立即讓前端顯示；書櫃 API／列表仍缺少的 metadata 再寫入 `metadata_jobs`，
由 response 後的 `BackgroundTasks` 立即喚醒處理器，APScheduler 每分鐘也會
接手尚未完成的工作。`GET /library/metadata-status?user_id=...` 可查詢
`pending`、`running`、`completed`、`failed` 與 `manual_review` 數量。
失敗工作從 24 小時開始 exponential backoff，單筆最多自動嘗試三次；達上限後
可由書櫃頁手動重試，或呼叫 `POST /library/metadata-retry?user_id=...`。

Readmoo 匯入器會監聽登入後閱讀器本身使用的官方書櫃 API，以平台書籍 ID 合併
ISBN、作者、封面與分類。平台資料先寫入避免前端空白，但 API 欄位缺漏的新書與
原本資料不完整的書會進入共用 pipeline，不在平台同步期間逐本開啟商品頁。
可靠資料的查找順序是國圖、Books.com.tw、Readmoo、Kobo、Open Library、
Google Books；Kobo 候選僅在同步取得該商品資料時存在。同步紀錄不包含 cookie、
token、書名或其他使用者內容。

Kobo 書櫃列表的 UUID 僅保存為 `platform_book_id`；列表封面會直接採用，缺少的
ISBN、作者、封面或分類再交由共用 metadata pipeline。平台 fallback 以 Readmoo
優先於 Kobo；Kobo
不覆蓋已有分類，即使先同步 Kobo，後續 Readmoo 同步仍可更新成 Readmoo
分類，而 pipeline 的較高優先序可靠結果仍可覆蓋兩者。完整結果由 SQLite 的
`Book` 與 `Purchase` 持久保存；共用 job 的失敗狀態、嘗試次數與下次可重試
時間也會寫入 SQLite。

現階段 Librovia 是單機、低頻率同步，SQLite durable job table 加上 FastAPI
`BackgroundTasks` 與 APScheduler 已足夠，暫不引入 Redis。job 本身可跨 process
重啟保留，排程器會重新取得中斷在 `running` 的項目；但執行機制仍在 API process
內，沒有跨 instance 鎖、完整的 timeout／取消、dead-letter queue 或獨立 worker
隔離。CPU/記憶體密集的 Playwright 工作也會和 API 競爭資源。APScheduler 若
同時啟動多個 API instance，可能在每個 instance 重複排程；`BackgroundTasks`
也不適合需要嚴格保證完成或長時間執行的工作。

出現下列任一情況時，應評估遷移到 Redis-backed queue（例如 RQ、Dramatiq 或
Celery），並讓 worker 與 API 分離：

- 需要水平擴充到多個 API instance，且必須避免重複同步
- 工作在 deploy／crash 後仍須保留，或需要 retry、timeout、取消與 dead-letter
- 同步量或排隊時間需要明確的 backpressure、優先序與營運指標
- Playwright 工作開始影響 API latency／記憶體，或需要獨立擴縮 worker


## 專案結構

```text
Librovia/
├── backend/
│   ├── app/
│   │   ├── api/          # FastAPI 路由
│   │   ├── services/     # 平台同步、登入與 metadata pipeline
│   │   ├── database.py
│   │   └── models.py
│   ├── scripts/          # 本機登入及同步輔助工具
│   ├── tests/
│   ├── main.py
│   └── requirements.txt
├── fronted/
│   └── src/              # React 前端
└── README.md
```

## 注意事項

- 平台網站改版後，選擇器或登入流程可能需要更新。
- 請遵守 Readmoo、Kobo 與各 metadata 來源的服務條款及使用限制。
- 請勿提交 `.env`、Cookie、Playwright storage state、資料庫或 API key。
- 大量同步前建議先備份資料庫，並避免短時間反覆觸發平台登入。
