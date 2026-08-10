# PartSouq 型錄與 NHTSA 官方資料同步器

這是 Python 3.12+ 的 evidence-first 資料管線。PartSouq 與 NHTSA 的 CLI 都直接寫入本機 MySQL 8。內部仍保留 SQLite adapter，僅供舊 archive snapshot 的一次性唯讀遷移與隔離單元測試；它不是 production backend。

目前已確認的邊界：

- PartSouq live：標準 HTTP、Playwright 與 Patchright 實測仍收到 Cloudflare challenge；headed Google Chrome + NoDriver 在本機 macOS 可無人工操作自動轉成 200，且已抓取、解析、寫入 MySQL。相同程式在 Linux Docker + Xvfb 仍是 403，Docker 開啟 Chrome sandbox 時則無法啟動。因此 server 必須在實際 Linux host 先通過 preflight，不能把本機成功誤寫成任意 server 已保證成功。
- PartSouq archive：可從 Common Crawl WARC、Wayback CDX 及合法持有的 HTML 取得真實歷史型錄。全部標為 `historical_archive`，fitment 固定是 unverified，不會冒充 current。
- NHTSA：已實作官方 bulk files、CSSI 與 vPIC allowlist endpoint 的完整 raw artifact、版本、lineage 與 current pointer 流程。

目前的資料筆數、站方後台需求矩陣、schema、E2E 結果與未完成範圍請看 [站方後台驗收報告](docs/station-backend-acceptance-2026-08-10.md)。原始規格與較早的 schema 驗收紀錄保留在 [需求與驗收文件](docs/requirements-notes-schema.md)。

## 安裝

```bash
uv sync --extra dev --frozen
python3.12 -m venv .venv-browser
.venv-browser/bin/pip install --requirement browser_worker/requirements.txt
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

### Browser transports

Playwright `browser` transport 只處理一般瀏覽器 JavaScript，仍會暴露自動化特徵；NoDriver 則是獨立外部 worker。任一 driver 偵測到未解除的 challenge 後都會啟動共用熔斷器：

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

Playwright、Patchright、SeleniumBase UC 與 NoDriver 是替代方案，不應疊在同一個 browser session。本次實測只有 NoDriver + headed Google Chrome 在本機成功取得 200；SeleniumBase 在 WebDriver 啟動階段逾時，沒有取得網站結果。正式路徑使用獨立 Python 3.12 worker、持久 profile、Chrome 自己的 network stack 與 CDP response body；不把 cookie 搬到 aiohttp，也不使用人工 CAPTCHA、付費 bypass API 或 proxy。

```bash
partsouq-crawler crawl-all \
  --run-id partsouq-nodriver-live \
  --seed-url 'https://partsouq.com/en/catalog/genuine' \
  --max-pages 10 --max-depth 0 --concurrency 1 --delay 5 \
  --robots-policy require --transport nodriver \
  --browser-executable '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' \
  --browser-profile-dir output/partsouq/browser-profile \
  --browser-worker-command '.venv-browser/bin/python browser_worker/worker.py'
```

NoDriver 0.50.3 是 AGPL-3.0；部署或散布前必須自行完成授權審查。偵測到尚未解除的 challenge 時，raw body 仍會先入庫，該次 run 立即停止。月排程只會以同一 profile 每 30 分鐘做最多 3 次 bounded retry，不會密集重試 403。

## 每月無人值守排程

正式範本在 `deploy/systemd/`。首次執行時間是台北時間每月 1 日 01:00；1 日 02:00 至 3 日 23:00 每小時做冪等恢復檢查，處理斷電、主機重開或 process hard kill。已完成或仍持有 lease 時會快速 no-op。

```bash
sudo install -m 0644 deploy/systemd/partsouq-monthly-sync.service /etc/systemd/system/
sudo install -m 0644 deploy/systemd/partsouq-monthly-sync.timer /etc/systemd/system/
sudo install -m 0600 deploy/systemd/monthly.env.example /etc/partsouq-crawler/monthly.env
sudo systemctl daemon-reload
sudo systemctl enable --now partsouq-monthly-sync.timer
systemctl list-timers partsouq-monthly-sync.timer
```

排程依序執行 NHTSA bulk、NHTSA API、站方資料投影／VIN 佇列／對帳、PartSouq live。`monthly_sync_runs` 以 `YYYY-MM` 唯一，避免同月重複；lease、heartbeat 與 fencing token 避免雙主。完成的 source 在後續 attempt 會 skip，只續未完成來源。stdout/stderr 同時進 journald 與 `monthly_sync_events`。

```bash
partsouq-crawler monthly-status --period 2026-08 --event-limit 200
journalctl -u partsouq-monthly-sync.service --since '2026-08-01'
```

正式 host 必須使用 headed Chrome + Xvfb、非 root service user、持久 profile、`PARTSOUQ_BROWSER_SANDBOX=1`。排程腳本會拒絕 true headless 與 `--no-sandbox`。在啟用 timer 前，必須在同一台 host 實跑一頁 `probe` 並確認 HTTP 200；Docker 測試路徑已證明不合格，不是 production fallback。

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

## 站方 CRUD／對帳後台

```bash
export PARTSOUQ_ADMIN_SECRET_KEY='replace-with-a-local-random-secret'
partsouq-admin
open http://127.0.0.1:8086/
```

後台可瀏覽、搜尋、人工建立、更新、停用及恢復以下十種資料：車型、零件分類、分解圖、料號、零件出現紀錄、適用關係、零件中英／俗稱、VIN 車型、VIN 適用零件、對帳案件。也可將 17 碼 VIN 加入持久解碼佇列；`station-sync` 或月排程會呼叫 NHTSA vPIC、保存 raw JSON 與 SHA-256，再建立 VIN 車型資料。

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

可直接安裝 `deploy/systemd/partsouq-admin.service` 與 `deploy/systemd/admin.env.example`。列表固定 3 條 SQL，detail 固定 4 條 SQL，來源 URL 裡的 `ssd`／VIN 在畫面預設遮蔽。

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
