ALTER TABLE vin_vehicle_mappings
    ADD COLUMN trim_name VARCHAR(255) NULL AFTER manufacturer_name;

ALTER TABLE vin_vehicle_mappings
    ADD COLUMN engine_configuration VARCHAR(255) NULL AFTER trim_name;

ALTER TABLE vin_vehicle_mappings
    ADD COLUMN engine_cylinders VARCHAR(64) NULL AFTER engine_configuration;

ALTER TABLE vin_vehicle_mappings
    ADD COLUMN displacement_l_raw VARCHAR(64) NULL AFTER engine_cylinders;

ALTER TABLE vin_vehicle_mappings
    ADD COLUMN engine_model VARCHAR(255) NULL AFTER displacement_l_raw;

ALTER TABLE vin_vehicle_mappings
    ADD COLUMN engine_manufacturer VARCHAR(255) NULL AFTER engine_model;

ALTER TABLE vin_vehicle_mappings
    ADD COLUMN fuel_type_primary VARCHAR(255) NULL AFTER engine_manufacturer;

ALTER TABLE vin_vehicle_mappings
    ADD COLUMN drive_type VARCHAR(255) NULL AFTER fuel_type_primary;

ALTER TABLE vin_vehicle_mappings
    ADD COLUMN transmission_style VARCHAR(255) NULL AFTER drive_type;

ALTER TABLE vin_vehicle_mappings
    ADD COLUMN plant_country VARCHAR(255) NULL AFTER transmission_style;
