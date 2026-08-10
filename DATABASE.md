# Database

PartSouq 與 NHTSA 使用同一個本機 MySQL 8 container，但各自使用 `partsouq` 與 `nhtsa` schema。兩條管線的 run、raw、normalized 與完成條件完全分離。

## PartSouq MySQL

權威 DDL：`src/partsouq_crawler/db/mysql_schema.sql`。增量 migration：`src/partsouq_crawler/db/mysql_migrations/`。

```text
crawl_runs 1 ── n crawl_queue 1 ── n http_responses n ── 1 response_bodies
     │                 │                    │
     ├── discovery_edges                    ├── parse_failures
     └── archive_import_manifests           ├── record_sources
                 │                          └── 0..1 archive_captures
                 └── archive_import_items               │
                                                        │
vehicle_configurations 1 ── n taxonomy_nodes            │
          │                       │                      │
          ├── n diagrams ─────────┘                      │
          │      │                                       │
          │      └── n part_occurrences n ── 1 part_numbers
          │                    │                         │
          └──────────────── fitments ────────────────────┘

part_numbers 1 ── n compatibility_hints
part_numbers 1 ── n part_relations

monthly_sync_runs 1 ── n monthly_sync_events

part_numbers 1 ── n part_term_mappings
vin_decode_requests n ── 0..1 vin_vehicle_mappings n ── 1 vin_decode_responses
vin_vehicle_mappings 1 ── n vin_part_fitments n ── 1 part_numbers
reconciliation_cases ──► admin override/audit
```

### State、raw 與 archive

| Table | 用途與主要約束 |
|---|---|
| `schema_migrations` | MySQL schema version。 |
| `crawl_runs` | 唯一 `run_key`、設定、狀態、block reason 與統計。 |
| `crawl_queue` | `(run_id,url_hash)` 唯一；pending／lease／attempt／worker／fencing／terminal state。 |
| `discovery_edges` | seed、robots、sitemap、HTML link 的發現來源；generated SHA-256 natural key。 |
| `response_bodies` | 對未壓縮 bytes 計算 SHA-256，再以 zlib 壓縮；相同 body 只存一次。 |
| `http_responses` | 每次實際 response 的 URL、redirect、status、headers、bytes、body SHA、challenge 與 queue fencing token。`Set-Cookie` 會 redact。 |
| `archive_captures` | Common Crawl／Wayback／owned export 的 capture time、collection、WARC locator、digest、truncation、metadata。 |
| `archive_import_manifests` | archive index manifest、來源與 selected count。 |
| `archive_import_items` | 可重入 archive queue；claim 使用 lease 與 fencing token；parse/challenge/http error 是永久終態。 |
| `sqlite_imports` / `sqlite_import_items` | 舊 SQLite snapshot 的唯讀遷移 manifest、cursor、reconciliation 與 quarantine。 |
| `parse_failures` | parser/type/error/context；raw HTML 不重複存放。 |
| `robots_snapshots` | robots response、User-Agent 與 body hash 的稽核快照。 |
| `monthly_sync_runs` | 每月 `YYYY-MM` 唯一 run、owner、lease、fencing、attempt、四個 source 狀態／run key 與 summary。 |
| `monthly_sync_events` | NHTSA／PartSouq child stdout、錯誤、progress 與 orchestrator lifecycle；依 run/source/type 可查。 |

### Normalized 與 provenance

| Table | 用途 |
|---|---|
| `record_sources` | normalized record → response、parser 名稱／版本、原始 source URL。 |
| `vehicle_configurations` | 品牌、車名、model、Prod Period、production range、catalog code。 |
| `taxonomy_nodes` | 任意深度 parent-child 分類樹與完整 path。 |
| `diagrams` | 車型、taxonomy、diagram code/name/range。 |
| `part_numbers` | 原始料號、normalized search value、英文名；前導 0 保留。 |
| `part_occurrences` | 同料號在特定 vehicle／diagram／callout／range／condition／note 的一次出現。 |
| `fitments` | occurrence 的適用主張、confidence、derivation、verified 狀態。Archive 固定 unverified。 |
| `compatibility_hints` | `/search/all` 的粗略相容文字，不會建立 verified fitment。 |
| `part_relations` | 網站明示 replacement／substitution；不做 transitive closure。 |
| `part_term_mappings` | PartSouq 英文零件名稱投影、站方中文名稱與中文俗稱的對照工作集。中文缺值不會自動杜撰。 |
| `vin_decode_requests` | 站方輸入 VIN 的持久 queue；attempt、lease、worker 與 fencing 防止重複完成。 |
| `vin_decode_responses` | NHTSA vPIC batch raw JSON、HTTP status、headers、bytes 與 SHA-256。 |
| `vin_vehicle_mappings` | VIN → 品牌、型號、系列／車身樣式、年份、Trim、引擎形式／汽缸數／排氣量／型號／製造商、燃料、驅動、變速箱、生產國；可由站方明確連結 PartSouq vehicle。 |
| `vin_part_fitments` | VIN 完成 PartSouq vehicle 連結後投影的料號適用關係；預設未驗證。 |
| `reconciliation_cases` | 缺中文名稱、缺 VIN→PartSouq vehicle 連結等對帳待辦與證據。 |

所有可能包含 `TEXT` 或 `NULL` 的 logical identity 都使用 generated `natural_key_sha256` unique key，避免 MySQL 前綴 unique 與 nullable unique 的歧義。原始 URL 保存在 `LONGTEXT`，不會為了 index 截斷；queue 另以完整 URL 的 SHA-256 去重。

### Queue claim 與 raw-first transaction

MySQL worker 在短 transaction 內：

1. `SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1`。
2. 將 item 改為 `in_progress`、增加 `fencing_token`、設定 server UTC lease。
3. 立即 commit；HTTP request 期間不持鎖。
4. body、response、解析結果與 queue terminal transition 依 raw-first 規則寫入。
5. renew／finish 都要求 `worker_id + fencing_token` 相符；租約過期的舊 worker 會收到 lease-lost，不能覆寫新結果。

Ingest 以一個 response 為 transaction，先在 Python 依 natural key 去重，再按 vehicle → taxonomy → diagram／part → occurrence → fitment → provenance 做批次 insert/select。每層最多固定數量的 round trip；不會對每個 part 做 SELECT+INSERT。

### 從 fitment 回查 raw response

```sql
SELECT
    f.id AS fitment_id,
    rs.response_id,
    hr.requested_url,
    hr.body_sha256,
    rs.parser_name,
    rs.parser_version,
    ac.archive_source,
    ac.captured_at
FROM fitments AS f
JOIN record_sources AS rs
  ON rs.record_type = 'fitment' AND rs.record_id = f.id
JOIN http_responses AS hr ON hr.id = rs.response_id
LEFT JOIN archive_captures AS ac ON ac.response_id = hr.id
WHERE f.id = 1;
```

```bash
partsouq-crawler dump-response \
  --response-id 1 \
  --output output/response-1.html
```

### 健康檢查

```bash
partsouq-crawler db-status
partsouq-crawler monthly-status --period 2026-08 --event-limit 200

docker exec -e MYSQL_PWD="$PARTSOUQ_MYSQL_PASSWORD" \
  nhtsa-mysql mysql -upartsouq partsouq -e '
  SELECT version, applied_at FROM schema_migrations ORDER BY version;
  CHECK TABLE crawl_queue, http_responses, archive_captures,
              vehicle_configurations, part_numbers, fitments, record_sources;
'
```

`db-status` 回報各表筆數、raw/compressed bytes、compression ratio、缺 provenance、orphan 與 foreign key 差異。低權限 MySQL 帳號不能讀取 `INNODB_TABLESPACES`，所以 `database_bytes_kind=mysql_statistics_lower_bound` 明確表示該值是 statistics 與已壓縮 payload 的保守下限，不冒充實體 tablespace 大小。MySQL 已由 InnoDB 強制 FK；health report 另以 explicit orphan query 驗證 normalized polymorphic provenance。

## 站方 CRUD overlay、VIN 與對帳

權威 DDL：`src/partsouq_crawler/admin/mysql_schema.sql`。

```text
source tables (SELECT only for partsouq_admin)
        │
        └── admin_override_heads 1 ── n admin_override_events
```

- `admin_override_heads`：每筆 source record 或 manual UUID 的目前 overlay、狀態、revision、base SHA、actor、reason。
- `admin_override_events`：append-only create／update／retire／restore 事件，保存 before／after、revision、base SHA、actor、reason。
- Source record 不會 UPDATE／DELETE。`retire` 是可恢復的 overlay tombstone。
- Update transaction 先 `FOR SHARE` 鎖 source、再 `FOR UPDATE` 鎖 head；`expected_revision` 不符回 409。
- `partsouq_admin` 只有 source `SELECT`、heads `INSERT/UPDATE`、events `INSERT`；對 source `UPDATE` 會由 MySQL 拒絕。
- 後台另可對 `vin_decode_requests` 做受限 `INSERT/UPDATE`，不能寫 NHTSA raw response、PartSouq source 或 normalized tables。
- `part_term_mappings`、`vin_vehicle_mappings`、`vin_part_fitments`、`reconciliation_cases` 也是 source record；站方修改仍只進 overlay/audit。
- 對帳案件的 `current_json`、`candidate_json`、`evidence_json` 保留機器資料；站方可覆寫狀態、嚴重度、留言、負責人與結案說明。

後台列表先用 keyset query 找當頁 key，再用兩次固定 batch query 取得 source/manual payload；每頁恰好 3 次 SQL。Source prefix search 先走各欄 index 的 `UNION` candidate set，只有小型 overlay 表做 JSON substring search。唯讀 `/monitoring` 也固定 3 次 SQL，顯示 monthly run/event 與 PartSouq queue／response／429／challenge，不修改任何 source 或 audit table。

## NHTSA MySQL

權威 DDL：`src/partsouq_crawler/nhtsa/mysql_schema.sql`。

```text
nhtsa_sync_runs
        │
        └── nhtsa_source_artifacts ── nhtsa_artifact_members
                    │
                    ├── nhtsa_artifact_records ── nhtsa_record_versions
                    └── nhtsa_rejected_rows

nhtsa_current_artifacts ── nhtsa_current_records (view)
```

| Table / View | 用途 |
|---|---|
| `nhtsa_sync_runs` | bulk／API run、scope、狀態與統計。 |
| `nhtsa_source_artifacts` | URL、headers、raw path、SHA-256、parser version、status 與行數。 |
| `nhtsa_artifact_members` | ZIP member／response.json 的 bytes、CRC、欄位及 schema hash。 |
| `nhtsa_record_versions` | `(dataset,natural key,payload SHA)` 的不可覆寫完整 JSON 版本。 |
| `nhtsa_artifact_records` | 原始 artifact/member/line → record version lineage。 |
| `nhtsa_rejected_rows` | 無法解析列及錯誤；不會進 current。 |
| `nhtsa_current_artifacts` | 全 scope 驗證成功後原子更新的 current pointer。 |
| `nhtsa_current_records` | current payload 加來源 URL、artifact SHA、member、line、parser version。 |

```sql
SELECT dataset_name, COUNT(*)
FROM nhtsa_current_records
GROUP BY dataset_name
ORDER BY dataset_name;

SELECT dataset_name, source_key, status, source_rows, rejected_rows, sha256
FROM nhtsa_source_artifacts
ORDER BY id DESC;
```

## 備份與資料保護

```bash
scripts/backup_database.sh
```

Script 使用 MySQL consistent snapshot：`mysqldump --single-transaction --quick --hex-blob`，並寫同名 `.sha256`。Raw body、archive URL、NHTSA artifact 與 export 可能含 VIN、`ssd` 或其他高熵 query token；備份只放 Git ignore 的 `output/`，不得上傳 GitHub。
