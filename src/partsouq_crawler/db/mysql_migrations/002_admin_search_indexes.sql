ALTER TABLE vehicle_configurations
    ADD INDEX idx_vehicle_brand_model_prefix (catalog_brand, model_raw(191), id);

ALTER TABLE vehicle_configurations
    ADD INDEX idx_vehicle_name_prefix (name_raw(191), id);

ALTER TABLE diagrams
    ADD INDEX idx_diagrams_name_prefix (diagram_name_raw(191), id);

ALTER TABLE part_numbers
    ADD INDEX idx_part_names_prefix (name_en_raw(191), id);

ALTER TABLE part_occurrences
    ADD INDEX idx_occurrences_callout (callout_raw, id);

ALTER TABLE fitments
    ADD INDEX idx_fitments_verified_confidence (is_verified, confidence, id);

ALTER TABLE fitments
    ADD INDEX idx_fitments_derivation (derivation, id);
