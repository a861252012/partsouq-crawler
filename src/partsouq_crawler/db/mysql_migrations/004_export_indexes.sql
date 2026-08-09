ALTER TABLE fitments
    ADD INDEX idx_fitments_verified_id (is_verified, id);
