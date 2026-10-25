"""Penyimpanan IGA di MySQL 8.

Satu koneksi per operasi (aman untuk web + worker dalam satu proses). Kode modul lain tetap menulis SQL
dengan placeholder `?`; lapisan ini menerjemahkannya ke gaya PyMySQL (`%s`).
Waktu disimpan sebagai string ISO-8601 dengan zona waktu (mis. 2026-10-25T09:30:00+07:00) sehingga
perbandingan dan pengurutan tetap benar, dan nilainya sama persis dengan yang tampil di log audit.
"""
import json
import re
import time
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal

import pymysql
import pymysql.cursors

from .config import MYSQL, TZ

# Kolom waktu: VARCHAR(32) berisi ISO-8601. Teks bebas: TEXT. Kunci dan label pendek: VARCHAR.
SCHEMA = [
    """CREATE TABLE IF NOT EXISTS employees (
    id VARCHAR(16) PRIMARY KEY COMMENT 'nomor karyawan dari HR, mis. E1042',
    first_name VARCHAR(80) NOT NULL,
    last_name VARCHAR(80) NOT NULL,
    sam VARCHAR(64) UNIQUE COMMENT 'sAMAccountName di AD',
    department VARCHAR(64) NOT NULL,
    position VARCHAR(64) NOT NULL COMMENT 'kunci katalog jabatan',
    manager_id VARCHAR(16),
    status VARCHAR(16) NOT NULL COMMENT 'scheduled | active | terminated',
    start_date VARCHAR(10) NOT NULL,
    end_date VARCHAR(10),
    acting_position VARCHAR(64) COMMENT 'jabatan sementara yang sedang berlaku',
    acting_until VARCHAR(10),
    ad_dn VARCHAR(255),
    created_at VARCHAR(32) NOT NULL,
    updated_at VARCHAR(32) NOT NULL,
    KEY ix_emp_manager (manager_id),
    CONSTRAINT fk_emp_manager FOREIGN KEY (manager_id) REFERENCES employees(id)
) COMMENT 'Data karyawan dari HR, sumber kebenaran untuk JML'""",

    """CREATE TABLE IF NOT EXISTS hr_events (
    id INT AUTO_INCREMENT PRIMARY KEY,
    employee_id VARCHAR(16) NOT NULL,
    kind VARCHAR(16) NOT NULL COMMENT 'joiner | mover | leaver',
    effective_date VARCHAR(10) NOT NULL,
    payload TEXT NOT NULL,
    status VARCHAR(16) NOT NULL COMMENT 'scheduled | done | failed',
    error TEXT,
    created_by VARCHAR(64) NOT NULL,
    created_at VARCHAR(32) NOT NULL,
    processed_at VARCHAR(32),
    KEY ix_hr_due (status, effective_date),
    CONSTRAINT fk_hr_emp FOREIGN KEY (employee_id) REFERENCES employees(id)
) COMMENT 'Kejadian joiner / mover / leaver, termasuk yang dijadwalkan'""",

    """CREATE TABLE IF NOT EXISTS access_requests (
    id INT AUTO_INCREMENT PRIMARY KEY,
    kind VARCHAR(16) NOT NULL COMMENT 'group | acting',
    target VARCHAR(64) NOT NULL COMMENT 'nama group atau kunci jabatan',
    beneficiary_id VARCHAR(16) NOT NULL,
    requested_by VARCHAR(16) NOT NULL,
    justification TEXT NOT NULL,
    start_date VARCHAR(10) NOT NULL,
    end_date VARCHAR(10) NOT NULL,
    privileged TINYINT NOT NULL DEFAULT 0,
    status VARCHAR(24) NOT NULL COMMENT 'pending_manager | pending_security | scheduled | active | rejected | cancelled | expired | revoked',
    manager_id VARCHAR(16),
    manager_decision VARCHAR(16), manager_at VARCHAR(32), manager_comment TEXT,
    security_by VARCHAR(16), security_decision VARCHAR(16), security_at VARCHAR(32), security_comment TEXT,
    created_at VARCHAR(32) NOT NULL,
    closed_at VARCHAR(32),
    KEY ix_req_status (status),
    KEY ix_req_beneficiary (beneficiary_id),
    CONSTRAINT fk_req_beneficiary FOREIGN KEY (beneficiary_id) REFERENCES employees(id),
    CONSTRAINT fk_req_requester FOREIGN KEY (requested_by) REFERENCES employees(id)
) COMMENT 'Tiket permintaan akses sementara dan posisi sementara (acting)'""",

    """CREATE TABLE IF NOT EXISTS entitlements (
    id INT AUTO_INCREMENT PRIMARY KEY,
    employee_id VARCHAR(16) NOT NULL,
    group_name VARCHAR(64) NOT NULL,
    source VARCHAR(16) NOT NULL COMMENT 'birthright | request | acting | exception',
    request_id INT,
    granted_at VARCHAR(32) NOT NULL,
    expires_at VARCHAR(32),
    revoked_at VARCHAR(32),
    revoked_reason VARCHAR(500),
    KEY ix_ent_active (employee_id, revoked_at),
    KEY ix_ent_expiry (revoked_at, expires_at),
    CONSTRAINT fk_ent_emp FOREIGN KEY (employee_id) REFERENCES employees(id),
    CONSTRAINT fk_ent_req FOREIGN KEY (request_id) REFERENCES access_requests(id)
) COMMENT 'Hak akses yang disetujui: alasan seseorang boleh berada di sebuah grup AD'""",

    """CREATE TABLE IF NOT EXISTS campaigns (
    id INT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(120) NOT NULL,
    scope VARCHAR(16) NOT NULL COMMENT 'all | privileged',
    created_by VARCHAR(64) NOT NULL,
    created_at VARCHAR(32) NOT NULL,
    due_date VARCHAR(10) NOT NULL,
    status VARCHAR(16) NOT NULL COMMENT 'open | closed',
    closed_at VARCHAR(32)
) COMMENT 'Kampanye access review / sertifikasi akses'""",

    """CREATE TABLE IF NOT EXISTS review_items (
    id INT AUTO_INCREMENT PRIMARY KEY,
    campaign_id INT NOT NULL,
    reviewer_id VARCHAR(16) NOT NULL,
    employee_id VARCHAR(16) NOT NULL,
    entitlement_id INT NOT NULL,
    group_name VARCHAR(64) NOT NULL,
    source VARCHAR(16) NOT NULL,
    decision VARCHAR(16) COMMENT 'keep | revoke',
    comment TEXT,
    decided_at VARCHAR(32),
    KEY ix_review_reviewer (campaign_id, reviewer_id),
    CONSTRAINT fk_ri_campaign FOREIGN KEY (campaign_id) REFERENCES campaigns(id),
    CONSTRAINT fk_ri_reviewer FOREIGN KEY (reviewer_id) REFERENCES employees(id),
    CONSTRAINT fk_ri_emp FOREIGN KEY (employee_id) REFERENCES employees(id),
    CONSTRAINT fk_ri_ent FOREIGN KEY (entitlement_id) REFERENCES entitlements(id)
) COMMENT 'Satu baris = satu akses yang harus dikonfirmasi manager'""",

    """CREATE TABLE IF NOT EXISTS audit_log (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    ts VARCHAR(32) NOT NULL,
    actor VARCHAR(128) NOT NULL,
    action VARCHAR(48) NOT NULL,
    target VARCHAR(255),
    details TEXT,
    result VARCHAR(16) NOT NULL DEFAULT 'success',
    KEY ix_audit_ts (ts),
    KEY ix_audit_target (target)
) COMMENT 'Jejak audit: siapa melakukan apa, kapan'""",

    """CREATE TABLE IF NOT EXISTS auth_events (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    ts VARCHAR(32) NOT NULL,
    account VARCHAR(96) NOT NULL,
    success TINYINT NOT NULL,
    status VARCHAR(64) NOT NULL,
    remote VARCHAR(64),
    service VARCHAR(32),
    KEY ix_auth_ts (ts),
    KEY ix_auth_account (account, ts)
) COMMENT 'Login ke AD, dibaca dari audit log JSON Samba'""",

    """CREATE TABLE IF NOT EXISTS alerts (
    id INT AUTO_INCREMENT PRIMARY KEY,
    ts VARCHAR(32) NOT NULL,
    kind VARCHAR(32) NOT NULL COMMENT 'brute_force | password_spray | disabled_login | off_hours | out_of_band_grant | tier0_change | leaver_enabled',
    severity VARCHAR(16) NOT NULL COMMENT 'critical | high | medium | low',
    subject VARCHAR(255) NOT NULL,
    details TEXT NOT NULL,
    dedup_key VARCHAR(191) UNIQUE,
    status VARCHAR(16) NOT NULL DEFAULT 'open' COMMENT 'open | resolved',
    resolved_by VARCHAR(64), resolved_at VARCHAR(32), resolution VARCHAR(500),
    KEY ix_alert_status (status, severity),
    KEY ix_alert_ts (ts)
) COMMENT 'Alert keamanan hasil deteksi'""",

    """CREATE TABLE IF NOT EXISTS group_events (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    ts VARCHAR(32) NOT NULL,
    action VARCHAR(16) NOT NULL COMMENT 'Added | Removed',
    group_dn VARCHAR(255) NOT NULL,
    member_dn VARCHAR(255) NOT NULL,
    actor_sid VARCHAR(96),
    remote VARCHAR(64),
    KEY ix_group_events (group_dn(120), member_dn(120), ts)
) COMMENT 'Perubahan keanggotaan grup di AD, dari audit log Samba'""",

    """CREATE TABLE IF NOT EXISTS kv (
    k VARCHAR(64) PRIMARY KEY,
    v TEXT NOT NULL
) COMMENT 'Status internal worker (posisi baca log, waktu rekonsiliasi terakhir)'""",
]


def now():
    return datetime.now(TZ).replace(microsecond=0)


def now_iso():
    return now().isoformat()


def today():
    return now().date().isoformat()


def _connect():
    return pymysql.connect(**MYSQL, charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor, autocommit=False,
                           connect_timeout=10, init_command="SET SESSION sql_mode = 'STRICT_ALL_TABLES,"
                           "ONLY_FULL_GROUP_BY,NO_ZERO_DATE,ERROR_FOR_DIVISION_BY_ZERO,NO_ENGINE_SUBSTITUTION'")


_PLACEHOLDER = re.compile(r"\?")


def _sql(sql):
    """Placeholder `?` -> `%s`. Tanda % literal di dalam SQL di-escape agar tidak dibaca PyMySQL."""
    return _PLACEHOLDER.sub("%s", sql.replace("%", "%%"))


def _plain(row):
    # SUM() di MySQL mengembalikan DECIMAL; ubah ke int/float supaya bisa dipakai di template dan JSON.
    return {k: (int(v) if v == v.to_integral_value() else float(v)) if isinstance(v, Decimal) else v
            for k, v in row.items()}


class _Conn:
    """Pembungkus tipis supaya kode lama `c.execute(sql, args)` tetap jalan di dalam satu transaksi."""

    def __init__(self, raw):
        self.raw = raw

    def execute(self, sql, args=()):
        cur = self.raw.cursor()
        cur.execute(_sql(sql), tuple(args))
        return cur


@contextmanager
def tx():
    raw = _connect()
    try:
        yield _Conn(raw)
        raw.commit()
    except Exception:
        raw.rollback()
        raise
    finally:
        raw.close()


def init(wait_seconds=120):
    """Tunggu MySQL siap (container bisa lebih lambat dari portal), lalu buat tabel bila belum ada."""
    deadline = time.time() + wait_seconds
    while True:
        try:
            with tx() as c:
                for ddl in SCHEMA:
                    c.execute(ddl)
            return
        except pymysql.err.OperationalError:
            if time.time() > deadline:
                raise
            time.sleep(2)


def q(sql, args=()):
    with tx() as c:
        return [_plain(r) for r in c.execute(sql, args).fetchall()]


def one(sql, args=()):
    rows = q(sql, args)
    return rows[0] if rows else None


def run(sql, args=()):
    with tx() as c:
        return c.execute(sql, args).lastrowid


def audit(actor, action, target=None, details=None, result="success"):
    if isinstance(details, (dict, list)):
        details = json.dumps(details, ensure_ascii=False)
    run("INSERT INTO audit_log (ts, actor, action, target, details, result) VALUES (?,?,?,?,?,?)",
        (now_iso(), actor, action, target, details, result))


def kv_get(key, default=None):
    row = one("SELECT v FROM kv WHERE k = ?", (key,))
    return json.loads(row["v"]) if row else default


def kv_set(key, value):
    run("INSERT INTO kv (k, v) VALUES (?, ?) AS new ON DUPLICATE KEY UPDATE v = new.v", (key, json.dumps(value)))
