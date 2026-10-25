"""Mesin Joiner-Mover-Leaver dan pengelolaan entitlement.

Prinsip:
- Setiap akses di AD yang dikelola IGA tercatat sebagai entitlement dengan sumber (birthright/request/acting).
- Group di AD dicabut hanya jika tidak ada lagi entitlement aktif untuk group tersebut.
- Setiap perubahan dicatat di audit log."""
import re
import unicodedata

from . import ad, db
from .config import DISABLED_OU, POSITIONS


class JMLError(Exception):
    pass


# ------------------------------------------------------------------ helper
def employee(emp_id):
    emp = db.one("SELECT * FROM employees WHERE id = ?", (emp_id,))
    if not emp:
        raise JMLError(f"Employee {emp_id} not found")
    return emp


def by_sam(sam):
    return db.one("SELECT * FROM employees WHERE lower(sam) = lower(?)", (sam,))


def full_name(emp):
    return f"{emp['first_name']} {emp['last_name']}"


def next_employee_id():
    row = db.one("SELECT id FROM employees ORDER BY CAST(SUBSTRING(id, 2) AS UNSIGNED) DESC LIMIT 1")
    return f"E{(int(row['id'][1:]) + 1) if row else 1001}"


def generate_sam(first, last):
    def clean(s):
        s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
        return re.sub(r"[^a-z]", "", s.lower())
    base = f"{clean(first)}.{clean(last)}"[:18].strip(".") or "user"
    sam, n = base, 2
    while db.one("SELECT 1 FROM employees WHERE lower(sam) = ?", (sam,)) or ad.sam_exists(sam):
        sam, n = f"{base}{n}", n + 1
    return sam


def active_entitlements(emp_id):
    return db.q("SELECT * FROM entitlements WHERE employee_id = ? AND revoked_at IS NULL ORDER BY group_name",
                (emp_id,))


def manager_dn(manager_id):
    if not manager_id:
        return None
    m = db.one("SELECT ad_dn FROM employees WHERE id = ?", (manager_id,))
    return m["ad_dn"] if m else None


# ------------------------------------------------------------------ entitlement
def grant(emp, group, source, actor, request_id=None, expires_at=None):
    if not emp.get("ad_dn"):
        raise JMLError(f"{emp['id']} has no AD account yet")
    ad.add_member(group, emp["ad_dn"])
    ent_id = db.run("INSERT INTO entitlements (employee_id, group_name, source, request_id, granted_at, expires_at) "
                    "VALUES (?,?,?,?,?,?)", (emp["id"], group, source, request_id, db.now_iso(), expires_at))
    db.audit(actor, "grant", emp["sam"], {"group": group, "source": source, "request": request_id,
                                          "expires": expires_at})
    return ent_id


def revoke(ent_id, reason, actor):
    ent = db.one("SELECT * FROM entitlements WHERE id = ?", (ent_id,))
    if not ent or ent["revoked_at"]:
        return
    emp = employee(ent["employee_id"])
    db.run("UPDATE entitlements SET revoked_at = ?, revoked_reason = ? WHERE id = ?", (db.now_iso(), reason, ent_id))
    still = db.one("SELECT 1 FROM entitlements WHERE employee_id = ? AND group_name = ? AND revoked_at IS NULL",
                   (emp["id"], ent["group_name"]))
    if not still and emp.get("ad_dn"):
        ad.remove_member(ent["group_name"], emp["ad_dn"])
    db.audit(actor, "revoke", emp["sam"], {"group": ent["group_name"], "source": ent["source"], "reason": reason,
                                           "removed_from_ad": not still})


# ------------------------------------------------------------------ joiner
def create_employee(first, last, department, position, manager_id, start_date, actor, emp_id=None):
    if position not in POSITIONS:
        raise JMLError(f"Position {position} is not in the catalog")
    if POSITIONS[position]["department"] != department:
        raise JMLError("Position doesn't belong to that department")
    emp_id = emp_id or next_employee_id()
    ts = db.now_iso()
    db.run("INSERT INTO employees (id, first_name, last_name, department, position, manager_id, status, start_date,"
           " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
           (emp_id, first.strip(), last.strip(), department, position, manager_id or None, "scheduled",
            start_date, ts, ts))
    schedule(emp_id, "joiner", start_date, {}, actor)
    return emp_id


def _joiner(emp, actor):
    sam = emp["sam"] or generate_sam(emp["first_name"], emp["last_name"])
    emp = {**emp, "sam": sam}
    dn = ad.create_user(emp, POSITIONS[emp["position"]]["title"], manager_dn(emp["manager_id"]))
    db.run("UPDATE employees SET sam = ?, ad_dn = ?, status = 'active', updated_at = ? WHERE id = ?",
           (sam, dn, db.now_iso(), emp["id"]))
    emp = employee(emp["id"])
    db.audit(actor, "joiner", sam, {"employee": emp["id"], "position": emp["position"], "dn": dn})
    for g in POSITIONS[emp["position"]]["groups"]:
        grant(emp, g, "birthright", actor)


# ------------------------------------------------------------------ mover
def _mover(emp, payload, actor):
    new_pos = payload.get("position", emp["position"])
    new_dept = POSITIONS[new_pos]["department"]
    new_mgr = payload.get("manager_id", emp["manager_id"]) or None
    old_groups = set(POSITIONS[emp["position"]]["groups"])
    new_groups = set(POSITIONS[new_pos]["groups"])

    dn = ad.move_user(emp["ad_dn"], ad.dept_ou(new_dept))
    ad.update_user(dn, {"title": POSITIONS[new_pos]["title"], "department": new_dept,
                        "manager": manager_dn(new_mgr)})
    db.run("UPDATE employees SET department = ?, position = ?, manager_id = ?, ad_dn = ?, updated_at = ? WHERE id = ?",
           (new_dept, new_pos, new_mgr, dn, db.now_iso(), emp["id"]))
    emp = employee(emp["id"])
    db.audit(actor, "mover", emp["sam"], {"from": payload.get("_from"), "to": new_pos, "manager": new_mgr})

    # Akses bawaan jabatan lama dicabut; akses bawaan jabatan baru diberikan.
    for ent in active_entitlements(emp["id"]):
        if ent["source"] == "birthright" and ent["group_name"] in old_groups - new_groups:
            revoke(ent["id"], f"moved: not part of {POSITIONS[new_pos]['title']}", actor)
    held = {e["group_name"] for e in active_entitlements(emp["id"]) if e["source"] == "birthright"}
    for g in sorted(new_groups - held):
        grant(emp, g, "birthright", actor)


# ------------------------------------------------------------------ leaver
def _leaver(emp, actor):
    for ent in active_entitlements(emp["id"]):
        revoke(ent["id"], "left the company", actor)
    ad.set_enabled(emp["ad_dn"], False)
    dn = ad.move_user(emp["ad_dn"], DISABLED_OU)
    ad.update_user(dn, {"employeeType": "Terminated", "description": f"Terminated {db.today()} via IGA",
                        "manager": None})
    for r in db.q("SELECT id FROM access_requests WHERE beneficiary_id = ? AND status IN "
                  "('pending_manager','pending_security','scheduled')", (emp["id"],)):
        db.run("UPDATE access_requests SET status = 'cancelled', closed_at = ? WHERE id = ?", (db.now_iso(), r["id"]))
    db.run("UPDATE employees SET status = 'terminated', end_date = ?, ad_dn = ?, acting_position = NULL, "
           "acting_until = NULL, updated_at = ? WHERE id = ?", (db.today(), dn, db.now_iso(), emp["id"]))
    db.audit(actor, "leaver", emp["sam"], {"employee": emp["id"], "dn": dn})
    orphans = db.q("SELECT id FROM employees WHERE manager_id = ? AND status != 'terminated'", (emp["id"],))
    if orphans:
        db.audit("system", "orphaned_reports", emp["sam"],
                 {"count": len(orphans), "note": "HR needs to assign a new manager"}, result="warning")


# ------------------------------------------------------------------ penjadwalan
def schedule(emp_id, kind, effective_date, payload, actor):
    import json
    ev = db.run("INSERT INTO hr_events (employee_id, kind, effective_date, payload, status, created_by, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (emp_id, kind, effective_date, json.dumps(payload), "scheduled", actor, db.now_iso()))
    db.audit(actor, f"{kind}_scheduled", emp_id, {"effective": effective_date, **payload})
    if effective_date <= db.today():
        process_event(ev)
    return ev


def process_event(ev_id):
    import json
    ev = db.one("SELECT * FROM hr_events WHERE id = ?", (ev_id,))
    if not ev or ev["status"] != "scheduled":
        return
    emp = employee(ev["employee_id"])
    actor = f"{ev['created_by']} (via HR)"
    try:
        if ev["kind"] == "joiner":
            _joiner(emp, actor)
        elif ev["kind"] == "mover":
            _mover(emp, json.loads(ev["payload"]), actor)
        elif ev["kind"] == "leaver":
            _leaver(emp, actor)
        db.run("UPDATE hr_events SET status = 'done', processed_at = ? WHERE id = ?", (db.now_iso(), ev_id))
    except Exception as exc:
        db.run("UPDATE hr_events SET status = 'failed', error = ?, processed_at = ? WHERE id = ?",
               (str(exc), db.now_iso(), ev_id))
        db.audit("system", f"{ev['kind']}_failed", emp.get("sam") or emp["id"], {"error": str(exc)}, "failure")
        raise


def process_due_events():
    for ev in db.q("SELECT id FROM hr_events WHERE status = 'scheduled' AND effective_date <= ? ORDER BY id",
                   (db.today(),)):
        try:
            process_event(ev["id"])
        except Exception:
            pass  # sudah dicatat sebagai failed


# ------------------------------------------------------------------ jabatan sementara (acting)
def start_acting(req, actor):
    emp = employee(req["beneficiary_id"])
    pos = POSITIONS[req["target"]]
    held = {e["group_name"] for e in active_entitlements(emp["id"])}
    added = []
    for g in pos["groups"]:
        if g not in held:
            grant(emp, g, "acting", actor, request_id=req["id"], expires_at=req["end_date"])
            added.append(g)
    ad.update_user(emp["ad_dn"], {"description": f"Acting {pos['title']} until {req['end_date']}"})
    db.run("UPDATE employees SET acting_position = ?, acting_until = ?, updated_at = ? WHERE id = ?",
           (req["target"], req["end_date"], db.now_iso(), emp["id"]))
    db.audit(actor, "acting_started", emp["sam"], {"position": req["target"], "until": req["end_date"],
                                                   "added_groups": added, "request": req["id"]})


def end_acting(req, actor, reason):
    emp = employee(req["beneficiary_id"])
    for ent in db.q("SELECT id FROM entitlements WHERE request_id = ? AND revoked_at IS NULL", (req["id"],)):
        revoke(ent["id"], reason, actor)
    if emp["acting_position"] == req["target"]:
        if emp.get("ad_dn"):
            ad.update_user(emp["ad_dn"], {"description": None})
        db.run("UPDATE employees SET acting_position = NULL, acting_until = NULL, updated_at = ? WHERE id = ?",
               (db.now_iso(), emp["id"]))
    db.audit(actor, "acting_ended", emp["sam"], {"position": req["target"], "reason": reason, "request": req["id"]})
