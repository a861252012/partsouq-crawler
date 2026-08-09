# PartSouq / NHTSA 同步需求、注意事項與資料庫 Schema

文件日期：2026-08-10（Asia/Taipei）

## 1. 目標與目前結果

本專案以 Python 實作兩條互相分離的資料管線。PartSouq 保存公開型錄頁的原始 HTTP response，再解析車輛、圖組、零件與適用關係；NHTSA 從官方 bulk files 與明列 API 取得 Vehicle Safety／vPIC 資料，直接寫入 MySQL。

| 管線 | 正式資料庫 | 已驗證結果 | 判定 |
|---|---|---:|---|
| PartSouq | SQLite `output/partsouq-live.sqlite3` | 1 筆 robots.txt 200、4 筆 Cloudflare 403、正式型錄資料 0 筆 | `blocked`，未完成型錄爬取 |
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

在不使用付費第三方 API、目前也沒有 PartSouq 書面授權的條件下，可接受的替代路徑只有：

1. 使用者正常瀏覽後，匯入其有權保存的 HTML／HAR／下載檔，再走既有 DB-first parser。
2. 取得合法持有的 OEM EPC／bulk export／供應商資料檔，新增離線 importer。
3. 若站方政策允許，使用 headed Playwright 收集沒有 challenge 的一般 JS 頁；一遇 challenge 就停止，不嘗試規避。

以上替代路徑都不能在現況下保證「自動全量取得 PartSouq 型錄」。

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
                                            └── record_sources
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

# 品質檢查
python -m pytest
ruff check .
ruff format --check .
mypy src
git diff --check
```

驗收不得只看程式是否結束。PartSouq 必須同時核對 challenge count、normalized tables、provenance 與 raw response；NHTSA 必須核對 current source manifest、source rows、current rejected、raw SHA、foreign keys、orphan rows 與 MySQL `CHECK TABLE`。

## 7. 目前不能宣稱的事項

- 不能宣稱 PartSouq 型錄已爬回；目前正式型錄相關表均為 0。
- 不能把 robots.txt 或 Cloudflare challenge 當型錄成果。
- 不能宣稱 NHTSA 全站所有研究資料都已下載；目前完成的是本文件列出的 12 個 Vehicle Safety／vPIC／CSSI datasets。
- 不能宣稱 Playwright、Selenium 或 stealth 能保證通過 Cloudflare。
- 不能把 raw NHTSA artifacts、MySQL volume 或私人 `.env` 上傳 GitHub。
