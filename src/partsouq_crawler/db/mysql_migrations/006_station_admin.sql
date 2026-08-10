CREATE TABLE IF NOT EXISTS part_term_mappings (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    part_number_id BIGINT UNSIGNED NULL,
    name_en_raw TEXT NOT NULL,
    name_en_normalized VARCHAR(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
    name_zh_tw TEXT NULL,
    common_names_zh_tw JSON NOT NULL,
    mapping_status VARCHAR(32) NOT NULL DEFAULT 'missing_translation',
    source_kind VARCHAR(64) NOT NULL,
    confidence DOUBLE NOT NULL DEFAULT 0,
    source_url LONGTEXT NULL,
    observed_at DATETIME(6) NOT NULL,
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    natural_key_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin
        GENERATED ALWAYS AS (
            SHA2(CAST(JSON_ARRAY(part_number_id, name_en_normalized) AS CHAR), 256)
        ) STORED,
    UNIQUE KEY uq_part_term_natural_key (natural_key_sha256),
    INDEX idx_part_term_part (part_number_id, id),
    INDEX idx_part_term_english (name_en_normalized(191), id),
    INDEX idx_part_term_status (mapping_status, id),
    CONSTRAINT fk_part_term_part
        FOREIGN KEY (part_number_id) REFERENCES part_numbers(id) ON DELETE RESTRICT
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS vin_decode_responses (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    batch_key_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    http_status SMALLINT UNSIGNED NOT NULL,
    response_headers_json JSON NOT NULL,
    body_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    body_json LONGBLOB NOT NULL,
    response_bytes BIGINT UNSIGNED NOT NULL,
    fetched_at DATETIME(6) NOT NULL,
    UNIQUE KEY uq_vin_decode_batch (batch_key_sha256),
    INDEX idx_vin_decode_body (body_sha256, id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS vin_vehicle_mappings (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    vin CHAR(17) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    make_name VARCHAR(255) NULL,
    model_name VARCHAR(255) NULL,
    series_name VARCHAR(255) NULL,
    body_class VARCHAR(255) NULL,
    vehicle_type VARCHAR(255) NULL,
    model_year SMALLINT UNSIGNED NULL,
    manufacturer_name VARCHAR(512) NULL,
    partsouq_vehicle_configuration_id BIGINT UNSIGNED NULL,
    decode_status VARCHAR(32) NOT NULL,
    error_code VARCHAR(64) NULL,
    error_text TEXT NULL,
    source_kind VARCHAR(64) NOT NULL,
    response_id BIGINT UNSIGNED NULL,
    decoded_at DATETIME(6) NULL,
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    UNIQUE KEY uq_vin_vehicle_vin (vin),
    INDEX idx_vin_vehicle_lookup (make_name, model_name, model_year, id),
    INDEX idx_vin_vehicle_partsouq (partsouq_vehicle_configuration_id, id),
    INDEX idx_vin_vehicle_status (decode_status, id),
    CONSTRAINT fk_vin_vehicle_response
        FOREIGN KEY (response_id) REFERENCES vin_decode_responses(id) ON DELETE SET NULL,
    CONSTRAINT fk_vin_vehicle_partsouq
        FOREIGN KEY (partsouq_vehicle_configuration_id)
        REFERENCES vehicle_configurations(id) ON DELETE SET NULL
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS vin_decode_requests (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    vin CHAR(17) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    attempts INT UNSIGNED NOT NULL DEFAULT 0,
    worker_id VARCHAR(191) NULL,
    fencing_token BIGINT UNSIGNED NOT NULL DEFAULT 0,
    lease_expires_at DATETIME(6) NULL,
    requested_by VARCHAR(191) NOT NULL,
    mapping_id BIGINT UNSIGNED NULL,
    last_error TEXT NULL,
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    finished_at DATETIME(6) NULL,
    UNIQUE KEY uq_vin_decode_request (vin),
    INDEX idx_vin_decode_request_claim (status, lease_expires_at, id),
    CONSTRAINT fk_vin_decode_request_mapping
        FOREIGN KEY (mapping_id) REFERENCES vin_vehicle_mappings(id) ON DELETE SET NULL
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS vin_part_fitments (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    vin_vehicle_mapping_id BIGINT UNSIGNED NOT NULL,
    part_number_id BIGINT UNSIGNED NOT NULL,
    vehicle_configuration_id BIGINT UNSIGNED NULL,
    is_verified TINYINT(1) NOT NULL DEFAULT 0,
    derivation VARCHAR(191) NOT NULL,
    confidence DOUBLE NOT NULL DEFAULT 0,
    source_url LONGTEXT NULL,
    observed_at DATETIME(6) NOT NULL,
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    natural_key_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin
        GENERATED ALWAYS AS (
            SHA2(CAST(JSON_ARRAY(
                vin_vehicle_mapping_id, part_number_id, vehicle_configuration_id, derivation
            ) AS CHAR), 256)
        ) STORED,
    UNIQUE KEY uq_vin_part_fitment (natural_key_sha256),
    INDEX idx_vin_part_vin (vin_vehicle_mapping_id, id),
    INDEX idx_vin_part_part (part_number_id, id),
    CONSTRAINT fk_vin_part_vin
        FOREIGN KEY (vin_vehicle_mapping_id) REFERENCES vin_vehicle_mappings(id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_vin_part_part
        FOREIGN KEY (part_number_id) REFERENCES part_numbers(id) ON DELETE RESTRICT,
    CONSTRAINT fk_vin_part_vehicle
        FOREIGN KEY (vehicle_configuration_id) REFERENCES vehicle_configurations(id)
        ON DELETE RESTRICT
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS reconciliation_cases (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    case_key_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    case_type VARCHAR(64) NOT NULL,
    subject_type VARCHAR(64) NOT NULL,
    subject_key VARCHAR(191) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
    severity VARCHAR(16) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'open',
    current_json JSON NOT NULL,
    candidate_json JSON NOT NULL,
    evidence_json JSON NOT NULL,
    comments_json JSON NOT NULL,
    assigned_to VARCHAR(191) NULL,
    resolution TEXT NULL,
    source_run_key VARCHAR(191) NULL,
    opened_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    resolved_at DATETIME(6) NULL,
    UNIQUE KEY uq_reconciliation_case (case_key_sha256),
    INDEX idx_reconciliation_work (status, severity, id),
    INDEX idx_reconciliation_subject (subject_type, subject_key, id)
) ENGINE=InnoDB;

ALTER TABLE admin_override_heads DROP CHECK chk_admin_override_entity;

ALTER TABLE admin_override_heads ADD CONSTRAINT chk_admin_override_entity CHECK (
    entity_type IN (
        'vehicle_configurations', 'taxonomy_nodes', 'diagrams', 'part_numbers',
        'part_occurrences', 'fitments', 'part_term_mappings',
        'vin_vehicle_mappings', 'vin_part_fitments', 'reconciliation_cases'
    )
);

ALTER TABLE monthly_sync_runs
    ADD COLUMN station_status VARCHAR(32) NOT NULL DEFAULT 'pending'
        AFTER nhtsa_api_run_key;

ALTER TABLE monthly_sync_runs
    ADD COLUMN station_run_key VARCHAR(191) NULL AFTER station_status;
