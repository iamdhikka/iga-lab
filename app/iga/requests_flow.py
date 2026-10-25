"""Permintaan akses sementara dan jabatan sementara (acting).

Alur:
  pending_manager --(manager setuju)--> [pending_security jika privileged] --> scheduled/active
  active --(tanggal berakhir)--> expired   (akses dicabut otomatis oleh worker)
  active --(dicabut manual)--> revoked
Aturan pemisahan tugas: peminta tidak boleh menyetujui permintaannya sendiri."""
from datetime import date, timedelta

from . import db, jml
from .config import ACTING_MAX_DAYS, GROUPS, POSITIONS, is_privileged, position_privileged


class RequestError(Exception):
    pass


def _d(s):
    return date.fromisoformat(s)


def validate(kind, target, beneficiary, start, end):
    if _d(end) < _d(start):
        raise RequestError("The end date must be on or after the start date.")
    if _d(start) < date.fromisoformat(db.today()):
        raise RequestError("The start date can't be in the past.")
    days = (_d(end) - _d(start)).days + 1
    if kind == "group":
        meta = GROUPS.get(target)
        if not meta or not meta.get("requestable"):
            raise RequestError(f"{target} can't be requested here. It comes with a position.")
        if days > meta.get("max_days", 90):
            raise RequestError(f"{target} can be granted for at most {meta.get('max_days')} days per request.")
        held = {e["group_name"] for e in jml.active_entitlements(beneficiary["id"])}
        if target in held:
            raise RequestError(f"{jml.full_name(beneficiary)} already has {target}.")
    elif kind == "acting":
        if target not in POSITIONS:
            raise RequestError("That position isn't in the catalog.")
        if target == beneficiary["position"]:
            raise RequestError("That is already their position.")
        if days > ACTING_MAX_DAYS:
            raise RequestError(f"An acting position can last at most {ACTING_MAX_DAYS} days.")
        if beneficiary["acting_position"]:
            raise RequestError("This person is already acting in another position.")
    else:
        raise RequestError("Unknown request type.")
    pending = db.one("SELECT id FROM access_requests WHERE beneficiary_id = ? AND kind = ? AND target = ? AND status IN "
                     "('pending_manager','pending_security','scheduled')", (beneficiary["id"], kind, target))
    if pending:
        raise RequestError(f"The same request (#{pending['id']}) is still open.")


def create(kind, target, beneficiary_id, requested_by, justification, start, end):
    ben = jml.employee(beneficiary_id)
    if ben["status"] != "active":
        raise RequestError("Requests can only be made for active employees.")
    if not justification or len(justification.strip()) < 15:
        raise RequestError("Describe the business reason in at least 15 characters so the approver can judge it.")
    validate(kind, target, ben, start, end)
    privileged = is_privileged(target) if kind == "group" else position_privileged(target)
    manager_id = ben["manager_id"]
    # Tanpa manager (mis. pimpinan puncak) atau manager = peminta: langsung ke IT Security.
    status = "pending_manager" if manager_id and manager_id != requested_by else "pending_security"
    rid = db.run("INSERT INTO access_requests (kind, target, beneficiary_id, requested_by, justification, start_date,"
                 " end_date, privileged, status, manager_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 (kind, target, ben["id"], requested_by, justification.strip(), start, end, int(privileged), status,
                  manager_id, db.now_iso()))
    actor = jml.employee(requested_by)["sam"]
    db.audit(actor, "request_created", ben["sam"], {"request": rid, "kind": kind, "target": target,
                                                    "start": start, "end": end, "privileged": privileged})
    return rid


def get(rid):
    r = db.one("SELECT * FROM access_requests WHERE id = ?", (rid,))
    if not r:
        raise RequestError("Request not found.")
    return r


def can_decide(req, user):
    """user: dict sesi {employee_id, roles}. Kembalikan 'manager' / 'security' / None."""
    if user["employee_id"] in (req["requested_by"], req["beneficiary_id"]):
        return None  # pemisahan tugas
    if req["status"] == "pending_manager" and req["manager_id"] == user["employee_id"]:
        return "manager"
    if req["status"] == "pending_security" and "security" in user["roles"]:
        return "security"
    return None


def decide(rid, user, approve, comment=""):
    req = get(rid)
    stage = can_decide(req, user)
    if not stage:
        raise RequestError("You can't decide on this request. Requests can't be approved by the requester or the person who gets the access.")
    if not approve and len((comment or "").strip()) < 5:
        raise RequestError("Add a comment so the requester knows why it was rejected.")
    actor = user["sam"]
    ts = db.now_iso()
    decision = "approved" if approve else "rejected"
    if stage == "manager":
        db.run("UPDATE access_requests SET manager_decision = ?, manager_at = ?, manager_comment = ? WHERE id = ?",
               (decision, ts, comment, rid))
    else:
        db.run("UPDATE access_requests SET security_by = ?, security_decision = ?, security_at = ?, "
               "security_comment = ? WHERE id = ?", (user["employee_id"], decision, ts, comment, rid))
    db.audit(actor, f"request_{decision}", f"#{rid}", {"stage": stage, "comment": comment})

    if not approve:
        db.run("UPDATE access_requests SET status = 'rejected', closed_at = ? WHERE id = ?", (ts, rid))
        return "rejected"
    if stage == "manager" and req["privileged"]:
        db.run("UPDATE access_requests SET status = 'pending_security' WHERE id = ?", (rid,))
        return "pending_security"
    db.run("UPDATE access_requests SET status = 'scheduled' WHERE id = ?", (rid,))
    activate_due()
    return get(rid)["status"]


def cancel(rid, user):
    req = get(rid)
    if req["requested_by"] != user["employee_id"] or req["status"] not in ("pending_manager", "pending_security",
                                                                            "scheduled"):
        raise RequestError("This request can't be cancelled anymore.")
    db.run("UPDATE access_requests SET status = 'cancelled', closed_at = ? WHERE id = ?", (db.now_iso(), rid))
    db.audit(user["sam"], "request_cancelled", f"#{rid}")


def _activate(req):
    actor = f"system (request #{req['id']})"
    ben = jml.employee(req["beneficiary_id"])
    if ben["status"] != "active":
        db.run("UPDATE access_requests SET status = 'cancelled', closed_at = ? WHERE id = ?", (db.now_iso(), req["id"]))
        return
    if req["kind"] == "group":
        jml.grant(ben, req["target"], "request", actor, request_id=req["id"], expires_at=req["end_date"])
    else:
        jml.start_acting(req, actor)
    db.run("UPDATE access_requests SET status = 'active' WHERE id = ?", (req["id"],))


def activate_due():
    for req in db.q("SELECT * FROM access_requests WHERE status = 'scheduled' AND start_date <= ?", (db.today(),)):
        try:
            _activate(req)
        except Exception as exc:
            db.audit("system", "request_activation_failed", f"#{req['id']}", {"error": str(exc)}, "failure")


def expire_due():
    """Akses berakhir di akhir hari end_date: dicabut saat tanggal sudah melewati end_date."""
    for req in db.q("SELECT * FROM access_requests WHERE status = 'active' AND end_date < ?", (db.today(),)):
        end(req, "expired", f"system (request #{req['id']})")


def end(req, final_status, actor):
    reason = "expired" if final_status == "expired" else f"removed early by {actor}"
    if req["kind"] == "group":
        for ent in db.q("SELECT id FROM entitlements WHERE request_id = ? AND revoked_at IS NULL", (req["id"],)):
            jml.revoke(ent["id"], reason, actor)
    else:
        jml.end_acting(req, actor, reason)
    db.run("UPDATE access_requests SET status = ?, closed_at = ? WHERE id = ?", (final_status, db.now_iso(), req["id"]))
    db.audit(actor, f"request_{final_status}", f"#{req['id']}")


def revoke_now(rid, user):
    req = get(rid)
    if req["status"] != "active":
        raise RequestError("Only active access can be removed.")
    if not ("security" in user["roles"] or req["manager_id"] == user["employee_id"]):
        raise RequestError("Only the person's manager or IT Security can remove this access.")
    end(req, "revoked", user["sam"])


def inbox(user):
    rows = db.q("SELECT * FROM access_requests WHERE status IN ('pending_manager','pending_security') "
                "ORDER BY created_at")
    return [r for r in rows if can_decide(r, user)]


def describe_target(req):
    if req["kind"] == "group":
        return req["target"], GROUPS.get(req["target"], {}).get("description", "")
    return f"Acting {POSITIONS[req['target']]['title']}", "Temporary position"


def max_end(kind, target, start):
    days = GROUPS.get(target, {}).get("max_days", 90) if kind == "group" else ACTING_MAX_DAYS
    return (_d(start) + timedelta(days=days - 1)).isoformat()
