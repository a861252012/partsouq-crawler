# PartSouq 型錄與 NHTSA 官方資料同步器

這是 Python 3.12+ 的 evidence-first 資料管線。PartSouq 與 NHTSA 的 CLI 都直接寫入本機 MySQL 8。內部仍保留 SQLite adapter，僅供舊 archive snapshot 的一次性唯讀遷移與隔離單元測試；它不是 production backend。

目前已確認的邊界：

- PartSouq live：標準 HTTP 與 headed Brave／Playwright 都會收到 Cloudflare challenge。系統會先保存 raw response，再把 run 標成 `blocked` 並停止；不會把 challenge HTML 當型錄，也不會使用 stealth、CAPTCHA solver、代理或 cookie 搬運。
- PartSouq archive：可從 Common Crawl WARC、Wayback CDX 及合法持有的 HTML 取得真實歷史型錄。全部標為 `historical_archive`，fitment 固定是 unverified，不會冒充 current。
- NHTSA：已實作官方 bulk files、CSSI 與 vPIC allowlist endpoint 的完整 raw artifact、版本、lineage 與 current pointer 流程。

資料表、最新實測數量及未完成範圍請看 [需求與驗收文件](docs/requirements-notes-schema.md)。

## 安裝

```bash
uv sync --extra dev --frozen
source .venv/bin/activate
partsouq-crawler --help
```

## 啟動本機 MySQL

Compose 綁定 `127.0.0.1:3308`，建立 `partsouq`、`partsouq_test`、`nhtsa` 及低權限後台帳號：

```bash
cp -n .env.example .env
docker compose -f compose.nhtsa.yml up -d
```

預設密碼只供本機開發。正式環境必須覆寫 `.env`；不得提交 `.env`、raw artifact、DB volume、archive index 或匯出資料。

## PartSouq live crawler

設定固定且可聯絡的 User-Agent：

```bash
export PARTSOUQ_USER_AGENT='YourCrawler/1.0 (contact: you@example.com)'
scripts/run_full_catalog.sh
```

等價命令：

```bash
partsouq-crawler crawl-all \
  --run-id partsouq-genuine-full \
  --seed-url 'https://partsouq.com/en/catalog/genuine' \
  --max-pages 0 --max-depth 0 \
  --concurrency 1 --delay 5 \
  --robots-policy require \
  --user-agent "$PARTSOUQ_USER_AGENT"
```

`0` 代表 unlimited。同一個 `--run-id` 重跑會延續 MySQL persistent queue；完成項目不會重抓，過期 lease 會回收。Queue claim 使用 `FOR UPDATE SKIP LOCKED`，完成寫入帶 worker 與 fencing token，舊 worker 無法覆寫新租約結果。

### 標準 Brave／Playwright transport

這條 transport 只處理一般瀏覽器 JavaScript，不隱藏自動化身分。任一 driver 偵測到 challenge 後會啟動共用熔斷器：

```bash
partsouq-crawler crawl-all \
  --run-id partsouq-browser-live \
  --seed-url 'https://partsouq.com/en/catalog/genuine' \
  --max-pages 10 --max-depth 0 \
  --concurrency 1 --delay 5 --retry-count 0 \
  --robots-policy require \
  --transport browser \
  --browser-executable '/Applications/Brave Browser.app/Contents/MacOS/Brave Browser'
```

Playwright 與 Selenium 是替代 driver，不需要疊加。Selenium 不能讓已出現的 Cloudflare challenge 變成一般內容；`selenium-stealth`、`undetected-chromedriver`、`pyppeteer-stealth` 等套件的目的包含隱藏自動化或規避偵測，因此本專案不安裝、不測試。

## PartSouq 歷史 archive

Common Crawl 與 Wayback 都先把每筆 index item 寫進 MySQL queue，再下載、保存 raw response／SHA-256／archive 座標、解析與寫 provenance。中斷後用相同命令續跑。

```bash
partsouq-crawler common-crawl-import \
  --run-id partsouq-common-crawl-full \
  --index /path/to/CC-MAIN-index.ndjson \
  --delay 0.25 --timeout 60

partsouq-crawler wayback-import \
  --run-id partsouq-wayback-full \
  --index /path/to/wayback-page-0.json \
  --index /path/to/wayback-page-1.json \
  --delay 2 --timeout 60
```

合法持有的單一 HTML：

```bash
partsouq-crawler archive-import \
  --run-id owned-capture-20260810 \
  --input /path/to/capture.html \
  --source-url 'https://partsouq.com/en/catalog/genuine/diagram?...' \
  --archive-source owned_export \
  --captured-at '2026-08-10T00:00:00Z'
```

舊 SQLite archive snapshot 可做一次性、可重入的 raw replay：

```bash
partsouq-crawler sqlite-archive-migrate \
  --source-sqlite /path/to/legacy-archive.sqlite3 \
  --run-id legacy-archive-migration \
  --batch-size 500
```

遷移會對封閉 snapshot 計算 SHA-256、重算每個 raw body hash、保留 capture provenance、用目前 parser 重播，再檢查 body 差集、provenance、orphan 與 foreign key。SQLite 不會成為執行中的 queue 或資料庫。

## 狀態、raw response、reparse、export

```bash
partsouq-crawler crawl-status --run-id partsouq-genuine-full
partsouq-crawler db-status
partsouq-crawler problem-urls --run-id partsouq-genuine-full --format jsonl

partsouq-crawler dump-response --response-id 1 --output output/response-1.html
partsouq-crawler reparse --response-id 1
partsouq-crawler export --output output/fitments.csv
partsouq-crawler export \
  --output output/historical.jsonl \
  --include-unverified-fitments
```

Export 使用 `(record_id, provenance_id)` keyset，每批 1,000 筆，不會一次載入全表。預設遮蔽 URL 裡的 `ssd`、VIN 與 17 碼 VIN 值；只有確定檔案留在本機時，才可明確加 `--include-sensitive-source-urls`。

`strict_complete=true` 必須同時滿足：queue 耗盡、無 failed／challenged／robots skip／parse failure、provenance 完整、foreign key 完整。Archive run 永遠會明確標示不是 current/full。

## 本機 CRUD 後台

```bash
export PARTSOUQ_ADMIN_SECRET_KEY='replace-with-a-local-random-secret'
partsouq-admin
open http://127.0.0.1:8086/
```

後台可瀏覽、搜尋、人工建立、更新、停用及恢復車型、diagram、料號、occurrence 與 fitment。Crawler source tables 對 `partsouq_admin` 只有 `SELECT`；所有人工異動寫入 overlay 與 append-only audit event，不會改掉 raw source fact。

後台沒有對外登入機制，因此啟動程式只接受 `127.0.0.1`、`localhost` 或 `::1`；不得改成對外網卡。列表固定 3 條 SQL，detail 固定 4 條 SQL，來源 URL 裡的 `ssd`／VIN 在畫面預設遮蔽。

列表使用 keyset 分頁與批次載入。Query tag／fingerprint 由測試固定，資料量或 fanout 增加時不會形成 N+1。

## NHTSA

目前範圍：Safety Ratings、Recalls、Investigations、Complaints、Manufacturer Communications summary/detail、CSSI、vPIC makes/models/manufacturers/variables/variable values。

```bash
partsouq-crawler nhtsa-sync-bulk --scope all --run-id nhtsa-bulk-full
partsouq-crawler nhtsa-sync-api --scope all --run-id nhtsa-api-full
partsouq-crawler nhtsa-status
```

每個 raw ZIP／CSV／JSON 先以 SHA-256 保存，驗證 ZIP member、CRC、欄位 schema 與逐列解析，再批次寫 MySQL。只有選定 scope 的全部 artifact 都成功且拒絕列為 0，才會原子更新 current pointers。VIN decode、VIN 枚舉與任意 API URL會被 policy 拒絕。

## 備份與驗收

```bash
scripts/backup_database.sh

NHTSA_TEST_MYSQL=1 PARTSOUQ_TEST_MYSQL=1 python -m pytest
ruff check .
ruff format --check .
mypy src
git diff --check
```

備份使用 `mysqldump --single-transaction --quick --hex-blob`，另寫 SHA-256。備份與匯出可能含敏感 query token，只能留在 Git ignore 的 `output/`。

發布 GitHub 前，必須確認 remote owner、SSH welcome 與 Brave 目前登入帳號三者都是 `a861252012`；禁止使用 Bibian 公司帳號或憑證。
