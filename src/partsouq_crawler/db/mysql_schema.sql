CREATE TABLE IF NOT EXISTS schema_migrations (
    version INT UNSIGNED PRIMARY KEY,
    applied_at DATETIME(6) NOT NULL
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS crawl_runs (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    run_key VARCHAR(191) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
    seed_urls_json JSON NOT NULL,
    config_json JSON NOT NULL,
    status VARCHAR(32) NOT NULL,
    blocked_reason TEXT NULL,
    started_at DATETIME(6) NULL,
    updated_at DATETIME(6) NOT NULL,
    ended_at DATETIME(6) NULL,
    pages_discovered BIGINT UNSIGNED NOT NULL DEFAULT 0,
    pages_done BIGINT UNSIGNED NOT NULL DEFAULT 0,
    pages_failed BIGINT UNSIGNED NOT NULL DEFAULT 0,
    pages_challenged BIGINT UNSIGNED NOT NULL DEFAULT 0,
    records_extracted BIGINT UNSIGNED NOT NULL DEFAULT 0,
    UNIQUE KEY uq_crawl_runs_key (run_key),
    INDEX idx_crawl_runs_status (status, id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS crawl_queue (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    run_id BIGINT UNSIGNED NOT NULL,
    requested_url LONGTEXT NOT NULL,
    url_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    parent_url LONGTEXT NULL,
    depth INT UNSIGNED NOT NULL,
    page_type_hint VARCHAR(64) NULL,
    priority INT NOT NULL DEFAULT 0,
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    attempts INT UNSIGNED NOT NULL DEFAULT 0,
    worker_id VARCHAR(191) NULL,
    fencing_token BIGINT UNSIGNED NOT NULL DEFAULT 0,
    lease_expires_at DATETIME(6) NULL,
    next_attempt_at DATETIME(6) NULL,
    last_error TEXT NULL,
    response_id BIGINT UNSIGNED NULL,
    discovered_at DATETIME(6) NOT NULL,
    started_at DATETIME(6) NULL,
    finished_at DATETIME(6) NULL,
    UNIQUE KEY uq_crawl_queue_url (run_id, url_hash),
    INDEX idx_crawl_queue_schedule (run_id, status, priority DESC, id, next_attempt_at),
    INDEX idx_crawl_queue_lease (run_id, status, lease_expires_at),
    INDEX idx_crawl_queue_worker (run_id, worker_id, status),
    CONSTRAINT fk_crawl_queue_run
        FOREIGN KEY (run_id) REFERENCES crawl_runs(id) ON DELETE CASCADE
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS discovery_edges (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    run_id BIGINT UNSIGNED NOT NULL,
    source_response_id BIGINT UNSIGNED NULL,
    parent_url LONGTEXT NULL,
    discovered_url LONGTEXT NOT NULL,
    discovery_method VARCHAR(64) NOT NULL,
    discovered_at DATETIME(6) NOT NULL,
    natural_key_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin
        GENERATED ALWAYS AS (
            SHA2(CAST(JSON_ARRAY(run_id, parent_url, discovered_url, discovery_method) AS CHAR), 256)
        ) STORED,
    UNIQUE KEY uq_discovery_edge (natural_key_sha256),
    INDEX idx_discovery_edges_run (run_id, id),
    CONSTRAINT fk_discovery_edges_run
        FOREIGN KEY (run_id) REFERENCES crawl_runs(id) ON DELETE RESTRICT
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS response_bodies (
    sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
    compression VARCHAR(16) NOT NULL,
    body_blob LONGBLOB NOT NULL,
    original_bytes BIGINT UNSIGNED NOT NULL,
    stored_bytes BIGINT UNSIGNED NOT NULL,
    created_at DATETIME(6) NOT NULL
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS http_responses (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    run_id BIGINT UNSIGNED NOT NULL,
    queue_id BIGINT UNSIGNED NULL,
    queue_fencing_token BIGINT UNSIGNED NULL,
    requested_url LONGTEXT NOT NULL,
    requested_url_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    final_url LONGTEXT NOT NULL,
    redirect_chain_json JSON NOT NULL,
    http_status SMALLINT UNSIGNED NOT NULL,
    response_headers_json JSON NOT NULL,
    content_type VARCHAR(255) NULL,
    charset VARCHAR(64) NULL,
    body_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    response_bytes BIGINT UNSIGNED NOT NULL,
    elapsed_ms BIGINT UNSIGNED NOT NULL,
    attempt INT UNSIGNED NOT NULL,
    is_cloudflare_challenge TINYINT(1) NOT NULL DEFAULT 0,
    challenge_reason VARCHAR(255) NULL,
    import_key_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NULL,
    fetched_at DATETIME(6) NOT NULL,
    UNIQUE KEY uq_http_response_import (import_key_sha256),
    INDEX idx_http_responses_run_url (run_id, requested_url_hash, id),
    INDEX idx_http_responses_queue (queue_id, id),
    INDEX idx_http_responses_body (body_sha256, id),
    CONSTRAINT fk_http_response_run
        FOREIGN KEY (run_id) REFERENCES crawl_runs(id) ON DELETE CASCADE,
    CONSTRAINT fk_http_response_queue
        FOREIGN KEY (queue_id) REFERENCES crawl_queue(id) ON DELETE SET NULL,
    CONSTRAINT fk_http_response_body
        FOREIGN KEY (body_sha256) REFERENCES response_bodies(sha256)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS archive_captures (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    response_id BIGINT UNSIGNED NOT NULL,
    capture_key_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    archive_source VARCHAR(64) NOT NULL,
    collection_name VARCHAR(191) NULL,
    captured_at DATETIME(6) NOT NULL,
    warc_filename TEXT NULL,
    warc_offset BIGINT UNSIGNED NULL,
    warc_length BIGINT UNSIGNED NULL,
    archive_digest VARCHAR(255) NULL,
    truncation_reason VARCHAR(255) NULL,
    metadata_json JSON NOT NULL,
    source_snapshot_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NULL,
    source_capture_id BIGINT UNSIGNED NULL,
    imported_at DATETIME(6) NOT NULL,
    UNIQUE KEY uq_archive_capture_response (response_id),
    UNIQUE KEY uq_archive_capture_key (capture_key_sha256),
    INDEX idx_archive_captures_source_time (archive_source, captured_at, id),
    CONSTRAINT fk_archive_capture_response
        FOREIGN KEY (response_id) REFERENCES http_responses(id) ON DELETE CASCADE
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS record_sources (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    record_type VARCHAR(64) NOT NULL,
    record_id BIGINT UNSIGNED NOT NULL,
    response_id BIGINT UNSIGNED NOT NULL,
    parser_name VARCHAR(128) NOT NULL,
    parser_version VARCHAR(64) NOT NULL,
    source_url LONGTEXT NOT NULL,
    extracted_at DATETIME(6) NOT NULL,
    UNIQUE KEY uq_record_source (
        record_type, record_id, response_id, parser_name, parser_version
    ),
    INDEX idx_record_sources_record (record_type, record_id, id),
    INDEX idx_record_sources_response (response_id, id),
    CONSTRAINT fk_record_source_response
        FOREIGN KEY (response_id) REFERENCES http_responses(id) ON DELETE CASCADE
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS vehicle_configurations (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    catalog_brand VARCHAR(255) NULL,
    brand_raw VARCHAR(255) NULL,
    brand_normalized VARCHAR(255) NULL,
    name_raw TEXT NULL,
    model_raw TEXT NULL,
    description_raw TEXT NULL,
    options_raw TEXT NULL,
    prod_period_raw VARCHAR(255) NULL,
    production_from VARCHAR(32) NULL,
    production_to VARCHAR(32) NULL,
    production_precision VARCHAR(32) NULL,
    catalog_code VARCHAR(255) NULL,
    vehicle_external_id VARCHAR(255) NULL,
    metadata_json JSON NOT NULL,
    source_url LONGTEXT NOT NULL,
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    natural_key_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin
        GENERATED ALWAYS AS (
            SHA2(CAST(JSON_ARRAY(
                catalog_brand, vehicle_external_id, model_raw, prod_period_raw, source_url
            ) AS CHAR), 256)
        ) STORED,
    UNIQUE KEY uq_vehicle_natural_key (natural_key_sha256),
    INDEX idx_vehicle_brand_model (catalog_brand, vehicle_external_id, id),
    INDEX idx_vehicle_catalog_code (catalog_code, id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS taxonomy_nodes (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    vehicle_configuration_id BIGINT UNSIGNED NOT NULL,
    parent_id BIGINT UNSIGNED NULL,
    depth INT UNSIGNED NOT NULL,
    code_raw VARCHAR(255) NULL,
    name_raw TEXT NOT NULL,
    path_raw TEXT NOT NULL,
    source_url LONGTEXT NOT NULL,
    natural_key_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin
        GENERATED ALWAYS AS (
            SHA2(CAST(JSON_ARRAY(vehicle_configuration_id, path_raw) AS CHAR), 256)
        ) STORED,
    UNIQUE KEY uq_taxonomy_natural_key (natural_key_sha256),
    INDEX idx_taxonomy_vehicle_parent (vehicle_configuration_id, parent_id, id),
    CONSTRAINT fk_taxonomy_vehicle
        FOREIGN KEY (vehicle_configuration_id) REFERENCES vehicle_configurations(id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_taxonomy_parent
        FOREIGN KEY (parent_id) REFERENCES taxonomy_nodes(id) ON DELETE CASCADE
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS diagrams (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    vehicle_configuration_id BIGINT UNSIGNED NOT NULL,
    taxonomy_node_id BIGINT UNSIGNED NULL,
    diagram_code_raw VARCHAR(255) NULL,
    diagram_name_raw TEXT NULL,
    diagram_range_raw VARCHAR(255) NULL,
    diagram_from VARCHAR(32) NULL,
    diagram_to VARCHAR(32) NULL,
    metadata_json JSON NOT NULL,
    source_url LONGTEXT NOT NULL,
    natural_key_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin
        GENERATED ALWAYS AS (
            SHA2(CAST(JSON_ARRAY(
                vehicle_configuration_id, diagram_code_raw, diagram_name_raw,
                diagram_range_raw, source_url
            ) AS CHAR), 256)
        ) STORED,
    UNIQUE KEY uq_diagram_natural_key (natural_key_sha256),
    INDEX idx_diagrams_vehicle_taxonomy (vehicle_configuration_id, taxonomy_node_id, id),
    INDEX idx_diagrams_code (diagram_code_raw, id),
    CONSTRAINT fk_diagram_vehicle
        FOREIGN KEY (vehicle_configuration_id) REFERENCES vehicle_configurations(id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_diagram_taxonomy
        FOREIGN KEY (taxonomy_node_id) REFERENCES taxonomy_nodes(id) ON DELETE SET NULL
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS part_numbers (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    part_brand_raw VARCHAR(255) NULL,
    number_raw VARCHAR(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
    number_normalized VARCHAR(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
    name_en_raw TEXT NULL,
    is_assembly_inferred TINYINT(1) NOT NULL DEFAULT 0,
    assembly_inference_reason TEXT NULL,
    source_url LONGTEXT NOT NULL,
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    natural_key_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin
        GENERATED ALWAYS AS (
            SHA2(CAST(JSON_ARRAY(part_brand_raw, number_raw) AS CHAR), 256)
        ) STORED,
    UNIQUE KEY uq_part_number_natural_key (natural_key_sha256),
    INDEX idx_part_numbers_lookup (part_brand_raw, number_raw(191), id),
    INDEX idx_part_numbers_search (number_normalized(191), part_brand_raw, id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS part_occurrences (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    part_number_id BIGINT UNSIGNED NOT NULL,
    diagram_id BIGINT UNSIGNED NOT NULL,
    vehicle_configuration_id BIGINT UNSIGNED NOT NULL,
    callout_raw VARCHAR(255) NULL,
    quantity_raw VARCHAR(255) NULL,
    part_range_raw VARCHAR(255) NULL,
    part_from VARCHAR(32) NULL,
    part_to VARCHAR(32) NULL,
    part_condition_raw TEXT NULL,
    note_raw TEXT NULL,
    row_metadata_json JSON NOT NULL,
    source_url LONGTEXT NOT NULL,
    natural_key_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin
        GENERATED ALWAYS AS (
            SHA2(CAST(JSON_ARRAY(
                part_number_id, diagram_id, callout_raw, quantity_raw, part_range_raw,
                part_condition_raw, note_raw, source_url
            ) AS CHAR), 256)
        ) STORED,
    UNIQUE KEY uq_occurrence_natural_key (natural_key_sha256),
    INDEX idx_occurrences_part (part_number_id, id),
    INDEX idx_occurrences_diagram (diagram_id, id),
    INDEX idx_occurrences_vehicle_diagram (vehicle_configuration_id, diagram_id, id),
    CONSTRAINT fk_occurrence_part
        FOREIGN KEY (part_number_id) REFERENCES part_numbers(id) ON DELETE RESTRICT,
    CONSTRAINT fk_occurrence_diagram
        FOREIGN KEY (diagram_id) REFERENCES diagrams(id) ON DELETE RESTRICT,
    CONSTRAINT fk_occurrence_vehicle
        FOREIGN KEY (vehicle_configuration_id) REFERENCES vehicle_configurations(id)
        ON DELETE CASCADE
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS fitments (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    part_occurrence_id BIGINT UNSIGNED NOT NULL,
    part_number_id BIGINT UNSIGNED NOT NULL,
    vehicle_configuration_id BIGINT UNSIGNED NOT NULL,
    diagram_id BIGINT UNSIGNED NOT NULL,
    is_verified TINYINT(1) NOT NULL,
    derivation VARCHAR(191) NOT NULL,
    confidence DOUBLE NOT NULL,
    effective_from VARCHAR(32) NULL,
    effective_to VARCHAR(32) NULL,
    source_url LONGTEXT NOT NULL,
    natural_key_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin
        GENERATED ALWAYS AS (
            SHA2(CAST(JSON_ARRAY(part_occurrence_id, derivation) AS CHAR), 256)
        ) STORED,
    UNIQUE KEY uq_fitment_natural_key (natural_key_sha256),
    INDEX idx_fitments_part_vehicle (part_number_id, vehicle_configuration_id, id),
    INDEX idx_fitments_vehicle_part (vehicle_configuration_id, part_number_id, id),
    INDEX idx_fitments_diagram (diagram_id, id),
    CONSTRAINT fk_fitment_occurrence
        FOREIGN KEY (part_occurrence_id) REFERENCES part_occurrences(id) ON DELETE RESTRICT,
    CONSTRAINT fk_fitment_part
        FOREIGN KEY (part_number_id) REFERENCES part_numbers(id) ON DELETE CASCADE,
    CONSTRAINT fk_fitment_vehicle
        FOREIGN KEY (vehicle_configuration_id) REFERENCES vehicle_configurations(id)
        ON DELETE CASCADE,
    CONSTRAINT fk_fitment_diagram
        FOREIGN KEY (diagram_id) REFERENCES diagrams(id) ON DELETE CASCADE
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS compatibility_hints (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    part_number_id BIGINT UNSIGNED NOT NULL,
    brand_text VARCHAR(255) NULL,
    model_text TEXT NULL,
    compatibility_text LONGTEXT NOT NULL,
    source_url LONGTEXT NOT NULL,
    observed_at DATETIME(6) NOT NULL,
    natural_key_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin
        GENERATED ALWAYS AS (
            SHA2(CAST(JSON_ARRAY(part_number_id, compatibility_text, source_url) AS CHAR), 256)
        ) STORED,
    UNIQUE KEY uq_compatibility_hint_natural_key (natural_key_sha256),
    INDEX idx_compatibility_hint_part (part_number_id, id),
    CONSTRAINT fk_compatibility_hint_part
        FOREIGN KEY (part_number_id) REFERENCES part_numbers(id) ON DELETE RESTRICT
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS part_relations (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    from_part_number_id BIGINT UNSIGNED NOT NULL,
    to_part_number_raw VARCHAR(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
    to_part_number_normalized VARCHAR(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
    relation_type VARCHAR(128) NOT NULL,
    relation_text TEXT NULL,
    confidence DOUBLE NOT NULL,
    source_url LONGTEXT NOT NULL,
    observed_at DATETIME(6) NOT NULL,
    natural_key_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin
        GENERATED ALWAYS AS (
            SHA2(CAST(JSON_ARRAY(
                from_part_number_id, to_part_number_raw, relation_type, source_url
            ) AS CHAR), 256)
        ) STORED,
    UNIQUE KEY uq_part_relation_natural_key (natural_key_sha256),
    INDEX idx_part_relations_from (from_part_number_id, relation_type, id),
    CONSTRAINT fk_part_relation_from
        FOREIGN KEY (from_part_number_id) REFERENCES part_numbers(id) ON DELETE RESTRICT
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS parse_failures (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    response_id BIGINT UNSIGNED NOT NULL,
    parser_name VARCHAR(128) NOT NULL,
    page_type VARCHAR(64) NOT NULL,
    error_type VARCHAR(128) NOT NULL,
    error_message LONGTEXT NOT NULL,
    selector_context_json JSON NOT NULL,
    created_at DATETIME(6) NOT NULL,
    natural_key_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin
        GENERATED ALWAYS AS (
            SHA2(CAST(JSON_ARRAY(response_id, parser_name, error_type, error_message) AS CHAR), 256)
        ) STORED,
    UNIQUE KEY uq_parse_failure_natural_key (natural_key_sha256),
    INDEX idx_parse_failures_response (response_id, id),
    CONSTRAINT fk_parse_failure_response
        FOREIGN KEY (response_id) REFERENCES http_responses(id) ON DELETE RESTRICT
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS robots_snapshots (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    run_id BIGINT UNSIGNED NOT NULL,
    response_id BIGINT UNSIGNED NOT NULL,
    user_agent VARCHAR(512) NOT NULL,
    body_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    fetched_at DATETIME(6) NOT NULL,
    UNIQUE KEY uq_robots_snapshot (run_id, response_id, user_agent),
    CONSTRAINT fk_robots_snapshot_run
        FOREIGN KEY (run_id) REFERENCES crawl_runs(id) ON DELETE CASCADE,
    CONSTRAINT fk_robots_snapshot_response
        FOREIGN KEY (response_id) REFERENCES http_responses(id) ON DELETE CASCADE
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS archive_import_manifests (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    run_id BIGINT UNSIGNED NOT NULL,
    archive_source VARCHAR(64) NOT NULL,
    manifest_key CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    metadata_json JSON NOT NULL,
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    UNIQUE KEY uq_archive_manifest (run_id, archive_source, manifest_key),
    CONSTRAINT fk_archive_manifest_run
        FOREIGN KEY (run_id) REFERENCES crawl_runs(id) ON DELETE CASCADE
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS archive_import_items (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    manifest_id BIGINT UNSIGNED NOT NULL,
    capture_key CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    source_url LONGTEXT NOT NULL,
    collection_name VARCHAR(191) NOT NULL DEFAULT '',
    warc_filename TEXT NOT NULL,
    warc_offset BIGINT UNSIGNED NOT NULL DEFAULT 0,
    warc_length BIGINT UNSIGNED NOT NULL DEFAULT 0,
    index_timestamp VARCHAR(64) NOT NULL DEFAULT '',
    index_digest VARCHAR(255) NOT NULL DEFAULT '',
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    attempts INT UNSIGNED NOT NULL DEFAULT 0,
    worker_id VARCHAR(191) NULL,
    fencing_token BIGINT UNSIGNED NOT NULL DEFAULT 0,
    lease_expires_at DATETIME(6) NULL,
    response_id BIGINT UNSIGNED NULL,
    last_error TEXT NULL,
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    finished_at DATETIME(6) NULL,
    UNIQUE KEY uq_archive_item (manifest_id, capture_key),
    INDEX idx_archive_items_claim (manifest_id, status, id),
    INDEX idx_archive_items_lease (manifest_id, status, lease_expires_at),
    CONSTRAINT fk_archive_item_manifest
        FOREIGN KEY (manifest_id) REFERENCES archive_import_manifests(id) ON DELETE CASCADE,
    CONSTRAINT fk_archive_item_response
        FOREIGN KEY (response_id) REFERENCES http_responses(id) ON DELETE SET NULL
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS sqlite_imports (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    source_snapshot_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    source_schema_version INT UNSIGNED NULL,
    source_bytes BIGINT UNSIGNED NOT NULL,
    status VARCHAR(32) NOT NULL,
    last_archive_capture_id BIGINT UNSIGNED NOT NULL DEFAULT 0,
    source_counts_json JSON NOT NULL,
    target_counts_json JSON NOT NULL,
    error_message TEXT NULL,
    started_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    ended_at DATETIME(6) NULL,
    UNIQUE KEY uq_sqlite_import_snapshot (source_snapshot_sha256)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS sqlite_import_items (
    import_id BIGINT UNSIGNED NOT NULL,
    source_capture_id BIGINT UNSIGNED NOT NULL,
    source_response_id BIGINT UNSIGNED NOT NULL,
    body_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    target_response_id BIGINT UNSIGNED NULL,
    status VARCHAR(32) NOT NULL,
    error_message TEXT NULL,
    updated_at DATETIME(6) NOT NULL,
    PRIMARY KEY (import_id, source_capture_id),
    CONSTRAINT fk_sqlite_import_item_import
        FOREIGN KEY (import_id) REFERENCES sqlite_imports(id) ON DELETE CASCADE,
    CONSTRAINT fk_sqlite_import_item_response
        FOREIGN KEY (target_response_id) REFERENCES http_responses(id) ON DELETE SET NULL
) ENGINE=InnoDB;

INSERT IGNORE INTO schema_migrations(version, applied_at)
VALUES (1, UTC_TIMESTAMP(6));
