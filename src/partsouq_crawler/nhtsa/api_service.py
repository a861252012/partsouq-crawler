from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from partsouq_crawler.logging import CrawlLogger
from partsouq_crawler.nhtsa.api import NhtsaApiParser
from partsouq_crawler.nhtsa.api_client import NhtsaApiClient
from partsouq_crawler.nhtsa.config import NhtsaConfig
from partsouq_crawler.nhtsa.datasets import CSSI_SOURCES, VPIC_FIXED_SOURCES, ApiSource
from partsouq_crawler.nhtsa.models import ApiDocument
from partsouq_crawler.nhtsa.repository import NhtsaMySQLRepository
from partsouq_crawler.nhtsa.service import BATCH_SIZE, NhtsaRecordWriter

API_PARSER_NAME = "nhtsa_official_api_json"
API_PARSER_VERSION = "3"
API_REQUEST_BUDGET = 500
MANUFACTURER_PAGE_SIZE = 100
MAX_MANUFACTURER_PAGES = 500


@dataclass(frozen=True, slots=True)
class ApiSourceImport:
    artifact_id: int
    document: ApiDocument
    downloaded: bool
    new_versions: int


class NhtsaApiSyncService:
    def __init__(
        self,
        repository: NhtsaMySQLRepository,
        config: NhtsaConfig,
        *,
        parser: NhtsaApiParser | None = None,
        logger: CrawlLogger | None = None,
    ) -> None:
        self.repository = repository
        self.config = config
        self.parser = parser or NhtsaApiParser()
        self.logger = logger
        self.writer = NhtsaRecordWriter(repository)
        self.request_count = 0

    async def run(self, *, run_key: str, scope_name: str) -> dict[str, Any]:
        if scope_name not in {"all", "vpic", "cssi"}:
            raise ValueError(f"unsupported NHTSA API scope: {scope_name}")
        source_groups = []
        if scope_name in {"all", "vpic"}:
            source_groups.append("vpic")
        if scope_name in {"all", "cssi"}:
            source_groups.append("cssi")
        run_id = self.repository.start_run(run_key, f"api-{scope_name}", source_groups)
        downloaded = 0
        reused = 0
        source_rows = 0
        new_versions = 0
        rejected_rows = 0
        publishable: list[tuple[str, str, int]] = []
        replace_datasets: list[str] = []
        active_artifact_id: int | None = None
        self._event("nhtsa_api_run_started", run_key=run_key, scope=scope_name)
        try:
            async with NhtsaApiClient(self.config) as client:
                if scope_name in {"all", "vpic"}:
                    variables: ApiDocument | None = None
                    for source in VPIC_FIXED_SOURCES:
                        imported = await self._sync_source(client, source)
                        active_artifact_id = imported.artifact_id
                        downloaded += int(imported.downloaded)
                        reused += int(not imported.downloaded)
                        source_rows += imported.document.count
                        new_versions += imported.new_versions
                        rejected_rows += len(imported.document.rejections)
                        publishable.append((source.dataset_name, source.key, imported.artifact_id))
                        active_artifact_id = None
                        if source.dataset_name == "vpic_variables":
                            variables = imported.document

                    for page in range(1, MAX_MANUFACTURER_PAGES + 1):
                        source = ApiSource(
                            key=f"vpic_manufacturers_page_{page:03d}",
                            dataset_name="vpic_manufacturers",
                            url=(
                                "https://vpic.nhtsa.dot.gov/api/vehicles/"
                                f"GetAllManufacturers?format=json&page={page}"
                            ),
                        )
                        imported = await self._sync_source(client, source)
                        downloaded += int(imported.downloaded)
                        reused += int(not imported.downloaded)
                        source_rows += imported.document.count
                        new_versions += imported.new_versions
                        rejected_rows += len(imported.document.rejections)
                        if imported.document.count:
                            publishable.append(
                                (source.dataset_name, source.key, imported.artifact_id)
                            )
                        if imported.document.count < MANUFACTURER_PAGE_SIZE:
                            break
                    else:
                        raise ValueError("vPIC manufacturer pagination exceeded safety limit")

                    if variables is None:
                        raise ValueError("vPIC variable list was not collected")
                    variable_ids = sorted(
                        {
                            int(record.external_id)
                            for record in variables.records
                            if record.external_id and record.external_id.isdigit()
                        }
                    )
                    for variable_id in variable_ids:
                        source = ApiSource(
                            key=f"vpic_variable_{variable_id}_values",
                            dataset_name="vpic_variable_values",
                            url=(
                                "https://vpic.nhtsa.dot.gov/api/vehicles/"
                                f"GetVehicleVariableValuesList/{variable_id}?format=json"
                            ),
                            context=(("Variable_ID", str(variable_id)),),
                        )
                        imported = await self._sync_source(client, source)
                        downloaded += int(imported.downloaded)
                        reused += int(not imported.downloaded)
                        source_rows += imported.document.count
                        new_versions += imported.new_versions
                        rejected_rows += len(imported.document.rejections)
                        publishable.append((source.dataset_name, source.key, imported.artifact_id))
                    replace_datasets.extend(
                        (
                            "vpic_makes",
                            "vpic_models",
                            "vpic_manufacturers",
                            "vpic_variables",
                            "vpic_variable_values",
                        )
                    )

                if scope_name in {"all", "cssi"}:
                    for source in CSSI_SOURCES:
                        imported = await self._sync_source(client, source)
                        downloaded += int(imported.downloaded)
                        reused += int(not imported.downloaded)
                        source_rows += imported.document.count
                        new_versions += imported.new_versions
                        rejected_rows += len(imported.document.rejections)
                        publishable.append((source.dataset_name, source.key, imported.artifact_id))
                    replace_datasets.append("cssi_stations")

            if rejected_rows:
                raise ValueError(f"NHTSA API sync rejected {rejected_rows} records")
            self.repository.publish_artifacts(
                publishable,
                replace_datasets=replace_datasets,
            )
            self.repository.finish_run(
                run_id,
                status="completed",
                downloaded=downloaded,
                reused=reused,
                source_rows=source_rows,
                new_versions=new_versions,
                rejected_rows=rejected_rows,
            )
            self._event(
                "nhtsa_api_run_completed",
                run_key=run_key,
                api_requests=self.request_count,
                source_rows=source_rows,
                downloaded=downloaded,
                reused=reused,
            )
            return {
                "run_id": run_id,
                "run_key": run_key,
                "scope": scope_name,
                "status": "completed",
                "api_requests": self.request_count,
                "artifacts_downloaded": downloaded,
                "artifacts_reused": reused,
                "source_rows": source_rows,
                "new_versions": new_versions,
                "rejected_rows": rejected_rows,
                "published_sources": len(publishable),
            }
        except asyncio.CancelledError:
            self.repository.finish_run(
                run_id,
                status="interrupted",
                downloaded=downloaded,
                reused=reused,
                source_rows=source_rows,
                new_versions=new_versions,
                rejected_rows=rejected_rows,
                error_message="sync interrupted",
            )
            raise
        except Exception as error:
            if active_artifact_id is not None:
                self.repository.quarantine_artifact(active_artifact_id, str(error))
            self.repository.finish_run(
                run_id,
                status="failed",
                downloaded=downloaded,
                reused=reused,
                source_rows=source_rows,
                new_versions=new_versions,
                rejected_rows=rejected_rows,
                error_message=f"{type(error).__name__}: {error}",
            )
            self._event(
                "nhtsa_api_run_failed",
                run_key=run_key,
                status="failed",
                api_requests=self.request_count,
                error_type=type(error).__name__,
                error=str(error),
            )
            return {
                "run_id": run_id,
                "run_key": run_key,
                "scope": scope_name,
                "status": "failed",
                "api_requests": self.request_count,
                "error_type": type(error).__name__,
                "error": str(error),
                "artifacts_downloaded": downloaded,
                "artifacts_reused": reused,
                "source_rows": source_rows,
                "new_versions": new_versions,
                "rejected_rows": rejected_rows,
                "published_sources": 0,
            }

    async def _sync_source(
        self,
        client: NhtsaApiClient,
        source: ApiSource,
    ) -> ApiSourceImport:
        self.request_count += 1
        if self.request_count > API_REQUEST_BUDGET:
            raise ValueError(f"NHTSA API request budget exceeded ({API_REQUEST_BUDGET})")
        self._event(
            "nhtsa_api_source_started",
            source_key=source.key,
            dataset=source.dataset_name,
            request_number=self.request_count,
        )
        current = self.repository.current_artifact(source.dataset_name, source.key)
        download, body = await client.fetch(source, current_artifact=current)
        if download.reused_artifact_id is not None:
            if current is None:
                raise ValueError("reused API response has no current artifact")
            body = await asyncio.to_thread(Path(str(current["stored_path"])).read_bytes)
            document = self.parser.parse(body, source)
            self._event(
                "nhtsa_api_source_reused",
                source_key=source.key,
                artifact_id=download.reused_artifact_id,
                source_rows=document.count,
            )
            return ApiSourceImport(download.reused_artifact_id, document, False, 0)
        if download.sha256 is None or download.path is None or body is None:
            raise ValueError(f"{source.key} API download has no content")
        existing = self.repository.artifact_by_content(
            source.dataset_name,
            source.key,
            download.sha256,
            API_PARSER_VERSION,
        )
        if existing and existing["status"] == "imported":
            document = self.parser.parse(body, source)
            self._event(
                "nhtsa_api_source_reused",
                source_key=source.key,
                artifact_id=int(str(existing["id"])),
                source_rows=document.count,
            )
            return ApiSourceImport(int(str(existing["id"])), document, False, 0)
        if existing and existing["status"] == "quarantined":
            raise ValueError(
                f"{source.key} API content is quarantined: {existing['error_message']}"
            )
        artifact_id = self.repository.create_artifact(
            dataset_name=source.dataset_name,
            source_key=source.key,
            source_url=source.url,
            download=download,
            parser_name=API_PARSER_NAME,
            parser_version=API_PARSER_VERSION,
        )
        try:
            document = self.parser.parse(body, source)
            current_schema = self.repository.current_schema(source.dataset_name, source.key)
            current_rows = int(str(current["source_rows"])) if current else 0
            if (
                current_schema is not None
                and current_rows > 0
                and document.count > 0
                and current_schema != document.member.schema_sha256
            ):
                raise ValueError(
                    f"API schema drift for {source.key}: "
                    f"{current_schema} -> {document.member.schema_sha256}"
                )
            self.repository.store_member(artifact_id, document.member)
            self.repository.reset_artifact_import(artifact_id)
            new_versions = 0
            duplicate_rejections = 0
            records = list(document.records)
            for index in range(0, len(records), BATCH_SIZE):
                added, rejected = self.writer.insert(
                    artifact_id,
                    records[index : index + BATCH_SIZE],
                )
                new_versions += added
                duplicate_rejections += rejected
            self.repository.insert_rejections(artifact_id, document.rejections)
            total_rejections = duplicate_rejections + len(document.rejections)
            self.repository.complete_artifact(
                artifact_id,
                source_rows=document.count,
                new_versions=new_versions,
                rejected_rows=total_rejections,
            )
            if total_rejections:
                raise ValueError(f"{source.key} rejected {total_rejections} API records")
            self._event(
                "nhtsa_api_source_completed",
                source_key=source.key,
                artifact_id=artifact_id,
                source_rows=document.count,
                new_versions=new_versions,
            )
            return ApiSourceImport(artifact_id, document, True, new_versions)
        except Exception as error:
            self.repository.quarantine_artifact(artifact_id, str(error))
            raise

    def _event(self, event: str, **fields: object) -> None:
        if self.logger is not None:
            self.logger.event(event, **fields)
