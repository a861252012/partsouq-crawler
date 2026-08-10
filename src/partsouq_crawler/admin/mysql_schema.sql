CREATE TABLE IF NOT EXISTS admin_override_heads (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    entity_type VARCHAR(64) NOT NULL,
    identity_key VARCHAR(96) NOT NULL,
    source_record_id BIGINT UNSIGNED NULL,
    manual_uuid CHAR(36) NULL,
    payload_json JSON NOT NULL,
    status VARCHAR(16) NOT NULL,
    revision INT UNSIGNED NOT NULL,
    base_sha256 CHAR(64) NOT NULL,
    actor VARCHAR(191) NOT NULL,
    reason TEXT NOT NULL,
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    UNIQUE KEY uq_admin_override_identity (entity_type, identity_key),
    UNIQUE KEY uq_admin_override_source (entity_type, source_record_id),
    UNIQUE KEY uq_admin_override_manual (entity_type, manual_uuid),
    INDEX idx_admin_override_list (entity_type, status, source_record_id, id),
    CONSTRAINT chk_admin_override_entity CHECK (
        entity_type IN (
            'vehicle_configurations', 'taxonomy_nodes', 'diagrams', 'part_numbers',
            'part_occurrences', 'fitments', 'part_term_mappings',
            'vin_vehicle_mappings', 'vin_part_fitments', 'reconciliation_cases'
        )
    ),
    CONSTRAINT chk_admin_override_identity CHECK (
        (source_record_id IS NOT NULL AND manual_uuid IS NULL)
        OR (source_record_id IS NULL AND manual_uuid IS NOT NULL)
    ),
    CONSTRAINT chk_admin_override_status CHECK (status IN ('active', 'retired')),
    CONSTRAINT chk_admin_override_revision CHECK (revision >= 1)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS admin_override_events (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    head_id BIGINT UNSIGNED NOT NULL,
    entity_type VARCHAR(64) NOT NULL,
    identity_key VARCHAR(96) NOT NULL,
    source_record_id BIGINT UNSIGNED NULL,
    manual_uuid CHAR(36) NULL,
    action VARCHAR(16) NOT NULL,
    revision INT UNSIGNED NOT NULL,
    base_sha256 CHAR(64) NOT NULL,
    before_json JSON NULL,
    after_json JSON NULL,
    actor VARCHAR(191) NOT NULL,
    reason TEXT NOT NULL,
    created_at DATETIME(6) NOT NULL,
    UNIQUE KEY uq_admin_override_event_revision (head_id, revision),
    INDEX idx_admin_override_event_identity (entity_type, identity_key, revision),
    INDEX idx_admin_override_event_source (entity_type, source_record_id, id),
    CONSTRAINT fk_admin_override_event_head
        FOREIGN KEY (head_id) REFERENCES admin_override_heads(id) ON DELETE RESTRICT,
    CONSTRAINT chk_admin_override_event_action
        CHECK (action IN ('create', 'update', 'retire', 'restore')),
    CONSTRAINT chk_admin_override_event_revision CHECK (revision >= 1)
) ENGINE=InnoDB;
