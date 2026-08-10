from __future__ import annotations

from partsouq_crawler.crawl.classifier import classify_page
from partsouq_crawler.db.repository import Repository
from partsouq_crawler.parsers.base import CatalogParser, ParseError
from partsouq_crawler.services.ingest import IngestService


class ReparseService:
    def __init__(self, repository: Repository) -> None:
        self.repository = repository
        self.parser = CatalogParser()
        self.ingest = IngestService(repository)

    async def run(
        self,
        *,
        response_id: int | None = None,
        run_id: int | None = None,
        page_type: str | None = None,
        batch_size: int = 250,
    ) -> dict[str, int]:
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        report = {
            "selected": 0,
            "parsed": 0,
            "failed": 0,
            "skipped_http": 0,
            "skipped_non_catalog": 0,
            "resolved_failures": 0,
            "records_inserted": 0,
        }
        after_id: int | None = None
        while True:
            responses = await self.repository.find_responses(
                response_id=response_id,
                run_id=run_id,
                after_id=after_id,
                limit=None if response_id is not None else batch_size,
            )
            if not responses:
                break
            report["selected"] += len(responses)
            for row in responses:
                if int(row["http_status"]) >= 400 or row["is_cloudflare_challenge"]:
                    report["resolved_failures"] += await self.repository.clear_parse_failures(
                        int(row["id"]), "catalog_parser"
                    )
                    report["skipped_http"] += 1
                    continue
                body = self.repository.restore_body(row)
                detected = classify_page(row["final_url"], body, row["charset"] or "utf-8")
                if page_type and detected != page_type:
                    continue
                if detected in {"robots", "sitemap"}:
                    report["resolved_failures"] += await self.repository.clear_parse_failures(
                        int(row["id"]), "catalog_parser"
                    )
                    report["skipped_non_catalog"] += 1
                    continue
                try:
                    parsed = self.parser.parse(row["final_url"], body, row["charset"] or "utf-8")
                    archive_source = row["archive_source"]
                    inserted = await self.ingest.ingest(
                        run_id=int(row["run_id"]),
                        response_id=int(row["id"]),
                        source_url=row["final_url"],
                        parsed=parsed,
                        verified_fitments=archive_source is None,
                        fitment_derivation=(
                            "genuine_catalog_diagram_part_occurrence"
                            if archive_source is None
                            else f"historical_archive_{archive_source}"
                        ),
                    )
                    report["resolved_failures"] += await self.repository.clear_parse_failures(
                        int(row["id"]), "catalog_parser"
                    )
                except ParseError as error:
                    await self.repository.add_parse_failure(
                        int(row["id"]), "catalog_parser", detected, error
                    )
                    report["failed"] += 1
                    continue
                report["parsed"] += 1
                report["records_inserted"] += inserted
            if response_id is not None or len(responses) < batch_size:
                break
            after_id = int(responses[-1]["id"])
        return report

    async def repair_legacy_navigation_taxonomy(self, *, apply: bool) -> dict[str, int | bool]:
        report = await self._legacy_navigation_taxonomy_report()
        report["applied"] = False
        report["sources_deleted"] = 0
        report["roots_deleted"] = 0
        if not apply:
            return report
        if report["linked_diagrams"]:
            raise ValueError("legacy navigation taxonomy is still linked to diagrams")
        if report["linked_admin_overrides"]:
            raise ValueError("legacy navigation taxonomy still has admin overrides")

        async with self.repository.transaction() as connection:
            sources = await connection.execute(
                """
                DELETE FROM record_sources
                WHERE record_type = 'taxonomy_node'
                  AND record_id IN (
                      SELECT id FROM taxonomy_nodes
                      WHERE path_raw = 'Genuine Parts Catalogs'
                         OR path_raw LIKE 'Genuine Parts Catalogs > %%'
                  )
                """
            )
            roots = await connection.execute(
                """
                DELETE FROM taxonomy_nodes
                WHERE path_raw = 'Genuine Parts Catalogs'
                """
            )
            cursor = await connection.execute(
                """
                SELECT COUNT(*) AS count FROM taxonomy_nodes
                WHERE path_raw = 'Genuine Parts Catalogs'
                   OR path_raw LIKE 'Genuine Parts Catalogs > %%'
                """
            )
            row = await cursor.fetchone()
            if row is None or int(row["count"]) != 0:
                raise RuntimeError("legacy navigation taxonomy cleanup was incomplete")
            report["applied"] = True
            report["sources_deleted"] = max(int(sources.rowcount), 0)
            report["roots_deleted"] = max(int(roots.rowcount), 0)
        return report

    async def _legacy_navigation_taxonomy_report(self) -> dict[str, int]:
        cursor = await self.repository.connection.execute(
            """
            SELECT
                COUNT(*) AS bad_nodes,
                (
                    SELECT COUNT(*) FROM record_sources rs
                    JOIN taxonomy_nodes source_node
                      ON source_node.id = rs.record_id
                    WHERE rs.record_type = 'taxonomy_node'
                      AND (
                          source_node.path_raw = 'Genuine Parts Catalogs'
                          OR source_node.path_raw LIKE 'Genuine Parts Catalogs > %%'
                      )
                ) AS bad_sources,
                (
                    SELECT COUNT(*) FROM diagrams d
                    JOIN taxonomy_nodes diagram_node
                      ON diagram_node.id = d.taxonomy_node_id
                    WHERE diagram_node.path_raw = 'Genuine Parts Catalogs'
                       OR diagram_node.path_raw LIKE 'Genuine Parts Catalogs > %%'
                ) AS linked_diagrams
            FROM taxonomy_nodes node
            WHERE node.path_raw = 'Genuine Parts Catalogs'
               OR node.path_raw LIKE 'Genuine Parts Catalogs > %%'
            """
        )
        row = await cursor.fetchone()
        linked_admin_overrides = 0
        if self.repository.backend_name == "mysql":
            override_cursor = await self.repository.connection.execute(
                """
                SELECT COUNT(*) AS count FROM admin_override_heads h
                JOIN taxonomy_nodes override_node
                  ON override_node.id = h.source_record_id
                WHERE h.entity_type = 'taxonomy_nodes'
                  AND (
                      override_node.path_raw = 'Genuine Parts Catalogs'
                      OR override_node.path_raw LIKE 'Genuine Parts Catalogs > %%'
                  )
                """
            )
            override_row = await override_cursor.fetchone()
            linked_admin_overrides = int(override_row["count"]) if override_row else 0
        if row is None:
            return {
                "bad_nodes": 0,
                "bad_sources": 0,
                "linked_diagrams": 0,
                "linked_admin_overrides": linked_admin_overrides,
            }
        return {
            "bad_nodes": int(row["bad_nodes"]),
            "bad_sources": int(row["bad_sources"]),
            "linked_diagrams": int(row["linked_diagrams"]),
            "linked_admin_overrides": linked_admin_overrides,
        }
