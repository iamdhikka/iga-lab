"""Angka dan seri untuk halaman Dashboard: tiket permintaan, SLA, JML, alert, dan access review."""
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

from . import certification as cert, db, jml
from .config import POSITIONS, TZ, is_privileged, position_privileged

SLA_HOURS = {"pending_manager": 48, "pending_security": 24}
APPROVED = ("scheduled", "active", "expired", "revoked")
OUTCOMES = [("approved", "Approved"), ("rejected", "Rejected"), ("cancelled", "Cancelled"), ("pending", "In progress")]


def _dt(s):
    return datetime.fromisoformat(s).astimezone(TZ) if s else None


def _hours(a, b):
    return (b - a).total_seconds() / 3600


def _outcome(status):
    if status in APPROVED:
        return "approved"
    if status in ("pending_manager", "pending_security"):
        return "pending"
    return "rejected" if status == "rejected" else "cancelled"


def _pct(values, p):
    if not values:
        return None
    v = sorted(values)
    k = (len(v) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def scope_filter(user, scope):
    """('all' untuk HR/Security) atau ('team' untuk manager: diri sendiri + bawahan langsung)."""
    if scope == "all" and ({"hr", "security"} & set(user["roles"])):
        return None, "all"
    ids = {user["employee_id"]} | {e["id"] for e in db.q("SELECT id FROM employees WHERE manager_id = ?",
                                                          (user["employee_id"],))}
    return ids, "team"


def build(user, days=30, scope="all"):
    ids, scope = scope_filter(user, scope)
    now = db.now()
    today = now.date()
    since = today - timedelta(days=days - 1)
    emps = {e["id"]: e for e in db.q("SELECT * FROM employees")}

    reqs = db.q("SELECT * FROM access_requests ORDER BY created_at")
    if ids is not None:
        reqs = [r for r in reqs if r["beneficiary_id"] in ids]
    in_range = [r for r in reqs if _dt(r["created_at"]).date() >= since]

    # ------------------------------------------------ antrean terbuka & SLA
    queue = []
    for r in reqs:
        if r["status"] not in SLA_HOURS:
            continue
        start = _dt(r["manager_at"]) if r["status"] == "pending_security" and r["manager_at"] else _dt(r["created_at"])
        age = _hours(start, now)
        sla = SLA_HOURS[r["status"]]
        waiting = ("IT Security" if r["status"] == "pending_security"
                   else jml.full_name(emps[r["manager_id"]]) if r["manager_id"] in emps else "Manager")
        queue.append({**r, "age_h": age, "total_age_h": _hours(_dt(r["created_at"]), now), "sla_h": sla,
                      "left_h": sla - age, "overdue": age > sla, "waiting": waiting,
                      "who": jml.full_name(emps[r["beneficiary_id"]]),
                      "dept": emps[r["beneficiary_id"]]["department"]})
    queue.sort(key=lambda x: x["left_h"])
    aging = Counter()
    for item in queue:
        h = item["total_age_h"]
        aging["< 1 day" if h < 24 else "1–2 days" if h < 48 else "2–3 days" if h < 72 else "> 3 days"] += 1

    # ------------------------------------------------ waktu keputusan per tahap
    mgr_h, sec_h, e2e_h = [], [], []
    for r in in_range:
        c = _dt(r["created_at"])
        if r["manager_at"]:
            mgr_h.append(_hours(c, _dt(r["manager_at"])))
        if r["security_at"]:
            sec_h.append(_hours(_dt(r["manager_at"]) or c, _dt(r["security_at"])))
        final = r["security_at"] if (r["privileged"] or not r["manager_at"]) else r["manager_at"]
        if r["status"] not in ("pending_manager", "pending_security", "cancelled") and final:
            e2e_h.append(_hours(c, _dt(final)))
    stages = [
        {"label": "Manager", "median": _pct(mgr_h, 50), "p90": _pct(mgr_h, 90), "n": len(mgr_h),
         "sla": SLA_HOURS["pending_manager"]},
        {"label": "IT Security", "median": _pct(sec_h, 50), "p90": _pct(sec_h, 90), "n": len(sec_h),
         "sla": SLA_HOURS["pending_security"]},
        {"label": "Request to decision", "median": _pct(e2e_h, 50), "p90": _pct(e2e_h, 90), "n": len(e2e_h),
         "sla": None},
    ]

    # ------------------------------------------------ volume harian per hasil
    labels = [(since + timedelta(days=i)) for i in range(days)]
    per_day = {o: [0] * days for o, _ in OUTCOMES}
    for r in in_range:
        i = (_dt(r["created_at"]).date() - since).days
        per_day[_outcome(r["status"])][i] += 1
    volume = {"labels": [d.strftime("%-d %b") for d in labels],
              "series": [{"key": o, "label": lbl, "data": per_day[o]} for o, lbl in OUTCOMES]}

    decided = [r for r in in_range if _outcome(r["status"]) in ("approved", "rejected")]
    approved = [r for r in decided if _outcome(r["status"]) == "approved"]
    sla_met = [r for r in in_range if r["manager_at"]
               and _hours(_dt(r["created_at"]), _dt(r["manager_at"])) <= SLA_HOURS["pending_manager"]]

    # ------------------------------------------------ akses yang paling sering diminta & per departemen
    top = Counter()
    for r in in_range:
        top[r["target"] if r["kind"] == "group" else f"Acting {POSITIONS[r['target']]['title']}"] += 1
    top_access = [{"label": k, "count": v,
                   "privileged": is_privileged(k) or (k.startswith("Acting ") and any(
                       position_privileged(p) for p, m in POSITIONS.items() if f"Acting {m['title']}" == k))}
                  for k, v in top.most_common(8)]
    dept = defaultdict(lambda: {"total": 0, "approved": 0, "rejected": 0})
    for r in in_range:
        d = dept[emps[r["beneficiary_id"]]["department"]]
        d["total"] += 1
        o = _outcome(r["status"])
        if o in ("approved", "rejected"):
            d[o] += 1
    by_dept = sorted(({"dept": k, **v, "rate": round(v["approved"] * 100 / (v["approved"] + v["rejected"]))
                       if v["approved"] + v["rejected"] else None} for k, v in dept.items()),
                     key=lambda x: -x["total"])

    # ------------------------------------------------ akses sementara
    ents = db.q("SELECT en.*, e.first_name, e.last_name FROM entitlements en JOIN employees e ON e.id = en.employee_id "
                "WHERE en.revoked_at IS NULL AND en.expires_at IS NOT NULL ORDER BY en.expires_at")
    if ids is not None:
        ents = [e for e in ents if e["employee_id"] in ids]
    week = (today + timedelta(days=7)).isoformat()
    expiring = [e for e in ents if e["expires_at"] <= week]

    # ------------------------------------------------ JML per minggu (12 minggu)
    start_week = today - timedelta(days=today.weekday()) - timedelta(weeks=11)
    weeks = [start_week + timedelta(weeks=i) for i in range(12)]
    jml_series = {k: [0] * 12 for k in ("joiner", "mover", "leaver")}
    for ev in db.q("SELECT * FROM hr_events WHERE status = 'done' AND effective_date >= ?", (start_week.isoformat(),)):
        if ids is not None and ev["employee_id"] not in ids:
            continue
        i = (date.fromisoformat(ev["effective_date"]) - start_week).days // 7
        if 0 <= i < 12:
            jml_series[ev["kind"]][i] += 1
    jml_chart = {"labels": [w.strftime("%-d %b") for w in weeks],
                 "series": [{"key": k, "label": k.capitalize() + "s", "data": v} for k, v in jml_series.items()]}
    scheduled = db.q("SELECT h.*, e.first_name, e.last_name FROM hr_events h JOIN employees e ON e.id = h.employee_id "
                     "WHERE h.status = 'scheduled' ORDER BY effective_date LIMIT 6")
    failed_hr = db.one("SELECT COUNT(*) n FROM hr_events WHERE status = 'failed'")["n"]

    # ------------------------------------------------ alert (hanya untuk Security / HR, cakupan organisasi)
    alerts = None
    if ids is None:
        a_since = today - timedelta(days=13)
        rows = db.q("SELECT * FROM alerts WHERE ts >= ?", (a_since.isoformat(),))
        a_days = [a_since + timedelta(days=i) for i in range(14)]
        sev_order = ["critical", "high", "medium", "low"]
        per = {s: [0] * 14 for s in sev_order}
        for a in rows:
            i = (_dt(a["ts"]).date() - a_since).days
            if 0 <= i < 14:
                per[a["severity"]][i] += 1
        kinds = Counter(a["kind"] for a in rows)
        open_rows = [a for a in db.q("SELECT * FROM alerts WHERE status = 'open'")]
        resolved = [a for a in rows if a["status"] == "resolved" and a["resolved_at"]]
        mttr = _pct([_hours(_dt(a["ts"]), _dt(a["resolved_at"])) for a in resolved], 50)
        alerts = {"labels": [d.strftime("%-d %b") for d in a_days],
                  "totals": [sum(per[s][i] for s in sev_order) for i in range(14)],
                  "by_severity": {s: sum(per[s]) for s in sev_order},
                  "kinds": kinds.most_common(), "open": len(open_rows),
                  "open_critical": len([a for a in open_rows if a["severity"] in ("critical", "high")]),
                  "mttr_h": mttr}

    # ------------------------------------------------ access review
    campaign = db.one("SELECT * FROM campaigns WHERE status = 'open' ORDER BY created_at DESC LIMIT 1")
    review = None
    if campaign:
        reviewers = cert.by_reviewer(campaign["id"])
        if ids is not None:
            reviewers = [r for r in reviewers if r["id"] in ids]
        review = {"c": campaign, "s": cert.stats(campaign["id"]), "reviewers": reviewers[:8]}

    kpi = {
        "open": len(queue),
        "overdue": len([q for q in queue if q["overdue"]]),
        "median_h": _pct(e2e_h, 50),
        "approval_rate": round(len(approved) * 100 / len(decided)) if decided else None,
        "sla_rate": round(len(sla_met) * 100 / len([r for r in in_range if r["manager_at"]]))
        if [r for r in in_range if r["manager_at"]] else None,
        "requests": len(in_range),
        "decided": len(decided),
        "temporary": len(ents),
        "expiring": len(expiring),
        "privileged_temp": len([e for e in ents if is_privileged(e["group_name"])]),
    }
    return {"scope": scope, "days": days, "kpi": kpi, "queue": queue, "aging": aging, "stages": stages,
            "volume": volume, "top_access": top_access, "by_dept": by_dept, "expiring": expiring,
            "jml": jml_chart, "scheduled": scheduled, "failed_hr": failed_hr, "alerts": alerts, "review": review,
            "employees": emps}


def fmt_hours(h):
    if h is None:
        return "–"
    if h < 1:
        return f"{max(1, round(h * 60))} min"
    if h < 48:
        return f"{h:.1f} h" if h < 10 else f"{round(h)} h"
    return f"{h / 24:.1f} days"

