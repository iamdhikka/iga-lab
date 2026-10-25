"""Pemantauan keamanan identitas.

1. Ingest audit log JSON dari domain controller (login dan perubahan group).
2. Deteksi: brute force, password spraying, login ke akun nonaktif/terkunci, login di luar jam kerja.
3. Rekonsiliasi AD vs IGA: akses yang diberikan di luar IGA, akses IGA yang hilang di AD,
   akun leaver yang aktif lagi, dan perubahan group Tier-0."""
import json
import os
from collections import defaultdict
from datetime import datetime, timedelta

from . import ad, db, jml
from .config import AUDIT_DIR, BASE_DN, GROUPS, OFF_HOURS, TIER0, TZ, is_privileged

IGNORED_ACCOUNTS = ("svc-", "krbtgt", "dc1$")
SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


# ------------------------------------------------------------------ util
def _parse_ts(s):
    return datetime.fromisoformat(s).astimezone(TZ).replace(microsecond=0)


def _ip(remote):
    if not remote:
        return None
    parts = remote.split(":")
    return parts[1] if len(parts) >= 3 and parts[0] == "ipv4" else remote


def raise_alert(kind, severity, subject, details, dedup_key=None):
    """Kembalikan id alert baru, atau None jika duplikat."""
    with db.tx() as c:
        cur = c.execute("INSERT IGNORE INTO alerts (ts, kind, severity, subject, details, dedup_key) "
                        "VALUES (?,?,?,?,?,?)", (db.now_iso(), kind, severity, subject,
                                                 json.dumps(details, ensure_ascii=False), dedup_key))
        new_id = cur.lastrowid if cur.rowcount else None
    if new_id:
        db.audit("system", "alert_raised", subject, {"kind": kind, "severity": severity, "alert": new_id})
        from . import notify
        notify.alert(kind, severity, subject, details)
    return new_id


# ------------------------------------------------------------------ ingest
def _read_new_lines(name):
    path = os.path.join(AUDIT_DIR, name)
    if not os.path.exists(path):
        return []
    state = db.kv_get(f"offset:{name}", {"inode": None, "pos": 0})
    st = os.stat(path)
    pos = state["pos"] if state["inode"] == st.st_ino and state["pos"] <= st.st_size else 0
    lines = []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        fh.seek(pos)
        for line in fh:
            if not line.endswith("\n"):
                break  # baris belum lengkap; baca lagi pada siklus berikutnya
            pos += len(line.encode("utf-8"))
            line = line.strip()
            if line.startswith("{"):
                lines.append(line)
    db.kv_set(f"offset:{name}", {"inode": st.st_ino, "pos": pos})
    return lines


def ingest():
    n_auth = n_group = 0
    for line in _read_new_lines("auth.json"):
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        a = ev.get("Authentication")
        if not a:
            continue
        account = (a.get("mappedAccount") or a.get("clientAccount") or "").split("@")[0].lower()
        if not account or account.startswith(IGNORED_ACCOUNTS):
            continue
        db.run("INSERT INTO auth_events (ts, account, success, status, remote, service) VALUES (?,?,?,?,?,?)",
               (_parse_ts(ev["timestamp"]).isoformat(), account, int(a.get("status") == "NT_STATUS_OK"),
                a.get("status", ""), _ip(a.get("remoteAddress")), a.get("serviceDescription")))
        n_auth += 1
    for line in _read_new_lines("group.json"):
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        g = ev.get("groupChange")
        if not g or g.get("status") != "Success":
            continue
        db.run("INSERT INTO group_events (ts, action, group_dn, member_dn, actor_sid, remote) VALUES (?,?,?,?,?,?)",
               (_parse_ts(ev["timestamp"]).isoformat(), g.get("action"), g.get("group", ""), g.get("user", ""),
                g.get("userSid"), _ip(g.get("remoteAddress"))))
        n_group += 1
    return n_auth, n_group


# ------------------------------------------------------------------ deteksi login
def detect(window_minutes=15):
    since = (db.now() - timedelta(minutes=window_minutes)).isoformat()
    bucket = db.now().strftime("%Y-%m-%dT%H")
    failures = db.q("SELECT * FROM auth_events WHERE success = 0 AND ts >= ?", (since,))

    by_account = defaultdict(list)
    by_source = defaultdict(set)
    for f in failures:
        by_account[f["account"]].append(f)
        if f["remote"]:
            by_source[f["remote"]].add(f["account"])

    for account, evs in by_account.items():
        bad_pw = [e for e in evs if e["status"] in ("NT_STATUS_WRONG_PASSWORD", "NT_STATUS_ACCOUNT_LOCKED_OUT")]
        if len(bad_pw) >= 5:
            locked = any(e["status"] == "NT_STATUS_ACCOUNT_LOCKED_OUT" for e in evs)
            raise_alert("brute_force", "high", account,
                        {"failures": len(bad_pw), "window_minutes": window_minutes, "locked_out": locked,
                         "sources": sorted({e["remote"] for e in bad_pw if e["remote"]})},
                        f"brute:{account}:{bucket}")
        for e in evs:
            if e["status"] == "NT_STATUS_ACCOUNT_DISABLED":
                emp = jml.by_sam(account)
                left = emp and emp["status"] == "terminated"
                raise_alert("disabled_login", "high" if left else "medium", account,
                            {"note": "A former employee tried to sign in" if left else "Sign-in attempt to a disabled account",
                             "source": e["remote"], "at": e["ts"]}, f"disabled:{account}:{bucket}")

    for source, accounts in by_source.items():
        if len(accounts) >= 5:
            raise_alert("password_spray", "critical", source,
                        {"distinct_accounts": len(accounts), "accounts": sorted(accounts)[:20],
                         "window_minutes": window_minutes}, f"spray:{source}:{bucket}")

    start_h, end_h = OFF_HOURS
    for s in db.q("SELECT * FROM auth_events WHERE success = 1 AND ts >= ?", (since,)):
        hour = datetime.fromisoformat(s["ts"]).hour
        if (hour >= start_h or hour < end_h) and s["account"] != "administrator":
            raise_alert("off_hours", "low", s["account"], {"at": s["ts"], "source": s["remote"],
                                                           "service": s["service"]},
                        f"offhours:{s['account']}:{s['ts'][:10]}")


# ------------------------------------------------------------------ rekonsiliasi
_sid_cache = {}


def _actor_for(group_dn, member_dn):
    ev = db.one("SELECT * FROM group_events WHERE lower(group_dn) = lower(?) AND lower(member_dn) = lower(?) "
                "AND action = 'Added' ORDER BY ts DESC LIMIT 1", (group_dn, member_dn))
    if not ev:
        return None
    sid = ev["actor_sid"]
    if sid and sid not in _sid_cache:
        c = ad.conn()
        c.search(BASE_DN, f"(objectSid={sid})", attributes=["sAMAccountName"])
        _sid_cache[sid] = str(c.entries[0].sAMAccountName.value) if c.entries else sid
    return {"by": _sid_cache.get(sid, sid), "at": ev["ts"], "from": ev["remote"]}


def reconcile():
    """Bandingkan isi AD dengan catatan IGA. Mengembalikan ringkasan untuk dashboard/metrics."""
    summary = {"out_of_band": 0, "drift_fixed": 0, "leaver_enabled": 0, "tier0_changes": 0}
    employees = {e["ad_dn"].lower(): e for e in db.q("SELECT * FROM employees WHERE ad_dn IS NOT NULL")}
    expected = defaultdict(set)
    for ent in db.q("SELECT en.group_name, e.ad_dn FROM entitlements en JOIN employees e ON e.id = en.employee_id "
                    "WHERE en.revoked_at IS NULL AND e.ad_dn IS NOT NULL"):
        expected[ent["group_name"]].add(ent["ad_dn"].lower())

    actual = ad.managed_memberships()
    for group, members in actual.items():
        if group not in GROUPS:
            continue
        for dn in members - expected.get(group, set()):
            emp = employees.get(dn)
            who = emp["sam"] if emp else ad.sam_for_dn(dn)
            sev = "critical" if is_privileged(group) else "high"
            raise_alert("out_of_band_grant", sev, who,
                        {"group": group, "member_dn": dn, "privileged": is_privileged(group),
                         "note": "Present in Active Directory but never requested or approved in the portal",
                         "changed": _actor_for_group(group, dn)},
                        f"oob:{group}:{dn}")
            summary["out_of_band"] += 1
        for dn in expected.get(group, set()) - members:
            ad.add_member(group, employees[dn]["ad_dn"])
            db.audit("system", "drift_corrected", employees[dn]["sam"],
                     {"group": group, "note": "Approved access was missing in Active Directory and was restored"}, "warning")
            summary["drift_fixed"] += 1

    corp = ad.corp_users()
    for e in db.q("SELECT * FROM employees WHERE status = 'terminated' AND ad_dn IS NOT NULL"):
        info = corp.get(e["ad_dn"].lower())
        if info and info["enabled"]:
            raise_alert("leaver_enabled", "critical", e["sam"],
                        {"note": "The account of a former employee was enabled again in Active Directory", "end_date": e["end_date"]},
                        f"leaver_enabled:{e['sam']}:{db.today()}")
            summary["leaver_enabled"] += 1

    snapshot = db.kv_get("tier0_snapshot")
    current = {g: sorted(ad.group_members_by_name(g)) for g in TIER0}
    if snapshot is not None:
        for g in TIER0:
            before, after = set(snapshot.get(g, [])), set(current[g])
            for dn in after - before:
                raise_alert("tier0_change", "critical", ad.sam_for_dn(dn),
                            {"group": g, "action": "added", "changed": _actor_for_tier0(g, dn),
                             "note": "Tier-0 groups are outside the portal; every change needs a change ticket"},
                            f"tier0:{g}:{dn}:added:{db.now_iso()[:16]}")
                summary["tier0_changes"] += 1
            for dn in before - after:
                raise_alert("tier0_change", "medium", dn.split(",")[0][3:],
                            {"group": g, "action": "removed", "changed": _actor_for_tier0(g, dn)},
                            f"tier0:{g}:{dn}:removed:{db.now_iso()[:16]}")
                summary["tier0_changes"] += 1
    db.kv_set("tier0_snapshot", current)
    db.kv_set("last_reconcile", {"at": db.now_iso(), **summary})
    return summary


def _actor_for_group(group, member_dn):
    from .ad import group_dn
    return _actor_for(group_dn(group), member_dn)


def _actor_for_tier0(group, member_dn):
    c = ad.conn()
    c.search(BASE_DN, f"(&(objectClass=group)(sAMAccountName={group}))", attributes=["distinguishedName"])
    return _actor_for(c.entries[0].entry_dn, member_dn) if c.entries else None


# ------------------------------------------------------------------ tindak lanjut alert
def resolve(alert_id, user, action, note):
    a = db.one("SELECT * FROM alerts WHERE id = ?", (alert_id,))
    if not a or a["status"] != "open":
        raise ValueError("Alert not found or already resolved.")
    if len((note or "").strip()) < 5:
        raise ValueError("Add a note describing what you checked.")
    details = json.loads(a["details"])
    if a["kind"] == "out_of_band_grant":
        emp = jml.by_sam(a["subject"])
        if action == "remove":
            ad.remove_member(details["group"], details["member_dn"])
            db.audit(user["sam"], "out_of_band_removed", a["subject"], {"group": details["group"], "note": note})
        elif action == "adopt":
            if not emp or emp["status"] != "active":
                raise ValueError("Only access held by an active employee can be accepted as an exception.")
            db.run("INSERT INTO entitlements (employee_id, group_name, source, granted_at, expires_at) "
                   "VALUES (?,?,?,?,?)", (emp["id"], details["group"], "exception", db.now_iso(),
                                          (db.now() + timedelta(days=30)).date().isoformat()))
            db.audit(user["sam"], "out_of_band_adopted", a["subject"],
                     {"group": details["group"], "note": note, "expires_in_days": 30})
    elif a["kind"] == "leaver_enabled" and action == "remove":
        emp = jml.by_sam(a["subject"])
        ad.set_enabled(emp["ad_dn"], False)
        db.audit(user["sam"], "leaver_disabled_again", a["subject"], {"note": note})
    db.run("UPDATE alerts SET status = 'resolved', resolved_by = ?, resolved_at = ?, resolution = ? WHERE id = ?",
           (user["sam"], db.now_iso(), f"{action}: {note}", alert_id))
    db.audit(user["sam"], "alert_resolved", f"alert #{alert_id}", {"action": action, "note": note})


def expire_exceptions():
    for ent in db.q("SELECT id FROM entitlements WHERE source = 'exception' AND revoked_at IS NULL AND expires_at < ?",
                    (db.today(),)):
        jml.revoke(ent["id"], "exception expired", "system")


def auth_series(hours=24):
    since = (db.now() - timedelta(hours=hours)).replace(minute=0, second=0)
    rows = db.q("SELECT substr(ts, 1, 13) AS h, SUM(success) ok, SUM(1 - success) bad FROM auth_events "
                "WHERE ts >= ? GROUP BY h", (since.isoformat(),))
    m = {r["h"]: r for r in rows}
    out = []
    for i in range(hours):
        t = since + timedelta(hours=i + 1)
        key = t.strftime("%Y-%m-%dT%H")
        r = m.get(key, {"ok": 0, "bad": 0})
        out.append({"hour": t.strftime("%H:00"), "ok": r["ok"] or 0, "bad": r["bad"] or 0})
    return out

