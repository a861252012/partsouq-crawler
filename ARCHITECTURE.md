# Architecture

PartSouq 與 NHTSA 共用 CLI package，但下載、資料庫與完成條件互相獨立。

## Data flow

```text
Seed / robots / sitemap
          │
          ▼
SQLite persistent queue ── lease/recover/resume
          │
          ▼
aiohttp GET ── stable UA / CookieJar / per-host delay / retry
          │
          ▼
Store headers + status + raw body hash/zlib FIRST
          │
          ├── Cloudflare/access challenge ── blocked circuit breaker
          │
          └── HTML/XML
                 │
                 ├── sitemap/link discovery ── new queue rows
                 └── classifier + generic/brand parser
                               │
                               ▼
                    normalized records + provenance
                               │
                         ┌─────┴─────┐
                         ▼           ▼
                      export       reparse
```

## 元件

- `crawl.engine`：run lifecycle、persistent queue worker、status transition、signal pause 與 challenge circuit breaker。
- `crawl.fetcher`：單次 GET。正常 CookieJar、固定 User-Agent、timeout 與 per-host delay；不含 bypass 能力。
- `crawl.robots` / `crawl.sitemap` / `crawl.discovery`：robots gate、nested/XML.GZ sitemap 與 HTML/data/meta/simple-location URL 發現。
- `crawl.classifier`：URL route、query、DOM heading 與 table header 共同分類。
- `db.repository`：單一 aiosqlite connection 與 write lock；raw response、queue、status、backup 與 DB health query。
- `parsers.base`：generic metadata/table/breadcrumb parser。Toyota、Audi、Renault adapter 只處理欄位名稱差異。
- `services.ingest`：冪等 normalized 寫入與 `record_sources`。
- `services.archive_import`：匯入 Common Crawl、Wayback 或使用者合法持有的 HTML；不連線 PartSouq，先保存 raw response 與 archive provenance，再以 unverified historical derivation 寫入。
- `services.reparse`：只讀 raw body，重新分類與解析，不發 HTTP request。
- `services.export`：verified fitment 為預設；compatibility hint 與 unverified historical fitment 都必須明確 opt in。

## 歷史封存資料流

```text
Common Crawl WARC / Wayback capture / lawful owned HTML
                         │
                         ▼
           raw response + SHA-256 FIRST
                         │
                         ▼
  archive_captures(capture time / digest / WARC location / truncation)
                         │
                         ▼
             parser + normalized records
                         │
                         ▼
      is_verified=0 + historical_archive derivation
```

歷史封存 run 固定為 `completed_with_gaps`，不會更新 live/current view。截斷頁可保存已出現的直接記錄，但不能宣稱該頁或該車型完整。

## 狀態與提交順序

Queue 工作先取得有期限 lease。收到 response 後的提交順序固定為：

1. `response_bodies` 去重保存。
2. `http_responses` 保存 request/final URL、status、headers 與 SHA。
3. challenge detector 判斷；命中就將 queue 設為 `challenged`、run 設為 `blocked`，停止發新 request。
4. 正常 HTML/XML 才能 discovery 與 parse。
5. normalized record 與 provenance 同一 transaction 寫入。
6. queue 最後才轉成 `done`。

程序在第 1 至 5 步崩潰時，lease 到期後會恢復 pending。已存在 raw response 仍保留作證據；正常重啟不會重抓已經 `done` 的 URL。

429 會保存 response、尊重 `Retry-After`、將工作延後並 pause run。500/502/503/504 與 transport timeout 才會 exponential backoff + jitter。404/410 是 `gone` terminal。parser error 是 `parse_failed`，不重抓即可用 `reparse` 修復。

## URL identity

去重只做：scheme/hostname 小寫、default port 移除、fragment 移除、空 path 轉 `/`。path 與原始 query string保持不變，參數不排序、不移除，`ssd` 視為 opaque token。Redirect history 與 final URL 存在 response，不會覆寫 queue 的 requested URL。

## 完整性邊界

Crawler 的閉包是 seed、robots sitemap、nested sitemap 與已取得 HTML 公開連結所發現的 allowed URLs。它不枚舉 VIN/Frame、不登入，也不能證明網站中無連結的隱藏資料存在與否。

`completed` 必須 queue 耗盡且沒有 failed/challenged/skipped_robots/parse_failed。任何 gap 都是 `completed_with_gaps`，外部阻斷則是 `blocked`。`crawl-status` 另外以 foreign keys 與 provenance 完整性計算 `strict_complete`。

## NHTSA data flow

```text
NHTSA official bulk files / allowlisted API endpoints
                         │
                         ▼
       content-addressed raw artifact on disk
                         │
                         ▼
      ZIP + CRC + member + schema validation
                         │
               ┌─────────┴─────────┐
               ▼                   ▼
       parsed source rows     rejected rows
               │                   │
               ▼                   ▼
      MySQL record versions    quarantined artifact
               │
               ▼
        artifact/row lineage
               │
       all selected sources pass
               │
               ▼
   atomic nhtsa_current_artifacts publish
```

NHTSA 設計邊界：

- API client 只接受 `vpic.nhtsa.dot.gov` 與 `api.nhtsa.gov` 的明列 endpoint；VIN decode 與 VIN 枚舉不在 allowlist。
- bulk 檔與 API JSON 都先保存 raw bytes，再解析及入庫。
- parser schema 改變時以版本隔離；已 quarantined 的內容不會被靜默重用。
- `nhtsa_record_versions` 保存歷史 payload；`nhtsa_current_artifacts` 只指向本次完整驗證的來源集合。
- 同一官方 artifact 的重複列或不同更新日期會保留每個來源行號；不同 artifact 對同一 natural key 提供不同內容時會阻擋發佈。
- 每一批資料各自 transaction，不用單一超大 transaction；範圍發佈本身保持原子性。
