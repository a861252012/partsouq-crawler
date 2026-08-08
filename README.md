# PartSouq Genuine Catalog crawler

這是一套 evidence-first、可斷點續爬的 Python crawler。它只讀取 PartSouq 公開頁面，先保存每次實際 HTTP response，再解析車輛、分類、圖組、零件與 fitment 到本機 SQLite。

它不會解 CAPTCHA、注入 `cf_clearance`、輪替 proxy 或繞過 Cloudflare。遇到 challenge 時會保存證據、將 run 標成 `blocked`，並以非 0 code 結束。

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
```

`reparse` 只讀取 DB 的 raw body，不會打 PartSouq。normalized 寫入採冪等 natural key。CSV 預設只含 verified fitment，並防護 Excel 公式注入；`/search/all` 的文字只會輸出為 compatibility hint。

Backup 使用 SQLite Backup API，不會直接複製執行中的 WAL DB。

## DB 查詢

```bash
sqlite3 output/partsouq-live.sqlite3 '
SELECT id, http_status, content_type, is_cloudflare_challenge,
       requested_url, body_sha256, fetched_at
FROM http_responses ORDER BY id DESC LIMIT 20;'
```

資料表與 provenance 查詢請看 [DATABASE.md](DATABASE.md)。資料流與 failure boundary 請看 [ARCHITECTURE.md](ARCHITECTURE.md)。

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
