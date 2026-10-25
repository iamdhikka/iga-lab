"""Data demo untuk lab: organisasi fiktif ±80 orang, dibuat lewat mesin JML yang sama (akun AD-nya nyata),
lalu tanggalnya dimundurkan agar timeline akses punya riwayat 90 hari.
Semua nama dan kejadian fiktif. Hanya dijalankan jika database masih kosong dan IGA_SEED_DEMO=true."""
import random
from datetime import date, timedelta

from . import certification as cert, db, jml, requests_flow as rf

R = random.Random(7)
FIRST = ["Andi", "Budi", "Citra", "Dewi", "Eka", "Fitri", "Gita", "Hendra", "Indah", "Irfan", "Kartika", "Lina",
         "Made", "Nadia", "Oki", "Putri", "Rizky", "Rudi", "Sinta", "Taufik", "Utami", "Wahyu", "Yogi", "Ayu",
         "Bayu", "Dimas", "Galih", "Intan", "Lestari", "Maya", "Novi", "Reza", "Siti", "Tono", "Yuni", "Arif"]
LAST = ["Pratama", "Saputra", "Wijaya", "Santoso", "Kusuma", "Hidayat", "Nugroho", "Siregar", "Wibowo",
        "Setiawan", "Gunawan", "Rahman", "Simanjuntak", "Purnomo", "Halim", "Utomo", "Lubis", "Harahap", "Tanjung"]
ACTOR = "hana.wijaya"


def _d(days_ago):
    return (date.fromisoformat(db.today()) - timedelta(days=days_ago)).isoformat()


def _ts(days_ago, hour=10):
    return f"{_d(days_ago)}T{hour:02d}:{R.randint(0, 59):02d}:00+07:00"


def _hire(first, last, position, manager=None):
    from .config import POSITIONS
    return jml.create_employee(first, last, POSITIONS[position]["department"], position, manager, db.today(), ACTOR)


def _backdate(emp_id, days_ago):
    db.run("UPDATE employees SET start_date = ?, created_at = ? WHERE id = ?", (_d(days_ago), _ts(days_ago), emp_id))
    db.run("UPDATE entitlements SET granted_at = ? WHERE employee_id = ? AND source = 'birthright'",
           (_ts(days_ago), emp_id))
    db.run("UPDATE hr_events SET effective_date = ?, created_at = ?, processed_at = ? WHERE employee_id = ? "
           "AND kind = 'joiner'", (_d(days_ago), _ts(days_ago + 3), _ts(days_ago, 7), emp_id))


def _user(emp_id, roles=()):
    e = jml.employee(emp_id)
    return {"employee_id": e["id"], "sam": e["sam"], "roles": list(roles)}


def _past_request(emp_id, group, days_ago_start, days, justification):
    """Permintaan yang sudah selesai (riwayat): hanya tercatat di IGA, tidak mengubah AD saat ini."""
    start, end = _d(days_ago_start), _d(days_ago_start - days + 1)
    e = jml.employee(emp_id)
    rid = db.run("INSERT INTO access_requests (kind, target, beneficiary_id, requested_by, justification, start_date,"
                 " end_date, privileged, status, manager_id, manager_decision, manager_at, created_at, closed_at)"
                 " VALUES ('group',?,?,?,?,?,?,0,'expired',?,'approved',?,?,?)",
                 (group, emp_id, emp_id, justification, start, end, e["manager_id"], _ts(days_ago_start + 1, 14),
                  _ts(days_ago_start + 1, 9), _ts(days_ago_start - days, 0)))
    db.run("INSERT INTO entitlements (employee_id, group_name, source, request_id, granted_at, expires_at, revoked_at,"
           " revoked_reason) VALUES (?,?,'request',?,?,?,?,'expired')",
           (emp_id, group, rid, _ts(days_ago_start, 8), end, _ts(days_ago_start - days, 0)))
    return rid


def _move(emp_id, position, manager_id, days_ago):
    old = jml.employee(emp_id)["position"]
    jml.schedule(emp_id, "mover", db.today(), {"position": position, "manager_id": manager_id, "_from": old}, ACTOR)
    db.run("UPDATE entitlements SET revoked_at = ? WHERE employee_id = ? AND revoked_at IS NOT NULL",
           (_ts(days_ago), emp_id))
    db.run("UPDATE entitlements SET granted_at = ? WHERE employee_id = ? AND revoked_at IS NULL AND granted_at > ?",
           (_ts(days_ago), emp_id, _ts(1)))
    db.run("UPDATE hr_events SET effective_date = ?, created_at = ?, processed_at = ? WHERE employee_id = ? "
           "AND kind = 'mover'", (_d(days_ago), _ts(days_ago + 5), _ts(days_ago, 7), emp_id))


def _leave(emp_id, days_ago):
    jml.schedule(emp_id, "leaver", db.today(), {}, ACTOR)
    db.run("UPDATE employees SET end_date = ? WHERE id = ?", (_d(days_ago), emp_id))
    db.run("UPDATE entitlements SET revoked_at = ? WHERE employee_id = ?", (_ts(days_ago, 17), emp_id))
    db.run("UPDATE hr_events SET effective_date = ?, created_at = ?, processed_at = ? WHERE employee_id = ? "
           "AND kind = 'leaver'", (_d(days_ago), _ts(days_ago + 10), _ts(days_ago, 17), emp_id))


REQ_POOL = [  # (group, bobot, alasan)
    ("VPN-Users", 9, ["Kerja dari rumah selama renovasi kantor", "Dinas ke cabang Surabaya dua minggu",
                      "Memantau pengiriman shift malam dari rumah"]),
    ("APP-CRM", 7, ["Membantu tim Sales menyiapkan tender", "Rekonsiliasi data pelanggan untuk audit",
                    "Follow-up keluhan pelanggan korporat"]),
    ("SHARE-Finance", 6, ["Menyiapkan laporan biaya untuk rapat budget", "Mengambil dokumen faktur untuk audit"]),
    ("APP-ERP-User", 6, ["Input PO selama staf purchasing cuti", "Cek status pembayaran vendor"]),
    ("APP-ERP-Approver", 3, ["Approver pembayaran saat penutupan buku", "Menggantikan manager approve PO"]),
    ("Server-Admins", 3, ["Patch keamanan bulanan (change ticket)", "Instal agen backup di server aplikasi"]),
    ("DB-Admins", 2, ["Tuning query lambat di database ERP", "Migrasi database ke server baru"]),
    ("APP-HRIS", 1, ["Membantu input data karyawan baru batch Oktober"]),
    ("SHARE-HR-Confidential", 1, ["Menyiapkan dokumen investigasi internal"]),
]
REJECT_REASONS = ["Tidak ada kaitan dengan tugas saat ini", "Gunakan akun tim, bukan akses pribadi",
                  "Durasi terlalu panjang, ajukan ulang 3 hari saja", "Belum ada change ticket yang disetujui",
                  "Sudah ditangani tim lain"]


def _lat(median_h, slow_share=0.1):
    h = R.lognormvariate(0, 0.9) * median_h
    if R.random() < slow_share:
        h += R.uniform(48, 90)
    return max(h, 0.1)


def _history(staff_ids):
    """±140 tiket yang sudah selesai dalam 60 hari terakhir. Hanya tercatat di portal; semuanya sudah berakhir,
    sehingga konsisten dengan isi AD saat ini."""
    from datetime import datetime as _datetime
    from .config import GROUPS, TZ
    weights = [w for _, w, _ in REQ_POOL]
    people = [jml.employee(e) for e in staff_ids]
    people = [p for p in people if p["status"] == "active" and p["manager_id"]]
    n = 0
    for days_ago in range(60, 1, -1):
        day = date.fromisoformat(_d(days_ago))
        per_day = R.randint(0, 2) if day.weekday() >= 5 else R.randint(1, 4)
        for _ in range(per_day):
            group, _, reasons = R.choices(REQ_POOL, weights=weights)[0]
            ben = R.choice(people)
            if any(e["group_name"] == group for e in jml.active_entitlements(ben["id"])):
                continue
            created = _datetime(day.year, day.month, day.day, R.randint(8, 16), R.randint(0, 59), tzinfo=TZ)
            priv = bool(GROUPS[group].get("privileged"))
            roll = R.random()
            m_at = created + timedelta(hours=_lat(5))
            s_at = m_at + timedelta(hours=_lat(2.5, 0.05)) if priv else None
            status, mdec, sdec, mcom, scom, closed = "expired", "approved", None, "", "", None
            if roll < 0.05:  # dibatalkan peminta sebelum diputuskan
                status, mdec, m_at, s_at = "cancelled", None, None, None
                closed = created + timedelta(hours=R.uniform(1, 30))
            elif roll < 0.17:
                status, closed = "rejected", (s_at if priv and R.random() < 0.5 else m_at)
                if priv and closed == s_at:
                    sdec, scom = "rejected", R.choice(REJECT_REASONS)
                else:
                    mdec, mcom, s_at = "rejected", R.choice(REJECT_REASONS), None
            elif priv:
                sdec = "approved"
            final = s_at or m_at
            start = (final or created).date()
            dur = min(GROUPS[group].get("max_days", 30), R.choice([1, 2, 3, 5, 7, 14, 30]))
            end = min(start + timedelta(days=dur - 1), date.fromisoformat(_d(1)))
            if status == "expired":
                if end < start:
                    continue
                closed = _datetime(end.year, end.month, end.day, 23, 59, tzinfo=TZ) + timedelta(minutes=6)
                if R.random() < 0.04:
                    status = "revoked"
            rid = db.run(
                "INSERT INTO access_requests (kind, target, beneficiary_id, requested_by, justification, start_date,"
                " end_date, privileged, status, manager_id, manager_decision, manager_at, manager_comment, security_by,"
                " security_decision, security_at, security_comment, created_at, closed_at)"
                " VALUES ('group',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (group, ben["id"], ben["id"], R.choice(reasons), start.isoformat(), end.isoformat(), int(priv), status,
                 ben["manager_id"], mdec, m_at.isoformat() if m_at else None, mcom or None,
                 _SARI[0] if sdec else None, sdec, s_at.isoformat() if s_at else None, scom or None,
                 created.isoformat(), closed.isoformat() if closed else None))
            if status in ("expired", "revoked"):
                db.run("INSERT INTO entitlements (employee_id, group_name, source, request_id, granted_at, expires_at,"
                       " revoked_at, revoked_reason) VALUES (?,?,'request',?,?,?,?,?)",
                       (ben["id"], group, rid, final.isoformat(), end.isoformat(), closed.isoformat(),
                        "expired" if status == "expired" else "removed early by manager"))
            n += 1
    return n


_SARI = []


def _aged_pending(emp_id, group, hours_ago, reason, manager_approved=False):
    rid = rf.create("group", group, emp_id, emp_id, reason, db.today(), _d(-R.randint(2, 5)))
    created = (db.now() - timedelta(hours=hours_ago)).isoformat()
    db.run("UPDATE access_requests SET created_at = ? WHERE id = ?", (created, rid))
    if manager_approved:
        e = jml.employee(emp_id)
        rf.decide(rid, _user(e["manager_id"]), True, "OK, lanjut ke IT Security")
        db.run("UPDATE access_requests SET manager_at = ? WHERE id = ?",
               ((db.now() - timedelta(hours=hours_ago * 0.6)).isoformat(), rid))
    return rid


def _alert_history():
    import json as _json
    kinds = [("off_hours", "low", 9), ("brute_force", "high", 4), ("disabled_login", "high", 2),
             ("out_of_band_grant", "high", 2), ("password_spray", "critical", 1)]
    people = [e["sam"] for e in db.q("SELECT sam FROM employees WHERE status = 'active'")]
    for kind, sev, count in kinds:
        for i in range(count):
            ts = db.now() - timedelta(days=R.randint(1, 13), hours=R.randint(0, 20))
            subject = "10.20.4.17" if kind == "password_spray" else R.choice(people)
            res = ts + timedelta(hours=_lat(3, 0.1))
            db.run("INSERT INTO alerts (ts, kind, severity, subject, details, dedup_key, status, resolved_by,"
                   " resolved_at, resolution) VALUES (?,?,?,?,?,?,'resolved','sari.hakim',?,?)",
                   (ts.isoformat(), kind, sev, subject, _json.dumps({"note": "Riwayat demo"}),
                    f"hist:{kind}:{i}", res.isoformat(), "close: dicek, tidak ada tindakan lanjutan"))


def run():
    if db.one("SELECT 1 FROM employees LIMIT 1"):
        return False
    print("[seed] Membuat organisasi demo...", flush=True)

    # Pimpinan departemen
    hana = _hire("Hana", "Wijaya", "hr-manager")
    agus = _hire("Agus", "Wibowo", "it-manager")
    fajar = _hire("Fajar", "Nugroho", "fin-manager")
    wulan = _hire("Wulan", "Siregar", "ops-manager")
    rina = _hire("Rina", "Lestari", "sales-manager")
    sari = _hire("Sari", "Hakim", "it-security", agus)
    joko = _hire("Joko", "Pratama", "fin-staff", fajar)
    dewi = _hire("Dewi", "Kusuma", "ops-supervisor", wulan)
    for e, days in [(hana, 1900), (agus, 1500), (fajar, 1200), (wulan, 1700), (rina, 900), (sari, 700),
                    (joko, 600), (dewi, 1100)]:
        _backdate(e, days)

    plan = [("hr-staff", hana, 3), ("it-engineer", agus, 6), ("it-dba", agus, 2), ("fin-staff", fajar, 9),
            ("ops-staff", dewi, 14), ("ops-supervisor", wulan, 2), ("ops-staff", wulan, 10),
            ("sales-exec", rina, 16)]
    used = {("Hana", "Wijaya"), ("Agus", "Wibowo"), ("Fajar", "Nugroho"), ("Wulan", "Siregar"),
            ("Rina", "Lestari"), ("Sari", "Hakim"), ("Joko", "Pratama"), ("Dewi", "Kusuma")}
    staff = []
    for position, mgr, n in plan:
        for _ in range(n):
            while True:
                name = (R.choice(FIRST), R.choice(LAST))
                if name not in used:
                    used.add(name)
                    break
            e = _hire(name[0], name[1], position, mgr)
            _backdate(e, R.randint(120, 1600))
            staff.append(e)
    print(f"[seed] {len(staff) + 8} karyawan dibuat", flush=True)

    # Riwayat JML 12 minggu terakhir, lewat mesin JML (AD-nya ikut berubah), lalu tanggalnya dimundurkan
    by_pos = lambda pos: [e for e in staff if jml.employee(e)["position"] == pos]  # noqa: E731
    for e in R.sample(staff[1:], 9):
        _backdate(e, R.randint(4, 80))                                   # joiner baru-baru ini
    mover = by_pos("sales-exec")[0]
    _move(mover, "fin-staff", fajar, 40)
    for e, pos, mgr, days in [(by_pos("ops-staff")[3], "ops-supervisor", wulan, 66),
                              (by_pos("ops-staff")[5], "ops-supervisor", wulan, 23),
                              (by_pos("it-engineer")[2], "it-dba", agus, 51),
                              (by_pos("sales-exec")[4], "sales-manager", None, 12)]:
        _move(e, pos, mgr, days)
    for e, days in [(by_pos("ops-staff")[-1], 7), (by_pos("ops-staff")[-2], 33), (by_pos("sales-exec")[-1], 58),
                    (by_pos("fin-staff")[-1], 19), (by_pos("it-engineer")[-1], 74)]:
        _leave(e, days)

    # Riwayat akses sementara yang sudah berakhir (bar abu-abu di timeline)
    _past_request(joko, "APP-CRM", 70, 21, "Membantu rekonsiliasi piutang pelanggan kuartal lalu")
    _past_request(dewi, "SHARE-Finance", 55, 14, "Menyiapkan laporan biaya operasional untuk audit")

    _SARI[:] = [sari]
    n_hist = _history(staff + [joko, dewi])
    print(f"[seed] {n_hist} tiket historis", flush=True)

    # Akses sementara yang sedang aktif
    sec = _user(sari, ["security"])
    r = rf.create("group", "APP-CRM", joko, joko, "Mendukung tim Sales menyiapkan penawaran tender bulan ini",
                  db.today(), _d(-12))
    rf.decide(r, _user(fajar), True, "OK sampai tender selesai")
    r = rf.create("acting", "ops-manager", dewi, wulan,
                  "Menggantikan saya selama cuti tahunan; perlu approve PO operasional", db.today(), _d(-20))
    rf.decide(r, sec, True, "Sesuai jadwal cuti di HR")
    eng = [e for e in staff if jml.employee(e)["position"] == "it-engineer"][0]
    r = rf.create("group", "Server-Admins", eng, eng, "Patch keamanan bulanan di server ERP (change CHG-2041)",
                  db.today(), _d(-2))
    rf.decide(r, _user(agus), True, "Sesuai change ticket")
    rf.decide(r, sec, True, "Dibatasi 3 hari, sesuai jendela maintenance")

    # Permintaan yang menunggu keputusan (isi inbox persona)
    fin = [e for e in staff if jml.employee(e)["position"] == "fin-staff"]
    rf.create("group", "APP-ERP-Approver", fin[0], fin[0],
              "Menjadi approver pembayaran vendor selama penutupan buku akhir bulan", db.today(), _d(-6))
    rf.create("group", "VPN-Users", [e for e in staff if jml.employee(e)["position"] == "ops-staff"][0], dewi,
              "Perlu akses dari rumah untuk memantau pengiriman shift malam", db.today(), _d(-29))
    r = rf.create("group", "DB-Admins", eng, eng, "Migrasi database CRM ke server baru akhir pekan ini",
                  _d(-1), _d(-3))
    rf.decide(r, _user(agus), True, "Disetujui, perlu cek IT Security")

    # Tiket yang sudah lama menunggu (untuk SLA di dashboard)
    fin_active = [e for e in fin[1:] if jml.employee(e)["status"] == "active"]
    for args, kw in [((fin_active[0], "APP-ERP-Approver", 62, "Approver pembayaran vendor selama manager dinas luar"), {}),
                     ((fin_active[1], "APP-CRM", 30, "Cek riwayat transaksi pelanggan yang komplain tagihan ganda"), {}),
                     ((by_pos("ops-staff")[1], "VPN-Users", 9, "Kerja dari rumah selama anak sakit, sekitar tiga hari"), {}),
                     ((by_pos("it-engineer")[1], "Server-Admins", 40,
                       "Pasang sertifikat TLS baru di server aplikasi HR (CHG-2077)"), {"manager_approved": True})]:
        try:
            _aged_pending(*args, **kw)
        except rf.RequestError as exc:
            print(f"[seed] lewati tiket contoh: {exc}", flush=True)
    _alert_history()

    # Kampanye review yang sedang berjalan, sebagian sudah dikerjakan
    cid = cert.create("Q4 2026 access review", "all", _d(-10), sec)
    for item in cert.my_items(_user(wulan))[:12]:
        cert.decide(item["id"], _user(wulan), "keep", "Masih dibutuhkan")
    for item in cert.my_items(_user(agus))[:6]:
        cert.decide(item["id"], _user(agus), "keep", "Masih dibutuhkan")
    db.run("UPDATE campaigns SET created_at = ? WHERE id = ?", (_ts(4), cid))

    db.audit("system", "demo_seeded", None, {"employees": len(staff) + 8})
    print("[seed] Selesai", flush=True)
    return True
