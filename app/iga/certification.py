"""Access certification: manager meninjau akses anak buahnya secara berkala.

- Item review dibuat dari entitlement aktif saat kampanye dimulai.
- Keputusan 'revoke' langsung mencabut akses di AD.
- Item yang tidak diputuskan sampai kampanye ditutup dicatat sebagai 'tidak ditinjau' (temuan audit)."""
from . import db, jml
from .config import is_privileged


class CertError(Exception):
    pass


def create(name, scope, due_date, actor_user):
    if scope not in ("all", "privileged"):
        raise CertError("Unknown campaign scope.")
    if due_date < db.today():
        raise CertError("The due date can't be in the past.")
    cid = db.run("INSERT INTO campaigns (name, scope, created_by, created_at, due_date, status) VALUES (?,?,?,?,?,?)",
                 (name.strip(), scope, actor_user["sam"], db.now_iso(), due_date, "open"))
    rows = db.q("SELECT en.id AS ent_id, en.group_name, en.source, e.id AS emp_id, e.manager_id FROM entitlements en "
                "JOIN employees e ON e.id = en.employee_id WHERE en.revoked_at IS NULL AND e.status = 'active'")
    created = skipped = 0
    for r in rows:
        if scope == "privileged" and not is_privileged(r["group_name"]):
            continue
        reviewer = r["manager_id"]
        if not reviewer or reviewer == r["emp_id"]:
            skipped += 1  # tidak ada atasan: perlu ditinjau IT Security secara terpisah
            continue
        db.run("INSERT INTO review_items (campaign_id, reviewer_id, employee_id, entitlement_id, group_name, source) "
               "VALUES (?,?,?,?,?,?)", (cid, reviewer, r["emp_id"], r["ent_id"], r["group_name"], r["source"]))
        created += 1
    db.audit(actor_user["sam"], "campaign_started", f"campaign #{cid}",
             {"name": name, "scope": scope, "items": created, "no_reviewer": skipped, "due": due_date})
    return cid


def decide(item_id, user, decision, comment=""):
    item = db.one("SELECT ri.*, c.status AS cstatus FROM review_items ri JOIN campaigns c ON c.id = ri.campaign_id "
                  "WHERE ri.id = ?", (item_id,))
    if not item or item["reviewer_id"] != user["employee_id"]:
        raise CertError("This item isn't assigned to you.")
    if item["cstatus"] != "open":
        raise CertError("This campaign is closed.")
    if item["decision"]:
        raise CertError("This item was already decided.")
    if decision not in ("keep", "revoke"):
        raise CertError("Unknown decision.")
    db.run("UPDATE review_items SET decision = ?, comment = ?, decided_at = ? WHERE id = ?",
           (decision, comment, db.now_iso(), item_id))
    emp = jml.employee(item["employee_id"])
    db.audit(user["sam"], f"certify_{decision}", emp["sam"],
             {"campaign": item["campaign_id"], "group": item["group_name"], "comment": comment})
    if decision == "revoke":
        ent = db.one("SELECT * FROM entitlements WHERE id = ?", (item["entitlement_id"],))
        if ent and not ent["revoked_at"]:
            if ent["request_id"]:
                from . import requests_flow
                req = db.one("SELECT * FROM access_requests WHERE id = ?", (ent["request_id"],))
                if req and req["status"] == "active":
                    requests_flow.end(req, "revoked", user["sam"])
                    return
            jml.revoke(ent["id"], f"access review #{item['campaign_id']}", user["sam"])


def close(cid, actor_user):
    c = db.one("SELECT * FROM campaigns WHERE id = ?", (cid,))
    if not c or c["status"] != "open":
        raise CertError("Campaign not found or already closed.")
    pending = db.one("SELECT COUNT(*) n FROM review_items WHERE campaign_id = ? AND decision IS NULL", (cid,))["n"]
    db.run("UPDATE campaigns SET status = 'closed', closed_at = ? WHERE id = ?", (db.now_iso(), cid))
    db.audit(actor_user["sam"] if actor_user else "system", "campaign_closed", f"campaign #{cid}",
             {"not_reviewed": pending})


def stats(cid):
    row = db.one("SELECT COUNT(*) total, SUM(decision IS NOT NULL) done, SUM(decision = 'revoke') revoked, "
                 "SUM(decision = 'keep') kept FROM review_items WHERE campaign_id = ?", (cid,))
    total = row["total"] or 0
    done = row["done"] or 0
    return {"total": total, "done": done, "revoked": row["revoked"] or 0, "kept": row["kept"] or 0,
            "pct": round(done * 100 / total) if total else 100}


def by_reviewer(cid):
    return db.q("SELECT e.id, e.first_name, e.last_name, e.sam, COUNT(*) total, SUM(ri.decision IS NOT NULL) done, "
                "SUM(ri.decision = 'revoke') revoked FROM review_items ri JOIN employees e ON e.id = ri.reviewer_id "
                "WHERE ri.campaign_id = ? GROUP BY e.id ORDER BY (COUNT(*) - SUM(ri.decision IS NOT NULL)) DESC, "
                "e.first_name", (cid,))


def my_items(user, cid=None):
    sql = ("SELECT ri.*, e.first_name, e.last_name, e.sam, e.position, en.granted_at, en.expires_at, c.name AS cname, "
           "c.due_date FROM review_items ri JOIN employees e ON e.id = ri.employee_id "
           "JOIN entitlements en ON en.id = ri.entitlement_id JOIN campaigns c ON c.id = ri.campaign_id "
           "WHERE ri.reviewer_id = ? AND c.status = 'open'")
    args = [user["employee_id"]]
    if cid:
        sql += " AND c.id = ?"
        args.append(cid)
    return db.q(sql + " ORDER BY ri.decision IS NOT NULL, e.first_name, e.last_name, ri.group_name", args)


def close_overdue():
    for c in db.q("SELECT id FROM campaigns WHERE status = 'open' AND due_date < ?", (db.today(),)):
        close(c["id"], None)
