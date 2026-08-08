# Database

正式 DB 預設為 `output/partsouq-live.sqlite3`。連線會啟用 foreign keys、WAL 與 5 秒 busy timeout。所有 HTTP body 都先對未壓縮 bytes 計算 SHA-256，再以 zlib 壓縮及內容雜湊去重。

## 關聯

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

## 資料表用途

- `crawl_runs`：run 設定、狀態、block reason 與統計。
- `crawl_queue`：persistent URL queue、attempt、lease、終止狀態與最後 response。
- `discovery_edges`：URL 從 seed、robots、sitemap 或 HTML 被發現的來源邊。
- `http_responses`：每次實際 HTTP response 的 status、headers、redirect、SHA 與 challenge 判斷。`Set-Cookie` 會 redact。
- `response_bodies`：依 SHA-256 去重的 zlib body。
- `record_sources`：normalized record 到 response、parser 名稱與版本的 provenance。
- `vehicle_configurations`：品牌、車名、model、原始 Prod Period 與安全解析結果。
- `taxonomy_nodes`：任意深度的 parent-child 分類樹與完整 path。
- `diagrams`：圖組、Diagram Range 與車輛、分類關聯。
- `part_numbers`：保留原始料號及搜尋用 normalized value；前導 0 不會被移除。
- `part_occurrences`：同料號在特定車輛、diagram、callout、range、condition 的一次出現。
- `fitments`：由 Genuine Catalog diagram/part occurrence 正向建立的 verified 關聯。
- `compatibility_hints`：`/search/all` 的粗略文字；不會建立 verified fitment。
- `part_relations`：網站實際顯示的 substitution/replacement 等關係；不做 transitive closure。
- `parse_failures`：parser/type/error/context；不重複保存 HTML。
- `robots_snapshots`：robots response、UA 與 body hash 的稽核快照。

主要 queue index 是 `(run_id, status, priority, next_attempt_at)`。料號搜尋 index 是 `(part_brand_raw, number_normalized)`。

## 從 fitment 回查 HTML

```sql
SELECT
    f.id AS fitment_id,
    rs.response_id,
    hr.requested_url,
    hr.body_sha256,
    rs.parser_name,
    rs.parser_version
FROM fitments f
JOIN record_sources rs
  ON rs.record_type = 'fitment' AND rs.record_id = f.id
JOIN http_responses hr ON hr.id = rs.response_id
WHERE f.id = 1;
```

取得 `response_id` 後：

```bash
partsouq-crawler dump-response \
  --sqlite output/partsouq-live.sqlite3 \
  --response-id 1 \
  --output output/response-1.html
```

也可用 `--url` 或 `--sha256`，三者擇一。

## Laravel 唯讀 staging 連線

將 Backup API 產生的 snapshot 提供給 Laravel，不要直接讓 staging 讀 crawler 正在寫入的 live DB：

```php
'partsouq' => [
    'driver' => 'sqlite',
    'database' => env('PARTSOUQ_SQLITE'),
    'foreign_key_constraints' => true,
],
```

應以 OS 權限及應用層 connection policy 限制唯讀。Laravel 不應對這個 DB 執行 migration 或 write query。

## WAL、備份與 retention

WAL DB 執行中不可直接 `cp` 主檔，否則 snapshot 可能缺少 WAL 內尚未 checkpoint 的內容。請用：

```bash
partsouq-crawler db-backup --sqlite output/partsouq-live.sqlite3 --output output/snapshot.sqlite3
```

Raw body 是 reparse 與稽核的負載核心，預設不自動刪除。用 `db-status` 檢查 raw/compressed bytes 與 compression ratio，再依明確 retention policy 另行歸檔；本專案不會靜默清理證據。
