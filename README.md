# PartSouq 型錄與 NHTSA 官方資料同步器

這是 Python 3.12+ 的 evidence-first 資料管線。PartSouq 與 NHTSA 的 CLI 都直接寫入本機 MySQL 8。內部仍保留 SQLite adapter，僅供舊 archive snapshot 的一次性唯讀遷移與隔離單元測試；它不是 production backend。

目前已確認的邊界：

- PartSouq live：標準 HTTP 與 Playwright 的最新 canary 仍收到 Cloudflare challenge。一次 NoDriver 本機 run 曾取得 56 個 200 response 並寫入 MySQL，之後仍遇 challenge，尚有 140,521 個 URL 未完成；因此只能算 partial live，不能稱為現行全量。正式月排程不使用 stealth／指紋偽裝／proxy／clearance 搬運，而是標準 headless Playwright 單次 canary；challenge raw-first 入庫後，同月份不再反覆探測。
- PartSouq archive：可從 Common Crawl WARC、Wayback CDX 及合法持有的 HTML 取得真實歷史型錄。全部標為 `historical_archive`，fitment 固定是 unverified，不會冒充 current。
- NHTSA：已實作官方 bulk files、CSSI 與 vPIC allowlist endpoint 的完整 raw artifact、版本、lineage 與 current pointer 流程。

目前的資料筆數、站方後台需求矩陣、schema、E2E 結果與未完成範圍請看 [站方後台驗收報告](docs/station-backend-acceptance-2026-08-10.md)。原始規格與較早的 schema 驗收紀錄保留在 [需求與驗收文件](docs/requirements-notes-schema.md)。

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

本機預設 root 是 `root/root`；應用程式使用 `partsouq/partsouq-local`、`nhtsa/nhtsa-local`，後台使用唯讀來源帳號 `partsouq_admin/partsouq-admin-local`。這些預設密碼只供綁在 `127.0.0.1` 的開發環境。正式環境必須覆寫 `.env`；不得提交 `.env`、raw artifact、DB volume、archive index、備份或匯出資料。

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
  --concurrency 1 --delay 30 --retry-count 1 \
  --robots-policy require \
  --user-agent "$PARTSOUQ_USER_AGENT"
```

`0` 代表 unlimited。同一個 `--run-id` 重跑會延續 MySQL persistent queue；完成項目不會重抓，過期 lease 會回收。Queue claim 使用 `FOR UPDATE SKIP LOCKED`，完成寫入帶 worker 與 fencing token，舊 worker 無法覆寫新租約結果。

### Browser transports

正式 `browser` transport 使用標準 Playwright，只處理一般瀏覽器 JavaScript。偵測到未解除的 challenge 後會啟動共用熔斷器：

```bash
partsouq-crawler crawl-all \
  --run-id partsouq-browser-live \
  --seed-url 'https://partsouq.com/en/catalog/genuine' \
  --max-pages 10 --max-depth 0 \
  --concurrency 1 --delay 30 --retry-count 0 \
  --robots-policy require \
  --transport browser \
  --browser-headless
```

研究期間也分別測過 Patchright、SeleniumBase 與 NoDriver；它們不是正式月排程依賴，也不會疊在同一個 browser session。NoDriver 的部分成功只證明該次本機 session 可讀到部分頁面，不能外推成 Linux server 或全量保證；其 AGPL-3.0 授權也需另行審查。所有 transport 都禁止人工 CAPTCHA、stealth 指紋偽裝、代理輪替、付費 bypass API 或把 `cf_clearance` 搬到 HTTP client。

正式月排程固定單 worker，主頁導航至少間隔 30 秒（最多 120 次／小時），一般暫時錯誤最多重試 1 次；429 至少冷卻 6 小時。Cloudflare challenge 不重試：保存 raw 後立即停止 PartSouq source，後續 recovery 只續 NHTSA 等未完成來源，同月份不再送第二個 PartSouq canary。

## 每月無人值守排程

正式範本在 `deploy/systemd/`。首次執行時間是台北時間每月 1 日 01:00；1 日 13:00 至 3 日 13:00 每 12 小時做冪等恢復檢查，處理斷電、主機重開或 process hard kill。已完成或仍持有 lease 時會快速 no-op。

```bash
sudo install -m 0644 deploy/systemd/partsouq-monthly-sync.service /etc/systemd/system/
sudo install -m 0644 deploy/systemd/partsouq-monthly-sync.timer /etc/systemd/system/
sudo install -m 0600 deploy/systemd/monthly.env.example /etc/partsouq-crawler/monthly.env
sudo systemctl daemon-reload
sudo systemctl enable --now partsouq-monthly-sync.timer
systemctl list-timers partsouq-monthly-sync.timer
```

排程依序執行 NHTSA bulk、NHTSA API、站方資料投影／VIN 佇列／對帳、PartSouq live。NHTSA bulk、API 與 VIN batch request 都至少間隔 1 秒。NHTSA API 遇到 429、408 或暫時性 5xx 時最多低頻重試 3 次，至少退避 30／60／120 秒並優先遵守更長的 `Retry-After`；每次重試也計入 500-request safety budget 與結構化 log。`monthly_sync_runs` 以 `YYYY-MM` 唯一，避免同月重複；lease、heartbeat 與 fencing token 避免雙主。完成的 source 在後續 attempt 會 skip，只續未完成來源。`crawl_policy`、response、retry、429 與 challenge 熔斷事件會同時進 journald 與 `monthly_sync_events`。

```bash
partsouq-crawler monthly-status --period 2026-08 --event-limit 200
journalctl -u partsouq-monthly-sync.service --since '2026-08-01'
```

正式 host 使用標準 headless Playwright、非 root service user 與瀏覽器 sandbox。排程腳本拒絕 headed monthly browser，避免無人值守時依賴 Xvfb／人工桌面。若 canary 是 challenge，當月狀態會是 `completed_with_gaps`，NHTSA 仍可完成；不得把 process 結束誤寫成 PartSouq live 全量成功。

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
partsouq-crawler reparse --batch-size 250
partsouq-crawler repair-taxonomy
partsouq-crawler repair-taxonomy --apply
partsouq-crawler export --output output/fitments.csv
partsouq-crawler export \
  --output output/historical.jsonl \
  --include-unverified-fitments
```

全量 reparse 以 response ID keyset 每批 250 筆讀 raw body，不會一次載入全部壓縮內容。`repair-taxonomy` 預設只預覽 parser v2 誤收的 `Genuine Parts Catalogs > ...` 導覽節點；只有 `--apply` 才會在確認沒有 diagram 連結後刪除舊 normalized 節點，raw response 不受影響。Export 每個 normalized record 只選最新版 parser 的一筆 provenance，並以 record ID keyset 每批 1,000 筆輸出；完整來源歷程仍保留在 DB 與後台 detail。預設遮蔽 URL 裡的 `ssd`、VIN 與 17 碼 VIN 值；只有確定檔案留在本機時，才可明確加 `--include-sensitive-source-urls`。

`strict_complete=true` 必須同時滿足：queue 耗盡、無 failed／challenged／robots skip／parse failure、provenance 完整、foreign key 完整。Archive run 永遠會明確標示不是 current/full。

## 站方 CRUD／對帳後台

```bash
export PARTSOUQ_ADMIN_SECRET_KEY='replace-with-a-local-random-secret'
partsouq-admin
open http://127.0.0.1:8086/
```

後台可瀏覽、搜尋、人工建立、更新、停用及恢復以下十種資料：車型、零件分類、分解圖、料號、零件出現紀錄、適用關係、零件中英／俗稱、VIN 車型、VIN 適用零件、對帳案件。也可將 17 碼 VIN 加入持久解碼佇列；`station-sync` 或月排程會呼叫 NHTSA vPIC、保存 raw JSON 與 SHA-256，再建立 VIN 車型資料。VIN 正規化欄位包含品牌、型號、年份、Trim、引擎形式、汽缸數、排氣量、引擎型號／製造商、燃料、驅動方式、變速箱與生產國；其他官方欄位仍保留在 raw JSON。`/monitoring` 是唯讀爬蟲監控頁，顯示 monthly source 狀態、PartSouq queue／response、429／challenge 與最近 100 筆 DB event。

Crawler source tables 對 `partsouq_admin` 只有 `SELECT`；所有人工異動寫入 overlay 與 append-only audit event，不會改掉 raw source fact。VIN 對應到 PartSouq 車型必須由站方明確選定 `partsouq_vehicle_configuration_id`，不做模糊字串自動配對；只有完成連結後才投影 VIN 適用零件，且預設仍是未驗證。

只在本機使用時可維持 loopback、無登入。若綁定非 loopback，程式會強制要求帳密、固定 secret 與 secure cookie，否則拒絕啟動。正式環境請放在 HTTPS reverse proxy 後，以 Gunicorn 啟動：

```bash
export PARTSOUQ_ADMIN_HOST=127.0.0.1
export PARTSOUQ_ADMIN_USERNAME='replace-me'
export PARTSOUQ_ADMIN_PASSWORD='replace-me'
export PARTSOUQ_ADMIN_SECRET_KEY='replace-with-random-secret'
export PARTSOUQ_ADMIN_SECURE_COOKIE=1
.venv/bin/gunicorn --bind 127.0.0.1:8086 'partsouq_crawler.admin.app:create_app()'
```

可直接安裝 `deploy/systemd/partsouq-admin.service` 與 `deploy/systemd/admin.env.example`。列表固定 3 條 SQL，detail 固定 4 條 SQL，監控頁固定 3 條聚合 SQL；來源 URL 裡的 `ssd`／VIN 在畫面預設遮蔽。

列表使用 keyset 分頁與批次載入。Query tag／fingerprint 由測試固定，資料量或 fanout 增加時不會形成 N+1。

## NHTSA

固定官方資料範圍：Safety Ratings、Recalls、Investigations、Complaints、Manufacturer Communications summary/detail、CSSI、vPIC makes/models/manufacturers/variables/variable values。另支援對「站方提供的 VIN」呼叫官方 vPIC batch decode；NHTSA 沒有可供列舉全球所有 VIN 的 bulk universe，因此 VIN 必須來自訂單、匯入檔或後台佇列。

```bash
partsouq-crawler nhtsa-sync-bulk --scope all --run-id nhtsa-bulk-full
partsouq-crawler nhtsa-sync-api --scope all --run-id nhtsa-api-full
partsouq-crawler station-sync --run-id station-2026-08
partsouq-crawler nhtsa-status
```

每個 raw ZIP／CSV／JSON 先以 SHA-256 保存，驗證 ZIP member、CRC、欄位 schema 與逐列解析，再批次寫 MySQL。只有選定 scope 的全部 artifact 都成功且拒絕列為 0，才會原子更新 current pointers。一般 NHTSA sync 仍拒絕任意 API URL 與 VIN 枚舉；VIN decode 只由固定 vPIC batch endpoint、17 碼驗證與最多 50 筆批次的 station queue 執行。

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
