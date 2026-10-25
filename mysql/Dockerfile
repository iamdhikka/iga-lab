# MySQL 8.4 LTS untuk portal IGA.
# File konfigurasi disalin ke image (bukan bind mount) supaya izin file dan akhir baris tetap benar
# walau repo di-checkout di Windows: MySQL mengabaikan .cnf yang world-writable.
FROM mysql:8.4
COPY conf.d/iga.cnf /etc/mysql/conf.d/iga.cnf
COPY init/ /docker-entrypoint-initdb.d/
RUN sed -i 's/\r$//' /etc/mysql/conf.d/iga.cnf /docker-entrypoint-initdb.d/*.sh \
 && chmod 644 /etc/mysql/conf.d/iga.cnf /docker-entrypoint-initdb.d/*
