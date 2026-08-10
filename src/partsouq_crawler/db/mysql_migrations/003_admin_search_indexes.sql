ALTER TABLE vehicle_configurations
    ADD INDEX idx_vehicle_model_prefix (model_raw(191), id);

ALTER TABLE part_occurrences
    ADD INDEX idx_occurrences_range (part_range_raw, id);

ALTER TABLE fitments
    ADD INDEX idx_fitments_effective_from (effective_from, id);

ALTER TABLE fitments
    ADD INDEX idx_fitments_effective_to (effective_to, id);
