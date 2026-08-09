# PartSouq 與 NHTSA 官方資料同步器

這是一套 evidence-first、可斷點續爬的 Python crawler。它只讀取 PartSouq 公開頁面，先保存每次實際 HTTP response，再解析車輛、分類、圖組、零件與 fitment 到本機 SQLite。

它不會解 CAPTCHA、注入 `cf_clearance`、輪替 proxy 或繞過 Cloudflare。遇到 challenge 時會保存證據、將 run 標成 `blocked`，並以非 0 code 結束。

NHTSA 路徑與 PartSouq 分離：NHTSA 使用官方 bulk download／API，normalized 資料直接寫入 MySQL，raw ZIP／CSV／JSON 以 SHA-256 命名保存在本機。NHTSA 不使用 SQLite。

## 安裝

需要 Python 3.12 以上。建議使用 `uv`：

```bash
uv sync --extra dev
source .venv/bin/activate
partsouq-crawler --help
```

或使用標準 virtualenv：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
```

## NHTSA：啟動 MySQL

此 Compose 只建立專案自己的 `nhtsa-mysql`，綁定 `127.0.0.1:3308`，不會碰本機既有的 3306 MySQL：

```bash
docker compose -f compose.nhtsa.yml up -d
cp -n .env.example .env
```

預設建立 `nhtsa` 與測試專用的 `nhtsa_test`。正式環境請覆寫 `.env` 內的密碼；`.env` 不應提交。

## NHTSA：資料範圍與全量同步

目前同步 NHTSA 官方 Vehicle Safety 資料：

- Safety Ratings
- Recalls
- Investigations
- Complaints
- Manufacturer Communications summary
- Manufacturer Communications／TSB detail
- CSSI car-seat inspection stations（全州與領地）
- vPIC makes、models、manufacturers、variables、variable values

提供的需求附件沒有 NHTSA 段落，因此本實作以 NHTSA 官方 Datasets and APIs 頁面的 Vehicle Safety 範圍為準；FARS crash statistics 不在目前範圍。

執行：

```bash
partsouq-crawler nhtsa-sync-bulk --scope all --run-id nhtsa-bulk-full
partsouq-crawler nhtsa-sync-api --scope all --run-id nhtsa-api-full
partsouq-crawler nhtsa-status
```

也可先按範圍執行：

```bash
partsouq-crawler nhtsa-sync-bulk --scope safety-ratings
partsouq-crawler nhtsa-sync-bulk --scope recalls
partsouq-crawler nhtsa-sync-bulk --scope investigations
partsouq-crawler nhtsa-sync-bulk --scope complaints
partsouq-crawler nhtsa-sync-bulk --scope manufacturer-communications-summary
partsouq-crawler nhtsa-sync-bulk --scope manufacturer-communications
partsouq-crawler nhtsa-sync-api --scope vpic
partsouq-crawler nhtsa-sync-api --scope cssi
```

每個來源先下載到 `NHTSA_RAW_DIR`，檢查 ZIP member、CRC、欄位 schema 與逐列解析，再批次寫入 MySQL。選定範圍只有在全部 artifact 都 `imported` 且零拒絕時，才會原子更新 current view。失敗資料會保留並標成 `quarantined`，不會冒充完成。

`output/` 已由 Git 忽略。Complaints、CSSI 等官方資料可能含 VIN 或聯絡欄位，不要把 raw artifact、DB volume 或匯出檔提交到 GitHub。

vPIC 只允許程式內明列的集合型 endpoint。VIN decode、VIN 批次 decode、VIN 枚舉與任意 URL 都會被 policy 拒絕。

## 低頻 probe

```bash
partsouq-crawler probe \
  'https://partsouq.com/en/catalog/genuine' \
  --sqlite output/partsouq-live.sqlite3 \
  --user-agent "$PARTSOUQ_USER_AGENT"
```

Probe 每次只發一個正常 GET。response 會先寫進 DB。403 challenge 不會被解析成型錄資料。

## 全量執行與續爬

設定具聯絡方式的固定 User-Agent：

```bash
export PARTSOUQ_USER_AGENT='YourCrawler/1.0 (contact: you@example.com)'
scripts/run_full_catalog.sh
```

等價完整指令：

```bash
partsouq-crawler crawl-all \
  --run-id partsouq-genuine-full \
  --seed-url 'https://partsouq.com/en/catalog/genuine' \
  --sqlite output/partsouq-live.sqlite3 \
  --max-pages 0 \
  --max-depth 0 \
  --concurrency 1 \
  --delay 5 \
  --robots-policy require \
  --user-agent "$PARTSOUQ_USER_AGENT"
```

`0` 代表 unlimited。中斷後用相同 `--run-id` 與 SQLite 路徑重跑即可；已完成 URL 不會重新 request，過期 lease 會恢復成 pending。

SIGINT/SIGTERM 會停止取得新工作。正在處理的 request 結束後，run 會標為 `paused`。若程序被強制終止，下次會回收過期 lease。

## 狀態與問題頁

```bash
partsouq-crawler crawl-status --sqlite output/partsouq-live.sqlite3 --run-id partsouq-genuine-full
partsouq-crawler db-status --sqlite output/partsouq-live.sqlite3
partsouq-crawler problem-urls --sqlite output/partsouq-live.sqlite3 --run-id partsouq-genuine-full --format jsonl
```

`strict_complete=true` 只會在 queue 耗盡、沒有 failed/challenged/skipped_robots/parse_failed、provenance 完整且 foreign key check 通過時出現。完成報告代表「所有已發現、scope 內 URL 已處理」，不代表網站中不可發現或私人資料也已下載。

Challenge 不會自動循環。取得正式 allowlist、API 或合法可存取環境後，才由使用者明確 requeue：

```bash
partsouq-crawler requeue-problems \
  --sqlite output/partsouq-live.sqlite3 \
  --run-id partsouq-genuine-full \
  --status challenged
```

## Raw response、reparse、export、backup

```bash
partsouq-crawler dump-response --sqlite output/partsouq-live.sqlite3 --response-id 1 --output output/response-1.html
partsouq-crawler reparse --sqlite output/partsouq-live.sqlite3 --run-id 1
partsouq-crawler export --sqlite output/partsouq-live.sqlite3 --output output/fitments.csv
partsouq-crawler export --sqlite output/partsouq-live.sqlite3 --output output/all.jsonl --include-compatibility-hints
partsouq-crawler db-backup --sqlite output/partsouq-live.sqlite3 --output output/backup.sqlite3
partsouq-crawler snapshot-publish --sqlite output/partsouq-live.sqlite3 --output output/partsouq-current.sqlite3
```

`snapshot-publish` 會在同一目錄先建立一致性備份，執行 `PRAGMA integrity_check`，寫入 SHA-256 manifest，再於發布鎖保護下原子替換快照。後台只應讀取這個已發布快照，不應直接讀 live DB。

`reparse` 只讀取 DB 的 raw body，不會打 PartSouq。normalized 寫入採冪等 natural key。CSV 預設只含 verified fitment，並防護 Excel 公式注入；`/search/all` 的文字只會輸出為 compatibility hint。

Backup 使用 SQLite Backup API，不會直接複製執行中的 WAL DB。

## DB 查詢

```bash
sqlite3 output/partsouq-live.sqlite3 '
SELECT id, http_status, content_type, is_cloudflare_challenge,
       requested_url, body_sha256, fetched_at
FROM http_responses ORDER BY id DESC LIMIT 20;'
```

資料表與 provenance 查詢請看 [DATABASE.md](DATABASE.md)。資料流與 failure boundary 請看 [ARCHITECTURE.md](ARCHITECTURE.md)。本次完整需求、注意事項、實際進度與兩套 schema 另提供 [Markdown](docs/requirements-notes-schema.md) 及 [HTML](docs/requirements-notes-schema.html)。

## 驗證

```bash
python -m pytest
ruff check .
ruff format --check .
mypy src
```

測試 DB 與 fixture 只會寫到 pytest 的 temporary directory 或 `tests/tmp/`，不會寫入 `output/partsouq-live.sqlite3`。

## 常見問題

**為什麼 run 是 blocked？** 先用 `dump-response` 檢查保存的 response。若為 Cloudflare challenge，crawler 會停機，不會繞過。

**robots.txt 讀不到怎麼辦？** `--robots-policy require` 會將 run 標為 blocked。它不會默認允許。`robots` 允許也不等於商業重製授權。

**`0605-` 為什麼沒有解析日期？** 沒有品牌與頁面上下文時格式不確定，normalized 日期保持 NULL，raw value 永遠保留。

**為什麼同一料號有多列？** 料號可對應不同 Vehicle、Diagram、Range、Condition、Callout 與 Note；這些 occurrence 不可互相覆蓋。
