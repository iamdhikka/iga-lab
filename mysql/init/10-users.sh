#!/bin/bash
# Dijalankan sekali oleh image resmi MySQL saat volume data masih kosong.
# Dua akun dengan hak minimum:
#   iga_app    : dipakai portal. Baca/tulis data + membuat tabel di database iga. Tidak bisa DROP, GRANT, atau akses DB lain.
#   iga_reader : hanya SELECT. Untuk MySQL Workbench, Grafana, atau auditor.

: "${MYSQL_DATABASE:?}" "${MYSQL_APP_PASSWORD:?}" "${MYSQL_READER_PASSWORD:?}"

mysql_note "Membuat akun iga_app dan iga_reader untuk database ${MYSQL_DATABASE}"
docker_process_sql --database=mysql <<-EOSQL
	CREATE USER IF NOT EXISTS 'iga_app'@'%' IDENTIFIED BY '${MYSQL_APP_PASSWORD}';
	GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX, REFERENCES ON \`${MYSQL_DATABASE}\`.* TO 'iga_app'@'%';

	CREATE USER IF NOT EXISTS 'iga_reader'@'%' IDENTIFIED BY '${MYSQL_READER_PASSWORD}';
	GRANT SELECT ON \`${MYSQL_DATABASE}\`.* TO 'iga_reader'@'%';
	GRANT SHOW VIEW ON \`${MYSQL_DATABASE}\`.* TO 'iga_reader'@'%';
	FLUSH PRIVILEGES;
EOSQL
