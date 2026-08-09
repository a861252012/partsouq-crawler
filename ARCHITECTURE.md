# Architecture

PartSouq crawler、archive importer、CRUD 後台與 NHTSA sync 共用一個 Python package，但各自有明確的資料來源、完成條件與 failure boundary。

## PartSouq live data flow

```text
seed / robots / sitemap / HTML links
                 │
                 ▼
       MySQL persistent queue
       lease + fencing + resume
                 │
                 ▼
       HTTP or Brave/Playwright
       stable UA / low concurrency
                 │
                 ▼
 raw response + SHA-256 + headers FIRST
                 │
        ┌────────┴─────────┐
        ▼                  ▼
 Cloudflare challenge   normal HTML/XML
 blocked circuit        classify/discover/parse
 breaker                   │
                           ▼
             batch normalized records
                     + provenance
```

### Live 元件

- `crawl.engine`：run lifecycle、queue worker、lease heartbeat、fencing、signal pause、challenge circuit breaker。
- `crawl.fetcher`：aiohttp 正常 GET、固定 UA、timeout、Retry-After 與 per-host delay。
- `crawl.browser_fetcher`：標準 Playwright browser transport。使用 fresh context，不讀既有 Brave profile、不搬 cookie、不含 stealth。
- `crawl.robots`／`crawl.sitemap`／`crawl.discovery`：robots gate、nested sitemap、gzip sitemap 與 HTML/data/meta/form URL discovery。
- `crawl.challenge`：集中判斷 403、`cf-mitigated: challenge`、Cloudflare title/body markers。
- `db.mysql_connection`：aiomysql pool、UTC、READ COMMITTED、schema migration、transaction rollback。
- `db.repository`：persistent state、raw-first response、queue fencing、archive queue、status 與 health query。
- `services.ingest`：依資料層次批次 upsert/select，再批次寫 `record_sources`；沒有 per-part SQL loop。
- `services.reparse`：只讀 MySQL raw body，以目前 parser 重播；不連 PartSouq。
- `services.export`：以 `(record_id, provenance_id)` keyset 每批 1,000 筆輸出。

## Challenge failure boundary

Challenge 是來源不可用狀態，不是空型錄：

1. 完整 response 先寫 `response_bodies` 與 `http_responses`。
2. Queue item 變成 `challenged`。
3. Run 變成 `blocked`，保存 reason。
4. 共用熔斷器停止 HTTP、Playwright 或其他正常 driver 取得新 item。
5. CLI 回非 0；`strict_complete=false`。
6. Challenge body 不進 parser，也不會建立 normalized record。

Driver 只可因一般相容性問題切換。已偵測 challenge 後，不允許改用 Selenium、stealth、UA 輪替、代理、CAPTCHA solver 或 `cf_clearance` 搬運繼續嘗試。

## Historical archive data flow

```text
Common Crawl CDXJ/WARC ─┐
Wayback CDX/playback ───┼─► manifest + MySQL archive queue
owned HTML ─────────────┘             │
                                      ▼
                     raw response + archive capture
                                      │
                           challenge/content gate
                                      │
                                      ▼
                         parser + unverified records
                                      │
                                      ▼
                       source_mode=historical_archive
                       current_or_complete=false
```

- Common Crawl 只根據本機 index 的 WARC locator 做 HTTP Range download。
- Wayback 只接受本機 CDX JSON，allowlist 是 `/en/catalog` 與子路徑；VIN path 與明確 17 碼 VIN query 預設排除。
- 每個 index item 先 enqueue；done/challenged/http_error/parse_failed 是永久終態，network failure 可續跑。
- Claim 與 finalize 都帶 fencing token，失效 worker 不能把 item 誤標完成。
- Body、HTTP response 與 `archive_captures` 在同一 transaction 冪等寫入；parser 可以在下一個 transaction 重播。
- Archive fitment 固定 `is_verified=0`，derivation 明確包含 archive source。
- Archive run 固定 `completed_with_gaps`／`current_or_complete=false`，不會取代 live current view。

## Legacy SQLite migration

SQLite 只是一個舊資料輸入格式：

1. 用 SQLite Backup API 建立封閉 snapshot，避免漏掉 WAL。
2. 對 snapshot 與每個未壓縮 body 計算 SHA-256。
3. 依 `archive_captures.id` keyset 分批讀取。
4. MySQL 同一 transaction 寫 body、response、capture 與 import item。
5. 用目前 parser 從 raw body 重建 normalized records；不直接信任舊 normalized table。
6. 對帳 source/target body hash、capture identity、parse count、provenance、orphan 與 FK。
7. 同一 snapshot 重跑時全部 skip；差異或損壞進 quarantine，不能靜默完成。

## CRUD 後台

```text
MySQL immutable source tables ───────► effective record view
             ▲                              ▲
             │ provenance                   │ overlay
             │                              │
raw responses / archive captures     admin_override_heads
                                             │
                                             ▼
                                    admin_override_events
                                      append-only audit
```

- Flask/Jinja/PyMySQL，只綁 `127.0.0.1`。
- Entity/type/table/field 都由固定 allowlist 映射，不接受 URL 拼 SQL identifier。
- `partsouq_admin` 對 source table 只有 SELECT；MySQL 權限是最後一道防線。
- 人工 create/update/retire/restore 寫 overlay，不改 source evidence。
- `expected_revision` + row lock 防止 lost update；衝突回 409。
- CSRF、Strict session cookie、CSP、no-referrer、nosniff 都由 app 固定設定。
- Raw response 不 inline render；raw HTML 只能明確下載。

### No-N+1 contract

- Dashboard：固定 2 次 SQL。
- List：固定 3 次 SQL。第一個 query 只取當頁 key；後兩個 query 批次取得 source 與 manual records。
- Detail：固定 4 次 SQL；audit 與 provenance fanout 各自最多 100 筆。
- List 使用 signed-order keyset，不用 `OFFSET` 或每頁 `COUNT(*)`。
- Prefix search 以每欄 index range 的 `UNION` 產生 source candidates；只有小型 overlay JSON 做 substring search。
- 每條 SQL 都有 query tag 與 normalized fingerprint。測試用 1／100／10,000 筆及高 fanout fixture，要求 query count 與 fingerprint 不變。
- MySQL integration 另驗 source row hash 不變、低權限 direct UPDATE 失敗、CRUD audit 完整。

## NHTSA data flow

```text
official bulk ZIP/CSV or allowlisted API
                   │
                   ▼
      local content-addressed raw artifact
                   │
      ZIP magic / CRC / member / schema gate
                   │
                   ▼
 record versions + artifact/member/line lineage
                   │
      all selected artifacts imported, rejects=0
                   │
                   ▼
       atomic current artifact pointers
                   │
                   ▼
           nhtsa_current_records
```

- Bulk 優先；API client 只能呼叫程式內明列的 CSSI／vPIC 集合 endpoint。
- VIN decode、VIN range、任意 URL 都由 policy guard 拒絕。
- 完整官方 payload 存 MySQL JSON；raw bytes 另依 SHA-256 保存在 Git ignore 目錄。
- Artifact/member/source line/parser version 全部可追溯。
- Quarantined artifact 與 rejected row 保留作稽核，但不會更新 current pointer。

## 完成條件

PartSouq live 的 strict completion：

```text
queue exhausted
AND failed = 0
AND challenged = 0
AND skipped_robots = 0
AND parse_failed = 0
AND missing provenance = 0
AND foreign key violations = 0
```

「queue exhausted」只代表所有已由 seed／robots／sitemap／HTML 發現且在 allowlist 的 URL 已處理；不代表不可發現、私人或受存取控制的頁面也已取得。

NHTSA current completion：指定 scope 的 expected source manifest 全部 imported、source rows 對帳、rejected=0，才允許發布 current pointers。
