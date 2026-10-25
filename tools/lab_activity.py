"""Simulator aktivitas untuk LAB. Jangan dijalankan di domain sungguhan.

Mode:
  python lab_activity.py              -> aktivitas normal terus-menerus (login karyawan di jam kerja)
  python lab_activity.py --scenario   -> sekali jalan: rangkaian insiden untuk menguji deteksi

Skenario insiden (semuanya terdeteksi portal dalam satu siklus worker):
  1. Brute force ke satu akun (6 password salah -> terkunci)
  2. Password spraying: 1 password ditebak ke 6 akun berbeda
  3. Mantan karyawan mencoba login
  4. Admin menambahkan user ke group privileged langsung di AD (di luar portal)
  5. Admin menambahkan user ke Domain Admins (Tier-0)
"""
import os
import random
import ssl
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import ldap3
import pymysql

HOST = os.environ.get("AD_HOST", "dc1.corp.lab")
REALM = os.environ.get("AD_REALM", "CORP.LAB").lower()
BASE_DN = ",".join(f"DC={p}" for p in REALM.split("."))
CA = os.environ.get("AD_CA_FILE", "/tls/ca.crt")
ADMIN_PW = os.environ["AD_ADMIN_PASSWORD"]
USER_PW = os.environ.get("IGA_INITIAL_PASSWORD", "Welc0me!Lab-2026")
MYSQL = dict(host=os.environ.get("MYSQL_HOST", "mysql"), port=int(os.environ.get("MYSQL_PORT", "3306")),
             database=os.environ.get("MYSQL_DATABASE", "iga"), user=os.environ.get("MYSQL_USER", "iga_reader"),
             password=os.environ.get("MYSQL_PASSWORD", ""))
TZ = ZoneInfo(os.environ.get("IGA_TIMEZONE", "Asia/Jakarta"))
INTERVAL = int(os.environ.get("ACTIVITY_INTERVAL_SECONDS", "45"))
R = random.Random()


def log(msg):
    print(datetime.now(TZ).strftime("%H:%M:%S"), msg, flush=True)


def server():
    return ldap3.Server(HOST, port=636, use_ssl=True,
                        tls=ldap3.Tls(validate=ssl.CERT_REQUIRED, ca_certs_file=CA), connect_timeout=5)


def bind(sam, pw):
    try:
        c = ldap3.Connection(server(), user=f"{sam}@{REALM}", password=pw, receive_timeout=10)
        ok = c.bind()
        c.unbind()
        return ok
    except ldap3.core.exceptions.LDAPException:
        return False


def people(status="active", position=None):
    """Daftar akun karyawan dari database portal (akun hanya-baca)."""
    sql = "SELECT sam FROM employees WHERE status = %s AND sam IS NOT NULL"
    args = [status]
    if position:
        sql += " AND position = %s"
        args.append(position)
    try:
        con = pymysql.connect(**MYSQL, connect_timeout=5)
    except pymysql.err.OperationalError:
        return []
    try:
        with con.cursor() as cur:
            cur.execute(sql, args)
            return [r[0] for r in cur.fetchall()]
    except pymysql.err.ProgrammingError:   # tabel belum dibuat (portal belum pernah jalan)
        return []
    finally:
        con.close()


def admin():
    return ldap3.Connection(server(), user=f"Administrator@{REALM}", password=ADMIN_PW, auto_bind=True)


def dn_of(c, sam):
    c.search(BASE_DN, f"(sAMAccountName={sam})", attributes=["distinguishedName"])
    return c.entries[0].entry_dn if c.entries else None


def scenario():
    staff = [s for s in people() if not s.startswith(("sari.", "hana.", "fajar.", "joko."))]
    if len(staff) < 8:
        log("Belum ada cukup karyawan di portal; jalankan lagi setelah seeding selesai.")
        return
    R.shuffle(staff)

    victim = staff[0]
    log(f"[1] brute force ke {victim}")
    for i in range(6):
        bind(victim, f"Summer2026!{i}")

    log("[2] password spraying ke 6 akun dengan satu password")
    for s in staff[1:7]:
        bind(s, "Jakarta2026!")

    left = people("terminated")
    if left:
        log(f"[3] mantan karyawan {left[0]} mencoba login")
        bind(left[0], USER_PW)

    c = admin()
    target = staff[7]
    udn = dn_of(c, target)
    log(f"[4] admin menambahkan {target} ke Server-Admins langsung di AD")
    c.modify(f"CN=Server-Admins,OU=Groups,OU=Corp,{BASE_DN}", {"member": [(ldap3.MODIFY_ADD, [udn])]})

    t0 = staff[8] if len(staff) > 8 else staff[1]
    log(f"[5] admin menambahkan {t0} ke Domain Admins")
    c.modify(f"CN=Domain Admins,CN=Users,{BASE_DN}", {"member": [(ldap3.MODIFY_ADD, [dn_of(c, t0)])]})
    c.unbind()
    log("Skenario selesai. Alert muncul di halaman Security dalam 30 detik.")


def baseline():
    log(f"Aktivitas normal berjalan setiap {INTERVAL} detik (jam kerja 07:00-21:00)")
    while True:
        staff = people()
        hour = datetime.now(TZ).hour
        if staff:
            n = R.randint(3, 8) if 7 <= hour < 21 else (1 if R.random() < 0.15 else 0)
            for s in R.sample(staff, min(n, len(staff))):
                if R.random() < 0.04:
                    bind(s, USER_PW + "x")  # salah ketik sesekali
                bind(s, USER_PW)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    for _ in range(60):
        try:
            admin().unbind()
            break
        except Exception:
            time.sleep(5)
    if "--scenario" in sys.argv:
        scenario()
    else:
        baseline()
