CREATE TABLE IF NOT EXISTS monthly_sync_runs (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    period_key CHAR(7) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    scheduled_for DATETIME(6) NOT NULL,
    status VARCHAR(32) NOT NULL,
    owner_id VARCHAR(191) NULL,
    fencing_token BIGINT UNSIGNED NOT NULL DEFAULT 0,
    lease_expires_at DATETIME(6) NULL,
    attempts INT UNSIGNED NOT NULL DEFAULT 0,
    max_attempts INT UNSIGNED NOT NULL,
    nhtsa_bulk_status VARCHAR(32) NOT NULL DEFAULT 'pending',
    nhtsa_bulk_run_key VARCHAR(191) NULL,
    nhtsa_api_status VARCHAR(32) NOT NULL DEFAULT 'pending',
    nhtsa_api_run_key VARCHAR(191) NULL,
    partsouq_status VARCHAR(32) NOT NULL DEFAULT 'pending',
    partsouq_run_key VARCHAR(191) NULL,
    config_json JSON NOT NULL,
    summary_json JSON NULL,
    last_error TEXT NULL,
    created_at DATETIME(6) NOT NULL,
    started_at DATETIME(6) NULL,
    heartbeat_at DATETIME(6) NULL,
    updated_at DATETIME(6) NOT NULL,
    ended_at DATETIME(6) NULL,
    UNIQUE KEY uq_monthly_sync_period (period_key),
    INDEX idx_monthly_sync_status_lease (status, lease_expires_at, id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS monthly_sync_events (
    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    monthly_run_id BIGINT UNSIGNED NOT NULL,
    fencing_token BIGINT UNSIGNED NOT NULL,
    source_name VARCHAR(32) NOT NULL,
    level VARCHAR(16) NOT NULL,
    event_type VARCHAR(64) NOT NULL,
    message TEXT NOT NULL,
    details_json JSON NOT NULL,
    occurred_at DATETIME(6) NOT NULL,
    INDEX idx_monthly_sync_events_run (monthly_run_id, id),
    INDEX idx_monthly_sync_events_type (monthly_run_id, source_name, event_type, id),
    CONSTRAINT fk_monthly_sync_event_run
        FOREIGN KEY (monthly_run_id) REFERENCES monthly_sync_runs(id) ON DELETE CASCADE
) ENGINE=InnoDB;
