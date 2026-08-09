# PartSouq / NHTSA 同步需求、注意事項與資料庫 Schema

文件日期：2026-08-10（Asia/Taipei）

## 1. 目標與目前結果

本專案以 Python 實作兩條互相分離的資料管線。PartSouq 保存公開型錄頁的原始 HTTP response，再解析車輛、圖組、零件與適用關係；NHTSA 從官方 bulk files 與明列 API 取得 Vehicle Safety／vPIC 資料，直接寫入 MySQL。

| 管線 | 正式資料庫 | 已驗證結果 | 判定 |
|---|---|---:|---|
| PartSouq | SQLite `output/partsouq-live.sqlite3`／`output/partsouq-browser-live.sqlite3` | HTTP：1 筆 robots.txt 200、4 筆 challenge；Brave/Playwright：1 筆 robots.txt 200、2 筆 challenge；正式型錄資料 0 筆 | `blocked`，未完成型錄爬取 |
| PartSouq 歷史封存 | SQLite `outputs/partsouq-common-crawl-diagrams.sqlite3` | Common Crawl 142 個真實 captures：115 個可解析型錄頁、27 個歷史 challenge；2,945 個唯一料號、5,928 筆 occurrence；另有 Wayback 單頁樣本 | 可用，但不是 live、current 或全量 |
| NHTSA | MySQL `nhtsa` | 457 個 current artifacts、12 個 datasets、9,876,638 筆 current rows、current rejected 0 | 本文件所列範圍已完成 |

GitHub remote 固定為 `git@github.com:a861252012/partsouq-crawler.git`。發布前必須同時確認：

1. `git remote -v` 的 owner 是 `a861252012`。
2. `ssh -T git@github.com` 回覆 `Hi a861252012!`。
3. Brave 顯示目前 GitHub 帳號為 `a861252012`。
4. 禁止使用 `Ted-Lin-Bibian`／Bibian 公司帳號、token 或 SSH key。

## 2. PartSouq 需求

- 從公開 Genuine Catalog seed、robots、sitemap 與已取得 HTML 連結持續發現 URL。
- 使用 SQLite persistent queue，支援 lease、斷點續爬、重試與終止狀態。
- 每次 response 必須先保存 status、headers、redirect chain、raw body、SHA-256 與時間，再進行分類或解析。
- 保留車輛、分類樹、diagram、料號、occurrence、verified fitment、compatibility hint 與 replacement 關係。
- 每筆 normalized record 必須可回查原始 response 與 parser version。
- 固定 User-Agent、低併發、每 host 延遲、遵守 robots；不得暴力重試 403。
- Cloudflare challenge 必須保存為證據並停止 run；challenge HTML 不得當成型錄 HTML。
- fixture、推測值或假資料不得計入 live 正式資料。

### Playwright / Selenium 決策

Playwright 與 Selenium 都是瀏覽器驅動層。兩者疊加不會讓同一個 session 更像真人，反而會產生兩套 lifecycle、cookie、等待與除錯模型。因此一條 collector 只應選一套；若網站只是一般 JavaScript render，優先選 Playwright。

目前 PartSouq response 已明確是 Cloudflare challenge，不是單純「頁面需要執行 JavaScript」。本專案不加入下列規避功能：

- `pyppeteer-stealth`、`playwright-stealth` 或其他 fingerprint／Web API 偽裝。
- `undetected-*` driver、TLS fingerprint 模仿、代理輪替或住宅代理。
- 搬運、複製或注入 `cf_clearance`。
- 自動解 CAPTCHA，或以「模擬真人」為目的的滑鼠／鍵盤行為偽裝。

這些套件不採用的依據不是名稱含有 `stealth`，而是其作者自己明示用途：

- `selenium-stealth` README 寫明用來「prevent detection」，並修改 platform、WebGL vendor、renderer 等瀏覽器特徵。
- `undetected-chromedriver` README 寫明會 patch driver，以避免觸發 anti-bot，並直接宣稱可通過 Cloudflare IUAM。
- `puppeteer-extra-plugin-stealth` README 寫明預設啟用多項 evasion techniques，目標是避免被網站偵測。
- Cloudflare 官方文件明示 Selenium、Puppeteer、Playwright 等自動化流量會被 challenge 阻擋。
- 原始需求第九節也明文禁止 browser fingerprint spoofing、Cloudflare bypass 與反覆重載 challenge。

因此這些不是「另一套正常瀏覽器 driver」，而是用來隱藏自動化身分、規避已出現的存取控制。本專案不對 PartSouq 安裝或測試它們。

在不使用付費第三方 API、目前也沒有 PartSouq 書面授權的條件下，已找到的替代路徑如下：

1. Common Crawl 多期 CDXJ/WARC：已實測成功。
2. Internet Archive Wayback CDX/快照：已實測成功。
3. 使用者正常瀏覽後，匯入其有權保存的 HTML／HAR／下載檔，再走既有 DB-first parser。
4. 取得合法持有的 OEM EPC／bulk export／供應商資料檔，使用同一個離線 importer。
5. 從 OEM 召回、維修公報、政府文件補特定料號與車型證據；這只能補片段，不能冒充完整 EPC。
6. 若站方政策允許，使用 headed Playwright 收集沒有 challenge 的一般 JS 頁；一遇 challenge 就停止。

以上路徑都不能在現況下保證「目前完整 PartSouq 型錄」，但前兩條已取得並解析真實歷史型錄，不再是只有 fixture。

### 歷史封存實測結果

Common Crawl 官方 CDXJ index 提供 capture URL、時間、digest、WARC filename、offset 與 length，可用 HTTP Range 只下載單一 WARC record。實測：

- `CC-MAIN-2021-49`：1,825 個 `/en/catalog/*` HTTP 200 URL。
- `CC-MAIN-2022-49`：357 個。
- `CC-MAIN-2024-10`：984 個。
- 三期聯集：3,161 個唯一 URL，包含 142 個 diagram URL。
- 批次依 WARC Range 下載 142 筆：成功 142、失敗 0；其中 115 筆為可解析型錄頁，27 筆為歷史 Cloudflare challenge，後者沒有進 parser。
- 批次 DB 保存 2,945 個唯一料號、5,928 筆 occurrence 與 5,928 筆歷史未驗證 fitment。
- 批次一度因 Python 系統 CA 鏈不足出現 `CERTIFICATE_VERIFY_FAILED`；改用已鎖版的 `certifi` CA bundle 後，仍維持 TLS 憑證驗證，單筆與完整批次均通過。
- 下載一個 `CC-MAIN-2024-10` diagram record 後取得真實 HTML；capture time 為 `2024-03-03T16:25:34Z`，body 為 1,048,576 bytes，WARC 標記 `truncated=length`。
- 修正 parser 後，該頁解析出 1 個 vehicle、3 個 diagrams、537 筆 part occurrences、171 個唯一料號。

Wayback CDX 以 `collapse=urlkey`、`statuscode=200` 分 6 頁取得：

- 24,562 個唯一 `/en/catalog/*` URL。
- 其中 4,864 個 diagram URL。
- 索引中有 1,083 個疑似含 VIN 的 URL；批次處理必須預設排除，不得輸出或上傳。
- 實際下載一個 2021 diagram 快照後，解析出 1 個 vehicle、1 個 diagram、23 筆 occurrences、23 個唯一料號。

Common Crawl 完整 diagram 批次已透過 `common-crawl-import` 先寫 raw body、SHA-256 與 `archive_captures`，再進 parser。輸出 DB 保存：

| 項目 | 數量 |
|---|---:|
| `http_responses` / `response_bodies` / `archive_captures` | 142 / 142 / 142 |
| 可解析型錄頁 / 歷史 challenge | 115 / 27 |
| `vehicle_configurations` | 115 |
| `diagrams` | 126 |
| `part_numbers` | 2,945 |
| `part_occurrences` | 5,928 |
| `fitments` | 5,928 |
| `record_sources` | 17,363 |
| `parse_failures` | 0 |
| 缺 provenance / foreign key violations | 0 / 0 |

這 5,928 筆 fitment 全部是 `is_verified=0`，derivation 是 `historical_archive_common_crawl`。三期分別保存 32／27／83 個 captures，capture time 落在 2021-11-27 至 2024-03-03；其中 2 個 WARC response 標記 `truncated=length`。另行驗證的 Wayback 單頁樣本有 23 個唯一料號與 23 筆 occurrence。歷史頁不能升格為 current verified fitment；截斷頁也不能宣稱頁面完整。

### 已實作的 Browser transport 與 live 證據

- CLI 已提供 `--transport browser`、`--browser-executable`、`--browser-headless`。
- 預設 headed，browser transport 固定 `concurrency=1`，沿用 host delay、robots、persistent queue、raw response、SHA-256、challenge detector 與 normalized ingest。
- 每次啟動都建立新的暫時 browser context；不使用既有 Brave profile，不搬運或保存 cookie。
- 2026-08-10 `probe` 實測 Genuine Catalog：`HTTP 403`、`cf-mitigated: challenge`、body SHA-256 `90a33cbb3c805578fbbd9609eef76ce1a43406c2b2eb647e708ec0c0fe4c1d96`、normalized 0。
- 2026-08-10 `crawl-all` 實測：`robots.txt` 200，接著 Genuine Catalog 403 challenge；run `partsouq-browser-crawl-20260810` 為 `blocked`，`cloudflare_challenge_response_count=1`、queue challenged 1、normalized 0。
- Live DB 共保存 3 筆 browser responses、3 個 unique bodies、12,357 raw bytes；foreign key violations 0。

## 3. NHTSA 需求與範圍

NHTSA 路徑只使用 MySQL，不使用 SQLite。所有官方 raw ZIP／CSV／JSON 先以 SHA-256 命名保存於 `output/nhtsa/raw`，再驗證、解析及寫入 MySQL。

目前 datasets：

- `safety_ratings`
- `recalls`
- `investigations`
- `complaints`
- `manufacturer_communications_summary`
- `manufacturer_communications`
- `cssi_stations`
- `vpic_makes`
- `vpic_models`
- `vpic_manufacturers`
- `vpic_variables`
- `vpic_variable_values`

範圍依 NHTSA 官方 Vehicle Safety datasets、CSSI 與 vPIC 集合型 endpoints 建立。使用者提供的 2,087 行附件沒有 NHTSA、vPIC、Recall、Complaint 或 FARS 段落，因此本專案不宣稱涵蓋 NHTSA 全部研究資料；FARS 不在目前範圍。

必要行為：

- bulk 優先；API 只允許程式內明列的集合型 endpoint。
- 禁止 VIN 掃描、VIN 範圍列舉、任意 URL 與把 VIN decode API 當 bulk download。
- 驗證 HTTP、ZIP magic、CRC、member、欄位 schema 與逐列解析。
- raw artifact、artifact member、record version、來源列號與 parser version 全部保留 provenance。
- 同一範圍只有全部來源 imported 且 rejected 為 0，才原子更新 current pointers。
- 失敗 artifact 保留為 `quarantined`，不得污染 current view。
- Git 只提交程式、測試、Compose 與文件；禁止提交 raw artifacts、MySQL volume、`.env` 或資料匯出檔。

### Current 資料快照

| Dataset | Current sources | Rows | Current rejected |
|---|---:|---:|---:|
| complaints | 7 | 2,233,015 | 0 |
| cssi_stations | 56 | 3,278 | 0 |
| investigations | 1 | 154,249 | 0 |
| manufacturer_communications | 7 | 5,797,781 | 0 |
| manufacturer_communications_summary | 7 | 1,208,330 | 0 |
| recalls | 2 | 325,810 | 0 |
| safety_ratings | 1 | 17,313 | 0 |
| vpic_makes | 1 | 12,321 | 0 |
| vpic_manufacturers | 229 | 22,881 | 0 |
| vpic_models | 1 | 31,827 | 0 |
| vpic_variable_values | 144 | 69,689 | 0 |
| vpic_variables | 1 | 144 | 0 |
| **合計** | **457** | **9,876,638** | **0** |

## 4. PartSouq SQLite Schema

權威 DDL：`src/partsouq_crawler/db/schema.sql`

```text
crawl_runs 1 ── n crawl_queue 1 ── n http_responses n ── 1 response_bodies
     │                 │                    │
     └── discovery_edges                    ├── parse_failures
                                            ├── record_sources
                                            └── 0..1 archive_captures
                                                   │
vehicle_configurations 1 ── n taxonomy_nodes       │
          │                       │                 │
          ├── n diagrams ─────────┘                 │
          │      │                                  │
          │      └── n part_occurrences n ── 1 part_numbers
          │                    │                    │
          └──────────────── fitments ───────────────┘

part_numbers 1 ── n compatibility_hints
part_numbers 1 ── n part_relations
```

| Table | Key / 關聯 | 用途 |
|---|---|---|
| `schema_migrations` | PK `version` | SQLite schema 版本 |
| `crawl_runs` | PK `id`；UQ `run_key` | run 設定、狀態、block reason、計數 |
| `crawl_queue` | PK `id`；FK `run_id`；UQ `(run_id,url_hash)` | persistent queue、lease、attempt、terminal state |
| `discovery_edges` | FK `run_id`、`source_response_id` | URL 發現來源與方法 |
| `response_bodies` | PK `sha256` | zlib raw body，依內容去重 |
| `http_responses` | FK `run_id`、`queue_id`、`body_sha256` | request／response、headers、status、challenge 證據 |
| `archive_captures` | UQ/FK `response_id`；index `(archive_source,captured_at)` | Common Crawl／Wayback／合法離線匯入的 capture time、collection、WARC 座標、digest、截斷狀態 |
| `record_sources` | FK `response_id`；UQ record/parser/source | normalized record provenance |
| `vehicle_configurations` | natural-key unique | 車輛、model、production period、catalog code |
| `taxonomy_nodes` | FK vehicle、self parent；UQ vehicle/path | 任意深度分類樹 |
| `diagrams` | FK vehicle、taxonomy | 圖組與有效期間 |
| `part_numbers` | UQ brand/raw/source；index normalized number | 原始／搜尋用料號 |
| `part_occurrences` | FK part、diagram、vehicle | 料號在圖組中的 callout、數量、range、condition |
| `fitments` | FK occurrence、part、vehicle、diagram | verified 適用關係與 derivation |
| `compatibility_hints` | FK part | 搜尋頁粗略相容文字，不是 verified fitment |
| `part_relations` | FK source part | replacement／substitution 等網站明示關係 |
| `parse_failures` | FK response | parser error 與 selector context |
| `robots_snapshots` | FK run、response | robots 原文 hash 與當次 User-Agent |

## 5. NHTSA MySQL Schema

權威 DDL：`src/partsouq_crawler/nhtsa/mysql_schema.sql`

```text
nhtsa_sync_runs
        │
        └── nhtsa_source_artifacts ── nhtsa_artifact_members
                    │
                    ├── nhtsa_artifact_records ── nhtsa_record_versions
                    └── nhtsa_rejected_rows

nhtsa_current_artifacts ── nhtsa_current_records (view)
```

| Table / View | Key / 關聯 | 用途 |
|---|---|---|
| `nhtsa_schema_migrations` | PK `version` | MySQL schema 版本 |
| `nhtsa_sync_runs` | PK `id`；index `(run_key,id)` | 一次 bulk／API run 與統計 |
| `nhtsa_source_artifacts` | PK `id`；UQ `(dataset_name,source_key,sha256,parser_version)` | URL、headers、raw path、hash、狀態、行數 |
| `nhtsa_artifact_members` | PK `(artifact_id,member_name)`；FK artifact | ZIP member／response.json 的 CRC、schema hash |
| `nhtsa_record_versions` | PK `(dataset_name,natural_key_sha256,record_sha256)` | 完整 JSON payload 的不可覆寫版本 |
| `nhtsa_artifact_records` | PK `(artifact_id,member_name,source_line)`；FK artifact/version | 原始來源行到 record version 的 lineage |
| `nhtsa_rejected_rows` | UQ `(artifact_id,member_name,source_line)`；FK artifact | 無法解析的原始列與錯誤 |
| `nhtsa_current_artifacts` | PK `(dataset_name,source_key)`；FK artifact | 已驗證 current artifact 指標 |
| `nhtsa_current_records` | view | current payload 加 URL、artifact SHA、member、line、parser version |

`nhtsa_record_versions` 提供常用索引：

- `(dataset_name, external_id)`
- `(dataset_name, make_name, model_name(191), model_year)`
- `(dataset_name, campaign_number)`

## 6. 執行與驗收

```bash
# NHTSA MySQL
docker compose -f compose.nhtsa.yml up -d
partsouq-crawler nhtsa-sync-bulk --scope all --run-id nhtsa-bulk-full
partsouq-crawler nhtsa-sync-api --scope all --run-id nhtsa-api-full
partsouq-crawler nhtsa-status

# PartSouq 現況查核
partsouq-crawler crawl-status \
  --sqlite output/partsouq-live.sqlite3 \
  --run-id partsouq-genuine-full

# PartSouq 標準 headed Brave／Playwright（fresh context）
partsouq-crawler crawl-all \
  --sqlite output/partsouq-browser-live.sqlite3 \
  --run-id partsouq-browser-crawl-20260810 \
  --seed-url 'https://partsouq.com/en/catalog/genuine' \
  --max-pages 10 --max-depth 0 --concurrency 1 --delay 5 \
  --retry-count 0 --robots-policy require \
  --transport browser \
  --browser-executable '/Applications/Brave Browser.app/Contents/MacOS/Brave Browser'

# 匯入已合法取得的歷史 HTML；不會連線 PartSouq
partsouq-crawler archive-import \
  --sqlite output/partsouq-archive.sqlite3 \
  --run-id archive-example \
  --input /path/to/capture.html \
  --source-url 'https://partsouq.com/en/catalog/genuine/diagram?...' \
  --archive-source common_crawl \
  --captured-at '2024-03-03T16:25:34Z' \
  --collection CC-MAIN-2024-10 \
  --warc-filename 'crawl-data/...warc.gz' \
  --warc-offset 123456 \
  --warc-length 7890 \
  --archive-digest 'sha1:...'

# 從已下載的 Common Crawl CDXJ index 續跑公開 WARC records；不連線 PartSouq
partsouq-crawler common-crawl-import \
  --sqlite output/partsouq-common-crawl.sqlite3 \
  --run-id common-crawl-diagrams \
  --index /path/to/CC-MAIN-index.ndjson \
  --delay 0.25 --timeout 60

# 歷史資料要明確要求才會輸出；預設 export 仍只含 verified fitment
partsouq-crawler export \
  --sqlite output/partsouq-archive.sqlite3 \
  --output output/partsouq-archive.csv \
  --include-unverified-fitments

# 品質檢查
python -m pytest
ruff check .
ruff format --check .
mypy src
git diff --check
```

驗收不得只看程式是否結束。PartSouq 必須同時核對 challenge count、normalized tables、provenance 與 raw response；NHTSA 必須核對 current source manifest、source rows、current rejected、raw SHA、foreign keys、orphan rows 與 MySQL `CHECK TABLE`。

## 7. 目前不能宣稱的事項

- 不能宣稱 PartSouq live/current 型錄已爬回；live 正式型錄相關表仍為 0。
- 可以宣稱 Common Crawl 批次與 Wayback 樣本已實際解析並入庫，但不能把 2,945 個料號或 5,928 筆歷史 occurrence 外推成目前全量。
- 不能把 robots.txt 或 Cloudflare challenge 當型錄成果。
- 不能宣稱 NHTSA 全站所有研究資料都已下載；目前完成的是本文件列出的 12 個 Vehicle Safety／vPIC／CSSI datasets。
- 不能宣稱 Playwright、Selenium 或 stealth 能保證通過 Cloudflare。
- 不能把 raw NHTSA artifacts、MySQL volume 或私人 `.env` 上傳 GitHub。
- 不能把含 VIN、`ssd` 或其他高熵查詢參數的 archive index、DB、CSV 或 raw HTML 上傳 GitHub。

## 8. 關鍵查證來源

- Cloudflare supported browsers：<https://developers.cloudflare.com/cloudflare-challenges/reference/supported-browsers/>
- Common Crawl CDXJ Index：<https://commoncrawl.org/cdxj-index>
- Common Crawl Get Started：<https://commoncrawl.org/get-started>
- Internet Archive Wayback CDX Server：<https://github.com/internetarchive/wayback/blob/master/wayback-cdx-server/README.md>
- `selenium-stealth` README：<https://github.com/diprajpatra/selenium-stealth>
- `undetected-chromedriver` README：<https://github.com/ultrafunkamsterdam/undetected-chromedriver>
- `puppeteer-extra-plugin-stealth` README：<https://github.com/berstend/puppeteer-extra/tree/master/packages/puppeteer-extra-plugin-stealth>
