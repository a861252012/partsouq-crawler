#!/bin/sh
set -eu

partsouq_password="${PARTSOUQ_MYSQL_PASSWORD:-partsouq-local}"
admin_password="${PARTSOUQ_ADMIN_MYSQL_PASSWORD:-partsouq-admin-local}"

mysql -uroot -p"${MYSQL_ROOT_PASSWORD}" <<SQL
CREATE DATABASE IF NOT EXISTS partsouq
    CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
CREATE DATABASE IF NOT EXISTS partsouq_test
    CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
CREATE USER IF NOT EXISTS 'partsouq'@'%' IDENTIFIED BY '${partsouq_password}';
ALTER USER 'partsouq'@'%' IDENTIFIED BY '${partsouq_password}';
GRANT ALL PRIVILEGES ON partsouq.* TO 'partsouq'@'%';
GRANT ALL PRIVILEGES ON partsouq_test.* TO 'partsouq'@'%';
CREATE USER IF NOT EXISTS 'partsouq_admin'@'%' IDENTIFIED BY '${admin_password}';
ALTER USER 'partsouq_admin'@'%' IDENTIFIED BY '${admin_password}';
FLUSH PRIVILEGES;
SQL

mysql -uroot -p"${MYSQL_ROOT_PASSWORD}" partsouq \
    < /opt/partsouq-schema/mysql_schema.sql
mysql -uroot -p"${MYSQL_ROOT_PASSWORD}" partsouq \
    < /opt/partsouq-schema/admin_mysql_schema.sql
mysql -uroot -p"${MYSQL_ROOT_PASSWORD}" partsouq \
    < /opt/partsouq-schema/005_monthly_sync_runs.sql
mysql -uroot -p"${MYSQL_ROOT_PASSWORD}" partsouq \
    < /opt/partsouq-schema/006_station_admin.sql
mysql -uroot -p"${MYSQL_ROOT_PASSWORD}" partsouq \
    < /opt/partsouq-schema/007_vin_vehicle_specs.sql
mysql -uroot -p"${MYSQL_ROOT_PASSWORD}" partsouq_test \
    < /opt/partsouq-schema/mysql_schema.sql
mysql -uroot -p"${MYSQL_ROOT_PASSWORD}" partsouq_test \
    < /opt/partsouq-schema/admin_mysql_schema.sql
mysql -uroot -p"${MYSQL_ROOT_PASSWORD}" partsouq_test \
    < /opt/partsouq-schema/005_monthly_sync_runs.sql
mysql -uroot -p"${MYSQL_ROOT_PASSWORD}" partsouq_test \
    < /opt/partsouq-schema/006_station_admin.sql
mysql -uroot -p"${MYSQL_ROOT_PASSWORD}" partsouq_test \
    < /opt/partsouq-schema/007_vin_vehicle_specs.sql

mysql -uroot -p"${MYSQL_ROOT_PASSWORD}" <<SQL
GRANT SELECT ON partsouq.* TO 'partsouq_admin'@'%';
GRANT INSERT, UPDATE ON partsouq.admin_override_heads TO 'partsouq_admin'@'%';
GRANT INSERT ON partsouq.admin_override_events TO 'partsouq_admin'@'%';
GRANT INSERT, UPDATE ON partsouq.vin_decode_requests TO 'partsouq_admin'@'%';
GRANT SELECT ON partsouq_test.* TO 'partsouq_admin'@'%';
GRANT INSERT, UPDATE ON partsouq_test.admin_override_heads TO 'partsouq_admin'@'%';
GRANT INSERT ON partsouq_test.admin_override_events TO 'partsouq_admin'@'%';
GRANT INSERT, UPDATE ON partsouq_test.vin_decode_requests TO 'partsouq_admin'@'%';
FLUSH PRIVILEGES;
SQL
