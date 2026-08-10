# PartSouq／NHTSA 需求、注意事項、Schema 與驗收紀錄

文件日期：2026-08-10（Asia/Taipei）

> 這是 MySQL／archive 階段的歷史驗收快照。PartSouq NoDriver 實測、每月無人值守排程與目前 release gate 請以 [最新驗收報告](../outputs/monthly-autonomous-crawler-report.md) 為準；下方的「live normalized 0」只是當時快照。

## 1. 結論先講

| 項目 | 實作狀態 | 已查證結果 |
|---|---|---|
| PartSouq production DB | 完成 | state、queue、raw body、HTTP response、archive capture、normalized record、provenance 全部直接寫 MySQL 8。SQLite adapter 只供舊 snapshot 遷移與隔離單元測試。 |
| PartSouq live HTTP | 外部阻擋 | aiohttp 與 headed Brave／Playwright 都實際收到 Cloudflare challenge；raw response 已入 MySQL，run 正確 `blocked`，live normalized 0。 |
| PartSouq 歷史資料 | 已實際取得並匯入 | Common Crawl 三期 WARC 與 Wayback 六頁 CDX 都使用 persistent MySQL queue；26,460 筆 archive capture 已入庫，資料一律標為 historical/unverified，不冒充 current。 |
| PartSouq CRUD 後台 | 完成 | Flask/Jinja/PyMySQL；5 類實體可 browse/search/create/update/retire/restore；source table 唯讀，人工異動有 append-only audit。 |
| 效能／N+1 | 完成並驗證 | 500 筆 ingest 的 round trip 不隨 part 數線性增加；列表固定 3 queries、detail 固定 4；keyset pagination、index range search、1,000 筆 batch export。 |
| NHTSA | 本文件定義範圍完成 | 12 個 Vehicle Safety／CSSI／vPIC datasets 共 9,876,638 筆 current rows；最新 full scope run `completed`、rejected 0。 |
| GitHub | 發布前硬閘門 | remote、SSH welcome、Brave GitHub 帳號都必須再次確認為 `a861252012`；禁止 Bibian 帳號、key 或 token。 |

「技術上可以用 Playwright」與「Playwright 能取得正常型錄」是兩件不同的事。本專案已實作並實跑 Playwright；實際 response 仍是 Cloudflare challenge，因此不能宣稱 PartSouq current 型錄已完整抓回。

## 2. 需求來源與後續變更

使用者提供的 2,087 行 PartSouq 規格要求：

- Python 正式 crawler，不是 PoC。
- seed、robots、nested sitemap 與 HTML link discovery。
- unlimited、persistent queue、lease、resume、graceful interrupt。
- raw response first、SHA-256、zlib、parser version、record provenance。
- 車型、分類、diagram、料號、occurrence、fitment、compatibility hint、part relation。
- `ssd` 是 opaque token；不得改寫或猜測。
- compatibility 不是 verified fitment；range／diagram range／production range 分開。
- challenge 保存、blocked、停止、非 0；不得用假資料或 challenge 冒充成果。

後續使用者明確變更：

- PartSouq 不使用 SQLite production DB，改為直接寫 MySQL。
- NHTSA 也要完整實作並實際寫 MySQL。
- 建立本機 CRUD 後台。
- 確保效能與 no-N+1。
- 產出需求、注意事項、schema 的 Markdown 與 HTML。
- 最後只可用 GitHub 個人帳號 `a861252012` 發布。

因此本版以後續明確指示覆蓋原規格中的 SQLite production／SQLite backup／snapshot publish；舊 SQLite 功能只保留在 migration compatibility path。

## 3. PartSouq normalized 欄位

必要輸出與目前資料模型的對應：

| 需求欄位 | MySQL 欄位／資料表 |
|---|---|
| Brand、Name、Model、Description、Options | `vehicle_configurations` |
| Prod period、Production from/to/precision | `vehicle_configurations` |
| 大／中／小分類、完整 path、任意深度 | `taxonomy_nodes(parent_id,depth,path_raw)` |
| Diagram code/name/range/from/to | `diagrams` |
| 產品英文名稱、原始料號、normalized 料號 | `part_numbers` |
| Callout、Quantity、Range、Condition、Note | `part_occurrences` |
| Confidence、Derivation、Verified | `fitments` |
| Compatibility 粗略文字 | `compatibility_hints`，不建立 verified fitment |
| replacement／substitution | `part_relations`，不做關係閉包 |
| Source URL、response、parser version | `record_sources → http_responses` |
| Raw HTML/XML、headers、status、時間、SHA | `http_responses → response_bodies` |
| Archive source/time/WARC locator/digest | `archive_captures` |

Identity 規則：

- 原始料號與前導 0 永遠保留；normalized 只供搜尋。
- URL request bytes 不會為去重而重排 query；queue 使用完整 URL SHA-256。
- `ssd` 不解析、不修改；raw DB 會保存原字串。
- 含 `TEXT`／`NULL` 的 natural key 使用 generated SHA-256 unique key。
- 同料號不同 vehicle／diagram／range／condition／callout／note 是不同 occurrence。
- Historical archive fitment 固定 `is_verified=0`；live challenge 不產生任何 fitment。

## 4. Cloudflare、Playwright 與 Selenium 決策

### 實際 MySQL live 證據

2026-08-10 使用標準 headed Brave／Playwright，fresh browser context：

```text
probe URL: https://partsouq.com/en/catalog/genuine
HTTP status: 403
cf-mitigated: challenge
challenge: true
body SHA-256: 3c8e2f8808e2d2ac47ce4a256157d6a6f28039c7626c554c907a9ecb94509ec5
response_id: 1521
normalized_records_inserted: 0
```

完整 crawler 驗收 run：`partsouq-browser-live-crawl-20260810-mysql`。

```text
robots.txt: HTTP 200，已保存 raw response
Genuine Catalog: HTTP 403 challenge，已保存 raw response
run status: blocked
blocked_reason: cloudflare_challenge
queue: challenged=1, pending sitemap=1
cloudflare_challenge_response_count: 1
normalized_record_count: 0
strict_complete: false
```

這證明 Playwright 確實有執行，也證明 challenge 不是單純缺少瀏覽器 JavaScript。

### 為何不疊加 Selenium／stealth

- Playwright、Selenium、Puppeteer 是替代 browser driver；把兩套 driver 接在同一 session 不會增加資料正確性，只會增加兩套 lifecycle、cookie 與等待模型。
- 一般 JavaScript render 可使用標準 Playwright；一旦 response 已確認是 challenge，改用另一套 driver 繼續試就是針對存取控制的工具切換。
- `selenium-stealth`、`pyppeteer-stealth`、`puppeteer-extra-plugin-stealth` 會修改 webdriver／WebGL／platform 等指紋以避免偵測。
- `undetected-chromedriver` 明示會 patch driver 以避免 anti-bot detection。
- 本專案不使用 fingerprint spoofing、TLS 模仿、proxy／住宅代理、UA 輪替、CAPTCHA solver、`cf_clearance` 搬運或 Cloudflare bypass 服務。

已實作的替代來源是 Common Crawl、Wayback，以及使用者合法持有的單一 HTML。HAR／PDF／CSV、OEM 或供應商 bulk export 目前只是可擴充來源，尚未實作。來源類型與信心必須分開，不得把其他來源標成 PartSouq live。

## 5. PartSouq archive 執行紀錄

### 舊 SQLite snapshot → MySQL

已對 `partsouq-common-crawl-diagrams.sqlite3` 做封閉 snapshot raw replay：

| 檢查 | 結果 |
|---|---:|
| 舊匯入建立的 capture | 142 |
| 新 migration manifest 對帳 | selected 142 / imported 0 / skipped 142 / quarantined 0 |
| body SHA source→target 差集 | 0 |
| vehicles/taxonomy/diagrams | 115 / 460 / 126 |
| unique part numbers | 2,945 |
| occurrences/fitments | 5,928 / 5,928 |
| record sources | 17,363 |
| parse failures | 0 |
| missing provenance/orphans/FK | 0 / 0 / 0 |

這 142 筆 raw response 已由舊匯入流程存在 MySQL，因此新版 persistent migration 第一次對帳就全部安全略過。立即重跑仍是 `imported=0`、`skipped=142`，normalized counts 不變，證明可重入。

### Common Crawl 完整 index

三期本機 CDXJ index 共 3,166 records；安全 allowlist 後 3,165 個唯一 WARC locator：

| Route | 數量 |
|---|---:|
| pick | 2,333 |
| vehicle | 625 |
| diagram | 142 |
| unit | 51 |
| locate | 11 |
| search | 2 |
| parts | 1 |

每筆 item 都有 pending/in_progress/done/challenged/http_error/parse_failed/failed 狀態、attempt、lease、worker、fencing token、response_id 與 last_error。已由 SQLite 匯入且 capture key 相同的 142 筆會直接標 done，不重新下載。

### Wayback 完整 index

六頁本機 CDX JSON 原始 24,562 rows；去重並排除 VIN path／明確 17 碼 VIN query 後，選出 23,295 個 capture。Queue 與 Common Crawl 使用相同的 MySQL manifest/lease/fencing contract；原始 URL 只存在 DB，報表與 export 預設遮蔽敏感 query。

### 實際 normalized 資料抽樣

以下是直接從 MySQL 的 `fitments → part_numbers → vehicle_configurations → diagrams → record_sources → archive_captures` join 查得；不是 fixture。歷史 fitment 一律保持 `is_verified=0`。

| Archive | Brand／車型 | Diagram | Part number | Name |
|---|---|---|---|---|
| Common Crawl | TOYOTA RAV4 J/L／ZCA26W-AWPNK | — | 163250H020 | GASKET, WATER INLET HOUSING, NO.1 |
| Common Crawl | TOYOTA RAV4 J/L／ZCA26W-AWPNK | — | 9091603129 | THERMOSTAT |
| Wayback | HONDA INTEGRA／17ST701 | ALTERNATOR BRACKET | 957010601208 | BOLT, FLANGE, 6X12 |
| Wayback | HONDA INTEGRA／17ST701 | ALTERNATOR BRACKET | 90059PR3000 | BOLT, KNOCK, 10X35 |

### 最終資料快照

下表是 2026-08-10 匯入終態後，以 `db-status` 與 SQL 直接對帳的結果。

<!-- PARTSOUQ_FINAL_SNAPSHOT_START -->

| 指標 | 數量／狀態 |
|---|---:|
| Common Crawl queue | 3,165/3,165 terminal：done 2,835、challenged 330、pending/in-progress/failed 0 |
| Wayback queue | 23,295/23,295 terminal：done 22,028、challenged 1,170、HTTP 404 67、HTTP 502 29、parse failed 1、pending/in-progress/failed 0 |
| `archive_captures` | 26,460（Common Crawl 3,165；Wayback 23,295） |
| `http_responses`／unique bodies | 26,463／23,647 |
| raw／compressed body bytes | 4,451,268,822／546,839,105（ratio 0.1229） |
| MySQL physical tablespaces | 1,853,718,528 bytes／24 tablespaces |
| `vehicle_configurations` | 21,509 |
| `taxonomy_nodes` | 74,623 |
| `diagrams` | 2,724 |
| `part_numbers` | 52,102 |
| `part_occurrences` | 159,505 |
| `fitments` | 165,438（Common Crawl 6,013；Wayback 159,425；全部 `is_verified=0`） |
| `record_sources` | 619,533 |
| `parse_failures` | 5（都是保留 raw response 後的 parser failure） |
| missing provenance／orphans／FK | 0／0／0 |

<!-- PARTSOUQ_FINAL_SNAPSHOT_END -->

## 6. PartSouq MySQL Schema

權威 DDL：`src/partsouq_crawler/db/mysql_schema.sql`；migration：`src/partsouq_crawler/db/mysql_migrations/`。

```text
crawl_runs 1 ── n crawl_queue 1 ── n http_responses n ── 1 response_bodies
     │                 │                    │
     ├── discovery_edges                    ├── parse_failures
     └── archive_import_manifests           ├── record_sources
                 │                          └── archive_captures
                 └── archive_import_items

vehicle_configurations 1 ── n taxonomy_nodes
          │                       │
          ├── n diagrams ─────────┘
          │      │
          │      └── n part_occurrences n ── 1 part_numbers
          │                    │
          └──────────────── fitments

part_numbers 1 ── n compatibility_hints
part_numbers 1 ── n part_relations
```

| Table | 關鍵欄位／用途 |
|---|---|
| `schema_migrations` | version、applied_at |
| `crawl_runs` | run_key unique、status、blocked_reason、counts |
| `crawl_queue` | `(run_id,url_hash)` unique、worker、fencing、lease、terminal state |
| `discovery_edges` | source response、parent、target、method、natural hash |
| `response_bodies` | SHA-256 PK、zlib blob、raw/stored bytes |
| `http_responses` | run/queue、URL、status、headers、body SHA、challenge、fencing |
| `archive_captures` | capture key unique、source/time/WARC/digest/truncation/metadata |
| `archive_import_manifests/items` | persistent archive traversal、resume、fencing、coverage |
| `sqlite_imports/items` | legacy snapshot manifest、cursor、reconciliation |
| `record_sources` | record type/id → response/parser/version/source URL |
| `vehicle_configurations` | 車型與 production metadata |
| `taxonomy_nodes` | 任意深度分類樹 |
| `diagrams` | vehicle/taxonomy/code/name/range |
| `part_numbers` | brand/raw/normalized/name |
| `part_occurrences` | part/diagram/vehicle/callout/quantity/range/condition/note |
| `fitments` | occurrence/part/vehicle/diagram/verified/derivation/confidence |
| `compatibility_hints` | 粗略相容文字 |
| `part_relations` | 明示替代關係 |
| `parse_failures` | parser error，不重複 body |
| `robots_snapshots` | robots provenance |

## 7. CRUD 後台與 no-N+1

後台實體：`vehicle_configurations`、`diagrams`、`part_numbers`、`part_occurrences`、`fitments`。

CRUD 語意：

- Browse/search：source + active overlay 的 effective view。
- Create：建立 manual UUID 與完整 audit event，不偽造 crawler provenance。
- Update：只更新 overlay head；source row hash 不變。
- Retire：soft tombstone，可 restore；不刪 source/raw/provenance。
- Concurrent update：`expected_revision` 不符回 409。

Schema：

| Table | 用途 |
|---|---|
| `admin_override_heads` | entity/identity/source/manual UUID、payload JSON、status、revision、base SHA、actor/reason |
| `admin_override_events` | head、action、revision、before/after JSON、actor/reason、timestamp；append-only |

真實 MySQL integration 與 deterministic query-contract tests 已驗：

- list HTTP 200，固定 `X-Admin-Query-Count: 3`。
- search HTTP 200；已知前綴 `01456` 回傳多筆 source part。
- create → update → stale update 409 → retire → restore，共 4 個 audit events。
- `partsouq_admin` 直接 UPDATE source table 被 MySQL 拒絕。
- 操作前後 source row SHA-256 相同。
- 500 part bulk ingest 重跑與併發都不重複；provenance/FK 完整。
- 1,000 queue items、8 workers 沒有重複 claim/finalize；stale fencing write 被拒絕。
- Admin 1／100／10,000 rows 的 query count 與 SQL fingerprint 不變；高 fanout 被 100 筆上限約束。
- `EXPLAIN ANALYZE` 無結果料號搜尋使用 brand/normalized/name 三個 index range；不再逐列 join scan 全部 source rows。

## 8. NHTSA 範圍與結果

使用者附件沒有 NHTSA 段落。本實作依 NHTSA 官方 Vehicle Safety datasets、CSSI 與 vPIC 集合型 API 定義範圍；不宣稱涵蓋 FARS 或 NHTSA 全站研究資料。

| Dataset | Current rows |
|---|---:|
| complaints | 2,233,015 |
| cssi_stations | 3,278 |
| investigations | 154,249 |
| manufacturer_communications | 5,797,781 |
| manufacturer_communications_summary | 1,208,330 |
| recalls | 325,810 |
| safety_ratings | 17,313 |
| vpic_makes | 12,321 |
| vpic_manufacturers | 22,881 |
| vpic_models | 31,827 |
| vpic_variable_values | 69,689 |
| vpic_variables | 144 |
| **合計** | **9,876,638** |

最新全量 run：`nhtsa-bulk-all-coverage-v4`，status `completed`，source rows 9,736,498，rejected 0。資料庫目前保存 483 個 imported artifacts 與 5 個舊 quarantined artifacts；5 個 quarantine 是早期失敗／被新版取代的 audit history，沒有進 current pointer。Current selected artifacts 的 rejected count 為 0。

### NHTSA Schema

```text
nhtsa_sync_runs
        │
        └── nhtsa_source_artifacts ── nhtsa_artifact_members
                    │
                    ├── nhtsa_artifact_records ── nhtsa_record_versions
                    └── nhtsa_rejected_rows

nhtsa_current_artifacts ── nhtsa_current_records (view)
```

| Table/View | 用途 |
|---|---|
| `nhtsa_sync_runs` | run/scope/status/counters |
| `nhtsa_source_artifacts` | URL、raw path、SHA、parser version、status、rows |
| `nhtsa_artifact_members` | ZIP member／JSON、CRC、schema hash |
| `nhtsa_record_versions` | dataset/natural key/payload SHA 的完整 JSON 版本 |
| `nhtsa_artifact_records` | artifact/member/line → version lineage |
| `nhtsa_rejected_rows` | 原始拒絕列與錯誤；不進 current |
| `nhtsa_current_artifacts` | 已驗證 current pointer |
| `nhtsa_current_records` | current payload + 完整來源欄位 |

## 9. 驗收命令與證據

```bash
NHTSA_TEST_MYSQL=1 PARTSOUQ_TEST_MYSQL=1 python -m pytest
ruff check .
ruff format --check .
mypy src
git diff --check
docker compose -f compose.nhtsa.yml config --quiet
```

本次完整 pytest：`110 passed`、`0 skipped`。PartSouq／NHTSA MySQL repository、NHTSA fake-source sync、admin、CLI archive migration、live browser probe、archive import、分批 export 及本機 HTTP admin smoke test都已實際通過。

## 10. 不能宣稱的事項

- 不能宣稱 PartSouq current/live 型錄已完整取得；目前 live 正常型錄是 0，原因是已保存的 Cloudflare challenge。
- 不能把 archive、fixture、search hint、robots 或 challenge 算成 current verified fitment。
- Common Crawl／Wayback 的選定 queue 已全部 terminal；其中 challenge、HTTP error 與 parse failure 是明列缺口，不算成功型錄頁。
- 不能宣稱 NHTSA 全站所有資料都已下載；目前完成的是本文件明列的 12 個 datasets。
- 不能把 DB、raw HTML、CDX index、WARC、NHTSA raw、CSV/JSONL 或含 VIN／`ssd` 的輸出提交 GitHub。
- 不能使用 Bibian 公司 GitHub 帳號、SSH key、token 或 repository owner。

## 11. GitHub 發布檢查

依序檢查，任一不符就停止：

1. `git remote -v` owner 是 `a861252012/partsouq-crawler`。
2. `ssh -T git@github.com` welcome 是 `Hi a861252012!`。
3. Brave 目前 GitHub 帳號顯示 `a861252012`。
4. Repository 是 private。
5. `git status` 不含 `.env`、output、work、DB、archive index 或 export。
6. 完整測試與 diff checks 綠燈後才 commit/push。

## 12. 查證來源

- 原始 PartSouq 需求附件：本機 `pasted-text.txt`，2,087 行。
- [Cloudflare Challenge Page 的 `cf-mitigated: challenge` 判定](https://developers.cloudflare.com/cloudflare-challenges/challenge-types/challenge-pages/detect-response/)
- [Cloudflare supported browsers](https://developers.cloudflare.com/cloudflare-challenges/reference/supported-browsers/)
- [Playwright BrowserContext](https://playwright.dev/python/docs/api/class-browsercontext)
- [Playwright BrowserType／自訂 browser executable](https://playwright.dev/python/docs/api/class-browsertype)
- [Selenium WebDriver](https://www.selenium.dev/documentation/webdriver/)
- [Common Crawl CDXJ Index](https://commoncrawl.org/cdxj-index)
- [Common Crawl Get Started](https://commoncrawl.org/get-started)
- [Internet Archive Wayback CDX Server](https://github.com/internetarchive/wayback/blob/master/wayback-cdx-server/README.md)
- [NHTSA Datasets and APIs](https://www.nhtsa.gov/nhtsa-datasets-and-apis)
- [NHTSA vPIC API](https://vpic.nhtsa.dot.gov/api/Home/Index)
- [MySQL locking reads／SKIP LOCKED](https://dev.mysql.com/doc/refman/8.0/en/innodb-locking-reads.html)
- [aiomysql pool](https://aiomysql.readthedocs.io/en/stable/pool.html)
- [selenium-stealth README](https://github.com/diprajpatra/selenium-stealth)
- [undetected-chromedriver README](https://github.com/ultrafunkamsterdam/undetected-chromedriver)
- [puppeteer-extra stealth README](https://github.com/berstend/puppeteer-extra/tree/master/packages/puppeteer-extra-plugin-stealth)
