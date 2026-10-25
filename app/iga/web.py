"""Portal web IGA."""
import csv
import io
import json
import os
import secrets
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import ad, certification as cert, dashboard as dash, db, jml, requests_flow as rf, security, worker
from .config import DEPARTMENTS, GROUPS, POSITIONS, SECRET_KEY, TZ, is_privileged

BASE = Path(__file__).resolve().parent
app = FastAPI(title="Access Governance Portal", docs_url=None, redoc_url=None)
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, same_site="lax", max_age=8 * 3600)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
tpl = Jinja2Templates(directory=BASE / "templates")

STATUS_LABEL = {
    "pending_manager": "Waiting for manager", "pending_security": "Waiting for IT Security",
    "scheduled": "Approved, starts later", "active": "Active", "rejected": "Rejected", "cancelled": "Cancelled",
    "expired": "Ended", "revoked": "Revoked",
}
SOURCE_LABEL = {"birthright": "Role", "request": "Request", "acting": "Acting", "exception": "Exception"}
ALERT_LABEL = {
    "brute_force": "Repeated failed sign-ins", "password_spray": "Password spraying",
    "disabled_login": "Sign-in to a disabled account", "off_hours": "Sign-in outside working hours",
    "out_of_band_grant": "Access granted outside the portal", "tier0_change": "Tier-0 group changed",
    "leaver_enabled": "Leaver account re-enabled",
}


# ------------------------------------------------------------------ jinja helpers
def fmt_date(s):
    if not s:
        return ""
    d = date.fromisoformat(str(s)[:10])
    return d.strftime("%-d %b %Y")


def fmt_dt(s):
    if not s:
        return ""
    t = datetime.fromisoformat(s).astimezone(TZ)
    return t.strftime("%-d %b %Y, %H:%M")


def rel_days(s):
    if not s:
        return ""
    n = (date.fromisoformat(str(s)[:10]) - date.fromisoformat(db.today())).days
    if n == 0:
        return "today"
    if n == 1:
        return "tomorrow"
    if n == -1:
        return "yesterday"
    return f"in {n} days" if n > 0 else f"{-n} days ago"


def ago(s):
    if not s:
        return ""
    secs = (db.now() - datetime.fromisoformat(s).astimezone(TZ)).total_seconds()
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)} min ago"
    if secs < 86400:
        return f"{int(secs // 3600)} h ago"
    d = int(secs // 86400)
    return "yesterday" if d == 1 else f"{d} days ago"


# Ikon garis sederhana (24x24, stroke) untuk navigasi dan aksi cepat.
ICONS = {
    "home": "M3 10.5 12 3l9 7.5V20a1 1 0 0 1-1 1h-5v-6h-6v6H4a1 1 0 0 1-1-1z",
    "dashboard": "M4 4h6v9H4zM14 4h6v5h-6zM14 13h6v7h-6zM4 17h6v3H4z",
    "approvals": "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM8.5 12l2.5 2.5 4.5-5",
    "reviews": "M9 3h6v3H9zM15 4.5h3v16.5H6V4.5h3M9 13.5l2 2 4-4",
    "requests": "M4 6h16v3.5a2.5 2.5 0 0 0 0 5V18H4v-3.5a2.5 2.5 0 0 0 0-5zM10 6v12",
    "people": "M16 20v-1a4 4 0 0 0-4-4H7a4 4 0 0 0-4 4v1M9.5 11a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7M21 20v-1a4 4 0 0 0-3-3.9M15 4.2a3.5 3.5 0 0 1 0 6.6",
    "catalog": "M5 4.5A1.5 1.5 0 0 1 6.5 3H19v15H6.5A1.5 1.5 0 0 0 5 19.5zM5 19.5A1.5 1.5 0 0 0 6.5 21H19M9 7h6",
    "campaigns": "M4 6h16v15H4zM4 10h16M8 3v5M16 3v5M9 15.5l2 2 4-4",
    "security": "M12 3 4 6v6c0 4.5 3.4 8.3 8 9 4.6-.7 8-4.5 8-9V6z",
    "audit": "M9 6h11M9 12h11M9 18h11M4.5 6h.01M4.5 12h.01M4.5 18h.01",
    "plus": "M12 5v14M5 12h14",
    "swap": "M7 4 3 8l4 4M3 8h14M17 20l4-4-4-4M21 16H7",
    "user-plus": "M15 20v-1a4 4 0 0 0-4-4H7a4 4 0 0 0-4 4v1M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8M19 8v6M16 11h6",
    "timeline": "M4 6h9M4 12h14M4 18h6",
    "logout": "M14 4h5v16h-5M10 8l-4 4 4 4M6 12h10",
    "bolt": "M13 3 5 13.5h6L10 21l8-10.5h-6z",
}


def icon(name, size=18):
    from markupsafe import Markup
    return Markup(f'<svg class="ico" width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" '
                  f'stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" '
                  f'aria-hidden="true"><path d="{ICONS[name]}"/></svg>')


AVATAR_TONES = ["#D7EDE6", "#FBE7C6", "#E4E0F7", "#DCE8F7", "#F8DCD6", "#E3EED2"]


def avatar(name, size="md"):
    from markupsafe import Markup, escape
    name = name or "?"
    if name == "Portal":
        return Markup(f'<span class="av {size} bot" aria-hidden="true">{icon("bolt", 14)}</span>')
    parts = [p for p in name.replace(".", " ").split() if p]
    ini = (parts[0][0] + (parts[1][0] if len(parts) > 1 else "")).upper()
    tone = AVATAR_TONES[sum(map(ord, name)) % len(AVATAR_TONES)]
    return Markup(f'<span class="av {size}" style="background:{tone}" aria-hidden="true">{escape(ini)}</span>')


def describe(a, by_sam, by_id):
    """Ubah baris audit menjadi kalimat yang mudah dibaca: (nama pelaku, kalimat)."""
    actor_sam = (a["actor"] or "").split(" ")[0]
    actor = "Portal" if actor_sam in ("system", "-", "") else (
        jml.full_name(by_sam[actor_sam]) if actor_sam in by_sam else actor_sam)
    t = a["target"] or ""
    who = jml.full_name(by_sam[t]) if t in by_sam else jml.full_name(by_id[t]) if t in by_id else t
    try:
        d = json.loads(a["details"]) if a["details"] else {}
    except ValueError:
        d = {}
    d = d if isinstance(d, dict) else {}
    act = a["action"]
    pos = lambda k: POSITIONS.get(d.get(k) or "", {}).get("title", d.get(k) or "")
    text = {
        "grant": f"gave {who} {d.get('group', 'access')}" + (f" ({SOURCE_LABEL.get(d.get('source'), '').lower()})" if d.get('source') else ""),
        "revoke": f"removed {d.get('group', 'access')} from {who}" + (f": {d['reason']}" if d.get('reason') else ""),
        "joiner": f"onboarded {who}",
        "mover": f"moved {who} to {pos('to')}",
        "leaver": f"offboarded {who}",
        "joiner_scheduled": f"scheduled {who} to join",
        "mover_scheduled": f"scheduled a position change for {who}",
        "leaver_scheduled": f"scheduled {who}'s last day",
        "request_created": f"asked for {d.get('target') if d.get('kind') == 'group' else 'an acting role'} for {who}",
        "request_approved": f"approved request {t}",
        "request_rejected": f"rejected request {t}",
        "request_cancelled": f"cancelled request {t}",
        "request_expired": f"closed request {t} at its end date",
        "request_revoked": f"ended request {t} early",
        "certify_revoke": f"removed {d.get('group', 'access')} from {who} during review",
        "certify_keep": f"kept {d.get('group', 'access')} for {who}",
        "campaign_started": f"started an access review ({t})",
        "campaign_closed": f"closed an access review ({t})",
        "alert_raised": f"flagged {ALERT_LABEL.get(d.get('kind'), 'an alert').lower()} on {who}",
        "alert_resolved": f"resolved {t}",
        "out_of_band_removed": f"removed {d.get('group', 'access')} that was added to {who} outside the portal",
        "out_of_band_adopted": f"kept {d.get('group', 'access')} for {who} as an exception",
        "leaver_disabled_again": f"disabled {who}'s account again",
        "drift_corrected": f"restored missing {d.get('group', 'access')} for {who}",
        "acting_started": f"started {who}'s acting period as {pos('position')}",
        "acting_ended": f"ended {who}'s acting period",
        "demo_seeded": "loaded the demo organisation",
    }.get(act, f"{act.replace('_', ' ')} {who}".strip())
    return {"actor": actor, "text": text, "ts": a["ts"], "failed": a["result"] == "failure"}


tpl.env.filters.update(date=fmt_date, dt=fmt_dt, rel=rel_days, ago=ago, fromjson=json.loads, hours=dash.fmt_hours,
                       tojson_safe=lambda v: json.dumps(v).replace("</", "<\\/"))
tpl.env.globals.update(POSITIONS=POSITIONS, GROUPS=GROUPS, STATUS_LABEL=STATUS_LABEL, SOURCE_LABEL=SOURCE_LABEL,
                       ALERT_LABEL=ALERT_LABEL, is_privileged=is_privileged, full_name=jml.full_name,
                       icon=icon, avatar=avatar)


# ------------------------------------------------------------------ sesi & otorisasi
def roles_for(emp):
    groups = {e["group_name"] for e in jml.active_entitlements(emp["id"])}
    roles = []
    if "IGA-HR" in groups:
        roles.append("hr")
    if "IGA-Security" in groups:
        roles.append("security")
    if db.one("SELECT 1 FROM employees WHERE manager_id = ? AND status = 'active'", (emp["id"],)):
        roles.append("manager")
    return roles


def current_user(request: Request):
    sid = request.session.get("employee_id")
    if not sid:
        return None
    emp = db.one("SELECT * FROM employees WHERE id = ?", (sid,))
    if not emp or emp["status"] != "active":
        request.session.clear()
        return None
    return {"employee_id": emp["id"], "sam": emp["sam"], "name": jml.full_name(emp), "emp": emp,
            "roles": roles_for(emp)}


def require(request, *roles):
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    if roles and not any(r in user["roles"] for r in roles):
        raise HTTPException(status_code=403, detail="You don't have access to this page.")
    return user


def csrf_token(request):
    if "csrf" not in request.session:
        request.session["csrf"] = secrets.token_urlsafe(24)
    return request.session["csrf"]


def check_csrf(request, token):
    if not token or token != request.session.get("csrf"):
        raise HTTPException(status_code=400, detail="This form has expired. Reload the page and try again.")


def counts(user):
    if not user:
        return {}
    c = {"approvals": len(rf.inbox(user)), "reviews": len([i for i in cert.my_items(user) if not i["decision"]])}
    if "security" in user["roles"]:
        c["alerts"] = db.one("SELECT COUNT(*) n FROM alerts WHERE status = 'open' AND severity IN ('critical','high')")["n"]
    return c


def render(request, name, user, status_code=200, **ctx):
    flash = request.session.pop("flash", None)
    return tpl.TemplateResponse(request, name, {"user": user, "csrf": csrf_token(request), "flash": flash,
                                                "nav": counts(user), "today": db.today(), **ctx},
                                status_code=status_code)


def flash(request, kind, text):
    request.session["flash"] = {"kind": kind, "text": text}


def back(url):
    return RedirectResponse(url, status_code=303)


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    if exc.status_code == 303:
        return RedirectResponse(exc.headers["Location"], status_code=303)
    user = current_user(request)
    return render(request, "error.html", user, status_code=exc.status_code, code=exc.status_code,
                  message=exc.detail)


@app.on_event("startup")
def startup():
    db.init()
    ad.wait_until_ready()
    ad.ensure_structure(DEPARTMENTS)
    if os.environ.get("IGA_SEED_DEMO", "false").lower() == "true":
        from . import seed
        seed.run()
    worker.start()


# ------------------------------------------------------------------ login
@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return render(request, "login.html", None, error=None, username="")


@app.post("/login")
def login(request: Request, username: str = Form(""), password: str = Form(""), csrf: str = Form("")):
    check_csrf(request, csrf)
    sam = username.strip().split("@")[0].split("\\")[-1].lower()
    emp = jml.by_sam(sam)
    ok = emp and emp["status"] == "active" and ad.authenticate(sam, password)
    db.audit(sam or "-", "portal_login", sam, None, "success" if ok else "failure")
    if not ok:
        return render(request, "login.html", None, username=username,
                      error="Username or password is incorrect, or the account isn't an active employee.")
    request.session.clear()
    request.session["employee_id"] = emp["id"]
    return back("/")


@app.post("/logout")
def logout(request: Request, csrf: str = Form("")):
    check_csrf(request, csrf)
    request.session.clear()
    return back("/login")


# ------------------------------------------------------------------ beranda
@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    user = require(request)
    inbox = rf.inbox(user)
    reviews = cert.my_items(user)
    pending_reviews = [r for r in reviews if not r["decision"]]
    my_access = jml.active_entitlements(user["employee_id"])
    expiring = db.q("SELECT en.*, e.first_name, e.last_name, e.sam FROM entitlements en JOIN employees e "
                    "ON e.id = en.employee_id WHERE en.revoked_at IS NULL AND en.expires_at IS NOT NULL "
                    "AND (en.employee_id = ? OR e.manager_id = ?) ORDER BY en.expires_at LIMIT 8",
                    (user["employee_id"], user["employee_id"]))
    my_requests = db.q("SELECT * FROM access_requests WHERE requested_by = ? OR beneficiary_id = ? "
                       "ORDER BY created_at DESC LIMIT 5", (user["employee_id"], user["employee_id"]))
    health = None
    if "security" in user["roles"] or "hr" in user["roles"]:
        health = {
            "active": db.one("SELECT COUNT(*) n FROM employees WHERE status = 'active'")["n"],
            "temporary": db.one("SELECT COUNT(*) n FROM entitlements WHERE revoked_at IS NULL "
                                "AND source IN ('request','acting','exception')")["n"],
            "privileged": db.one("SELECT COUNT(DISTINCT employee_id) n FROM entitlements WHERE revoked_at IS NULL "
                                 "AND group_name IN (%s)" % ",".join("?" * len([g for g in GROUPS if is_privileged(g)])),
                                 [g for g in GROUPS if is_privileged(g)])["n"],
            "alerts": db.q("SELECT severity, COUNT(*) n FROM alerts WHERE status = 'open' GROUP BY severity"),
            "scheduled": db.q("SELECT h.*, e.first_name, e.last_name FROM hr_events h JOIN employees e "
                              "ON e.id = h.employee_id WHERE h.status = 'scheduled' ORDER BY effective_date LIMIT 5"),
            "reconcile": db.kv_get("last_reconcile"),
            "campaign": db.one("SELECT * FROM campaigns WHERE status = 'open' ORDER BY created_at DESC LIMIT 1"),
        }
        if health["campaign"]:
            health["campaign_stats"] = cert.stats(health["campaign"]["id"])
    activity = []
    if health:
        emps = db.q("SELECT * FROM employees")
        by_sam, by_id = {e["sam"]: e for e in emps if e["sam"]}, {e["id"]: e for e in emps}
        activity = [describe(a, by_sam, by_id) for a in db.q(
            "SELECT * FROM audit_log WHERE action NOT IN ('portal_login','grant','revoke','certify_keep','demo_seeded') "
            "ORDER BY id DESC LIMIT 7")]
    hour = db.now().hour
    greeting = "Good morning" if hour < 12 else ("Good afternoon" if hour < 18 else "Good evening")
    return render(request, "home.html", user, greeting=greeting, inbox=inbox, pending_reviews=pending_reviews, my_access=my_access,
                  expiring=expiring, my_requests=my_requests, health=health, activity=activity,
                  employees=_emp_map())


def _emp_map():
    return {e["id"]: e for e in db.q("SELECT * FROM employees")}


# ------------------------------------------------------------------ dashboard
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, days: int = 30, scope: str = "all"):
    user = require(request, "manager", "hr", "security")
    days = days if days in (7, 30, 90) else 30
    data = dash.build(user, days, scope)
    charts = {"volume": data["volume"], "jml": data["jml"],
              "stages": [{"label": s["label"], "median": s["median"], "p90": s["p90"]} for s in data["stages"]],
              "top": data["top_access"], "dept": data["by_dept"],
              "alerts": {"labels": data["alerts"]["labels"], "totals": data["alerts"]["totals"]}
              if data["alerts"] else None}
    return render(request, "dashboard.html", user, d=data, charts=charts)


# ------------------------------------------------------------------ orang
@app.get("/people", response_class=HTMLResponse)
def people(request: Request, q: str = "", dept: str = "", status: str = "active"):
    user = require(request)
    sql = "SELECT * FROM employees WHERE 1=1"
    args = []
    if not ({"hr", "security"} & set(user["roles"])):
        sql += " AND (manager_id = ? OR id = ?)"
        args += [user["employee_id"], user["employee_id"]]
    if q:
        sql += " AND (lower(CONCAT(first_name, ' ', last_name)) LIKE ? OR lower(sam) LIKE ? OR lower(id) LIKE ?)"
        args += [f"%{q.lower()}%"] * 3
    if dept:
        sql += " AND department = ?"
        args.append(dept)
    if status:
        sql += " AND status = ?"
        args.append(status)
    rows = db.q(sql + " ORDER BY first_name, last_name", args)
    ent_counts = {r["employee_id"]: r for r in db.q(
        "SELECT employee_id, COUNT(*) n, SUM(source != 'birthright') temp FROM entitlements "
        "WHERE revoked_at IS NULL GROUP BY employee_id")}
    return render(request, "people.html", user, rows=rows, q=q, dept=dept, status=status, depts=DEPARTMENTS,
                  ent_counts=ent_counts, employees=_emp_map())


def _can_view(user, emp):
    return ({"hr", "security"} & set(user["roles"])) or emp["id"] == user["employee_id"] \
        or emp["manager_id"] == user["employee_id"]


@app.get("/people/new", response_class=HTMLResponse)
def new_person(request: Request):
    user = require(request, "hr")
    return render(request, "person_new.html", user, depts=DEPARTMENTS, managers=_managers(), form={}, error=None)


def _managers():
    return db.q("SELECT * FROM employees WHERE status = 'active' ORDER BY first_name, last_name")


@app.post("/people/new")
def create_person(request: Request, first_name: str = Form(...), last_name: str = Form(...),
                  position: str = Form(...), manager_id: str = Form(""), start_date: str = Form(...),
                  csrf: str = Form("")):
    check_csrf(request, csrf)
    user = require(request, "hr")
    form = {"first_name": first_name, "last_name": last_name, "position": position, "manager_id": manager_id,
            "start_date": start_date}
    try:
        if position not in POSITIONS:
            raise jml.JMLError("Choose a position from the catalog.")
        if start_date < db.today():
            raise jml.JMLError("Start date can't be in the past.")
        emp_id = jml.create_employee(first_name, last_name, POSITIONS[position]["department"], position,
                                     manager_id or None, start_date, user["sam"])
    except Exception as exc:
        return render(request, "person_new.html", user, depts=DEPARTMENTS, managers=_managers(), form=form,
                      error=str(exc))
    emp = jml.employee(emp_id)
    if emp["status"] == "active":
        flash(request, "ok", f"{jml.full_name(emp)} joined as {POSITIONS[position]['title']}. "
                             f"Account {emp['sam']} was created with {len(POSITIONS[position]['groups'])} role groups.")
    else:
        flash(request, "ok", f"{jml.full_name(emp)} is scheduled to join on {fmt_date(start_date)}. "
                             "The account will be created that morning.")
    return back(f"/people/{emp_id}")


def timeline(emp_id):
    """Batang akses untuk komponen timeline: 90 hari ke belakang sampai 45 hari ke depan."""
    today = date.fromisoformat(db.today())
    start, end = today - timedelta(days=90), today + timedelta(days=45)
    span = (end - start).days
    since = start.isoformat()
    rows = db.q("SELECT * FROM entitlements WHERE employee_id = ? AND (revoked_at IS NULL OR revoked_at >= ?) "
                "ORDER BY revoked_at IS NOT NULL, source = 'birthright' DESC, group_name, granted_at", (emp_id, since))
    bars = []
    for r in rows:
        g0 = max(date.fromisoformat(r["granted_at"][:10]), start)
        if r["revoked_at"]:
            g1, state = date.fromisoformat(r["revoked_at"][:10]), "revoked"
        elif r["expires_at"]:
            g1, state = date.fromisoformat(r["expires_at"]) + timedelta(days=1), "temporary"
        else:
            g1, state = end, "standing"
        g1 = min(max(g1, g0 + timedelta(days=1)), end)
        bars.append({**r, "left": (g0 - start).days * 100 / span, "width": max((g1 - g0).days * 100 / span, 0.8),
                     "state": state, "open_end": state == "standing"})
    ticks = []
    d = date(start.year, start.month, 1)
    while d <= end:
        if d >= start:
            left = (d - start).days * 100 / span
            if abs(left - (today - start).days * 100 / span) > 6:  # jangan tumpang tindih dengan label Today
                ticks.append({"label": d.strftime("%b"), "left": left})
        d = date(d.year + (d.month // 12), d.month % 12 + 1, 1)
    return {"bars": bars, "ticks": ticks, "today": (today - start).days * 100 / span}


@app.get("/people/{emp_id}", response_class=HTMLResponse)
def person(request: Request, emp_id: str):
    user = require(request)
    emp = db.one("SELECT * FROM employees WHERE id = ?", (emp_id,))
    if not emp:
        raise HTTPException(404, "This person doesn't exist.")
    if not _can_view(user, emp):
        raise HTTPException(403, "You can only see yourself and the people who report to you.")
    reqs = db.q("SELECT * FROM access_requests WHERE beneficiary_id = ? ORDER BY created_at DESC LIMIT 15", (emp_id,))
    events = db.q("SELECT * FROM hr_events WHERE employee_id = ? ORDER BY id DESC", (emp_id,))
    _emps = db.q("SELECT * FROM employees")
    _by_sam, _by_id = {e["sam"]: e for e in _emps if e["sam"]}, {e["id"]: e for e in _emps}
    history = db.q("SELECT * FROM audit_log WHERE target IN (?, ?) ORDER BY id DESC LIMIT 25",
                   (emp["sam"] or "-", emp_id))
    reports = db.q("SELECT * FROM employees WHERE manager_id = ? AND status = 'active' ORDER BY first_name", (emp_id,))
    return render(request, "person.html", user, emp=emp, tl=timeline(emp_id), active=jml.active_entitlements(emp_id),
                  reqs=reqs, events=events, history=[describe(h, _by_sam, _by_id) for h in history], reports=reports, employees=_emp_map(),
                  managers=_managers())


@app.post("/people/{emp_id}/move")
def move_person(request: Request, emp_id: str, position: str = Form(...), manager_id: str = Form(""),
                effective_date: str = Form(...), csrf: str = Form("")):
    check_csrf(request, csrf)
    user = require(request, "hr")
    emp = jml.employee(emp_id)
    if emp["status"] != "active":
        flash(request, "error", "Only active employees can change position.")
    elif position not in POSITIONS:
        flash(request, "error", "Choose a position from the catalog.")
    elif position == emp["position"] and (manager_id or None) == emp["manager_id"]:
        flash(request, "error", "Nothing changed: pick a different position or manager.")
    else:
        try:
            jml.schedule(emp_id, "mover", max(effective_date, db.today()),
                         {"position": position, "manager_id": manager_id or None, "_from": emp["position"]},
                         user["sam"])
            done = effective_date <= db.today()
            flash(request, "ok", f"Move to {POSITIONS[position]['title']} "
                                 + ("applied. Old role access was removed." if done
                                    else f"scheduled for {fmt_date(effective_date)}."))
        except Exception as exc:
            flash(request, "error", f"Move failed: {exc}")
    return back(f"/people/{emp_id}")


@app.post("/people/{emp_id}/terminate")
def terminate_person(request: Request, emp_id: str, effective_date: str = Form(...), confirm: str = Form(""),
                     csrf: str = Form("")):
    check_csrf(request, csrf)
    user = require(request, "hr")
    emp = jml.employee(emp_id)
    if confirm.strip().lower() != (emp["sam"] or "").lower():
        flash(request, "error", f"Type {emp['sam']} to confirm the departure.")
    elif emp["id"] == user["employee_id"]:
        flash(request, "error", "You can't process your own departure.")
    else:
        try:
            jml.schedule(emp_id, "leaver", max(effective_date, db.today()), {}, user["sam"])
            flash(request, "ok", f"{jml.full_name(emp)}: account disabled and all access removed."
                  if effective_date <= db.today() else
                  f"Departure scheduled for {fmt_date(effective_date)}. Access stays until then.")
        except Exception as exc:
            flash(request, "error", f"Departure failed: {exc}")
    return back(f"/people/{emp_id}")


# ------------------------------------------------------------------ permintaan
@app.get("/requests/new", response_class=HTMLResponse)
def new_request(request: Request, kind: str = "group", for_: str = Query("", alias="for"), target: str = ""):
    user = require(request)
    beneficiaries = [user["emp"]] + db.q("SELECT * FROM employees WHERE manager_id = ? AND status = 'active' "
                                         "ORDER BY first_name", (user["employee_id"],))
    return render(request, "request_new.html", user, kind=kind, beneficiaries=beneficiaries, error=None,
                  form={"beneficiary_id": for_ or user["employee_id"], "target": target,
                        "start_date": db.today(),
                        "end_date": (date.fromisoformat(db.today()) + timedelta(days=6)).isoformat()},
                  requestable={g: m for g, m in GROUPS.items() if m.get("requestable")})


@app.post("/requests/new")
def create_request(request: Request, kind: str = Form(...), target: str = Form(""), beneficiary_id: str = Form(...),
                   justification: str = Form(""), start_date: str = Form(...), end_date: str = Form(...),
                   csrf: str = Form("")):
    check_csrf(request, csrf)
    user = require(request)
    allowed = {user["employee_id"]} | {e["id"] for e in db.q("SELECT id FROM employees WHERE manager_id = ?",
                                                                (user["employee_id"],))}
    form = {"beneficiary_id": beneficiary_id, "target": target, "justification": justification,
            "start_date": start_date, "end_date": end_date}
    try:
        if beneficiary_id not in allowed:
            raise rf.RequestError("You can request access for yourself or your direct reports.")
        if not target:
            raise rf.RequestError("Choose what you need access to.")
        rid = rf.create(kind, target, beneficiary_id, user["employee_id"], justification, start_date, end_date)
    except rf.RequestError as exc:
        beneficiaries = [user["emp"]] + db.q("SELECT * FROM employees WHERE manager_id = ? AND status = 'active' "
                                             "ORDER BY first_name", (user["employee_id"],))
        return render(request, "request_new.html", user, kind=kind, beneficiaries=beneficiaries, error=str(exc),
                      form=form, requestable={g: m for g, m in GROUPS.items() if m.get("requestable")})
    flash(request, "ok", f"Request #{rid} sent. You'll see the decision here.")
    return back(f"/requests/{rid}")


@app.get("/requests", response_class=HTMLResponse)
def list_requests(request: Request, scope: str = "mine"):
    user = require(request)
    if scope == "all" and "security" in user["roles"]:
        rows = db.q("SELECT * FROM access_requests ORDER BY created_at DESC LIMIT 200")
    elif scope == "team":
        rows = db.q("SELECT r.* FROM access_requests r JOIN employees e ON e.id = r.beneficiary_id "
                    "WHERE e.manager_id = ? ORDER BY r.created_at DESC", (user["employee_id"],))
    else:
        scope = "mine"
        rows = db.q("SELECT * FROM access_requests WHERE requested_by = ? OR beneficiary_id = ? "
                    "ORDER BY created_at DESC", (user["employee_id"], user["employee_id"]))
    return render(request, "requests.html", user, rows=rows, scope=scope, employees=_emp_map())


@app.get("/approvals", response_class=HTMLResponse)
def approvals(request: Request):
    user = require(request)
    return render(request, "approvals.html", user, rows=rf.inbox(user), employees=_emp_map())


@app.get("/requests/{rid}", response_class=HTMLResponse)
def show_request(request: Request, rid: int):
    user = require(request)
    try:
        req = rf.get(rid)
    except rf.RequestError:
        raise HTTPException(404, "This request doesn't exist.")
    emps = _emp_map()
    ben = emps[req["beneficiary_id"]]
    visible = ({"security"} & set(user["roles"])) or user["employee_id"] in (
        req["requested_by"], req["beneficiary_id"], req["manager_id"]) or ben["manager_id"] == user["employee_id"]
    if not visible:
        raise HTTPException(403, "You aren't involved in this request.")
    held = [e["group_name"] for e in jml.active_entitlements(ben["id"])]
    adds = []
    if req["kind"] == "acting":
        adds = [g for g in POSITIONS[req["target"]]["groups"] if g not in held]
    days = (date.fromisoformat(req["end_date"]) - date.fromisoformat(req["start_date"])).days + 1
    return render(request, "request.html", user, req=req, ben=ben, employees=emps, stage=rf.can_decide(req, user),
                  days=days,
                  adds=adds, held=held,
                  can_revoke=req["status"] == "active" and ("security" in user["roles"]
                                                           or req["manager_id"] == user["employee_id"]),
                  can_cancel=req["requested_by"] == user["employee_id"] and req["status"] in (
                      "pending_manager", "pending_security", "scheduled"))


@app.post("/requests/{rid}/decide")
def decide_request(request: Request, rid: int, decision: str = Form(...), comment: str = Form(""),
                   csrf: str = Form(""), next: str = Form("")):
    check_csrf(request, csrf)
    user = require(request)
    try:
        result = rf.decide(rid, user, decision == "approve", comment)
        msg = {"rejected": f"Request #{rid} rejected.",
               "pending_security": f"Request #{rid} approved. It now goes to IT Security because it's privileged.",
               "active": f"Request #{rid} approved and access granted in Active Directory.",
               "scheduled": f"Request #{rid} approved. Access starts on the requested date."}.get(result, "Saved.")
        flash(request, "ok", msg)
    except rf.RequestError as exc:
        flash(request, "error", str(exc))
    return back(next if next.startswith("/") else f"/requests/{rid}")


@app.post("/requests/{rid}/cancel")
def cancel_request(request: Request, rid: int, csrf: str = Form("")):
    check_csrf(request, csrf)
    user = require(request)
    try:
        rf.cancel(rid, user)
        flash(request, "ok", f"Request #{rid} cancelled.")
    except rf.RequestError as exc:
        flash(request, "error", str(exc))
    return back(f"/requests/{rid}")


@app.post("/requests/{rid}/revoke")
def revoke_request(request: Request, rid: int, csrf: str = Form("")):
    check_csrf(request, csrf)
    user = require(request)
    try:
        rf.revoke_now(rid, user)
        flash(request, "ok", f"Access from request #{rid} removed now.")
    except rf.RequestError as exc:
        flash(request, "error", str(exc))
    return back(f"/requests/{rid}")


# ------------------------------------------------------------------ certification
@app.get("/reviews", response_class=HTMLResponse)
def reviews(request: Request):
    user = require(request)
    items = cert.my_items(user)
    grouped = {}
    for i in items:
        grouped.setdefault(i["employee_id"], {"emp": i, "items": []})["items"].append(i)

    def risky(i):
        return i["source"] != "birthright" or is_privileged(i["group_name"])
    for g in grouped.values():
        g["items"].sort(key=lambda i: (bool(i["decision"]), not risky(i), i["group_name"]))
        g["risky"] = sum(1 for i in g["items"] if risky(i) and not i["decision"])
    ordered = dict(sorted(grouped.items(), key=lambda kv: (-kv[1]["risky"], kv[1]["emp"]["first_name"])))
    risky_open = sum(g["risky"] for g in grouped.values())
    return render(request, "reviews.html", user, grouped=ordered, total=len(items), risky_open=risky_open,
                  done=len([i for i in items if i["decision"]]))


@app.post("/reviews/{item_id}")
def decide_review(request: Request, item_id: int, decision: str = Form(...), comment: str = Form(""),
                  csrf: str = Form("")):
    check_csrf(request, csrf)
    user = require(request)
    try:
        cert.decide(item_id, user, decision, comment)
    except cert.CertError as exc:
        flash(request, "error", str(exc))
    return back("/reviews")


@app.post("/reviews/keep-role/{emp_id}")
def keep_role_access(request: Request, emp_id: str, csrf: str = Form("")):
    """Pertahankan semua akses bawaan jabatan untuk satu orang sekaligus."""
    check_csrf(request, csrf)
    user = require(request)
    n = 0
    for i in cert.my_items(user):
        if i["employee_id"] == emp_id and not i["decision"] and i["source"] == "birthright":
            cert.decide(i["id"], user, "keep", "Role access confirmed")
            n += 1
    flash(request, "ok", f"Kept {n} role groups.")
    return back("/reviews")


@app.get("/campaigns", response_class=HTMLResponse)
def campaigns(request: Request):
    user = require(request, "security")
    rows = db.q("SELECT * FROM campaigns ORDER BY created_at DESC")
    return render(request, "campaigns.html", user, rows=rows, stats={r["id"]: cert.stats(r["id"]) for r in rows},
                  default_due=(date.fromisoformat(db.today()) + timedelta(days=14)).isoformat())


@app.post("/campaigns")
def create_campaign(request: Request, name: str = Form(...), scope: str = Form("all"), due_date: str = Form(...),
                    csrf: str = Form("")):
    check_csrf(request, csrf)
    user = require(request, "security")
    try:
        cid = cert.create(name, scope, due_date, user)
        flash(request, "ok", f"Review started: {cert.stats(cid)['total']} access items sent to managers.")
        return back(f"/campaigns/{cid}")
    except cert.CertError as exc:
        flash(request, "error", str(exc))
        return back("/campaigns")


@app.get("/campaigns/{cid}", response_class=HTMLResponse)
def campaign(request: Request, cid: int):
    user = require(request, "security")
    c = db.one("SELECT * FROM campaigns WHERE id = ?", (cid,))
    if not c:
        raise HTTPException(404, "This review doesn't exist.")
    revoked = db.q("SELECT ri.*, e.first_name, e.last_name, e.sam, r.first_name rf, r.last_name rl "
                   "FROM review_items ri JOIN employees e ON e.id = ri.employee_id "
                   "JOIN employees r ON r.id = ri.reviewer_id WHERE ri.campaign_id = ? AND ri.decision = 'revoke' "
                   "ORDER BY ri.decided_at DESC", (cid,))
    return render(request, "campaign.html", user, c=c, s=cert.stats(cid), reviewers=cert.by_reviewer(cid),
                  revoked=revoked)


@app.post("/campaigns/{cid}/close")
def close_campaign(request: Request, cid: int, csrf: str = Form("")):
    check_csrf(request, csrf)
    user = require(request, "security")
    try:
        cert.close(cid, user)
        flash(request, "ok", "Review closed. Undecided items are recorded as not reviewed.")
    except cert.CertError as exc:
        flash(request, "error", str(exc))
    return back(f"/campaigns/{cid}")


@app.get("/campaigns/{cid}/report.csv")
def campaign_csv(request: Request, cid: int):
    require(request, "security")
    rows = db.q("SELECT e.id emp_id, e.sam, CONCAT(e.first_name, ' ', e.last_name) name, ri.group_name, ri.source, "
                "r.sam reviewer, COALESCE(ri.decision, 'not reviewed') decision, ri.comment, ri.decided_at "
                "FROM review_items ri JOIN employees e ON e.id = ri.employee_id JOIN employees r "
                "ON r.id = ri.reviewer_id WHERE ri.campaign_id = ? ORDER BY e.sam, ri.group_name", (cid,))
    return _csv(rows, f"access-review-{cid}.csv")


# ------------------------------------------------------------------ keamanan & audit
@app.get("/security", response_class=HTMLResponse)
def security_page(request: Request, show: str = "open"):
    user = require(request, "security")
    sql = "SELECT * FROM alerts" + (" WHERE status = 'open'" if show == "open" else "")
    alerts = db.q(sql + " ORDER BY status = 'resolved', CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 "
                        "WHEN 'medium' THEN 2 ELSE 3 END, ts DESC LIMIT 200")
    series = security.auth_series(24)
    peak = max([s["ok"] + s["bad"] for s in series] + [1])
    top_failed = db.q("SELECT account, COUNT(*) n FROM auth_events WHERE success = 0 AND ts >= ? "
                      "GROUP BY account ORDER BY n DESC LIMIT 6",
                      ((db.now() - timedelta(hours=24)).isoformat(),))
    return render(request, "security.html", user, alerts=alerts, show=show, series=series, peak=peak,
                  top_failed=top_failed, recon=db.kv_get("last_reconcile"),
                  last_run=db.kv_get("worker_last_run"))


@app.post("/alerts/{aid}/resolve")
def resolve_alert(request: Request, aid: int, action: str = Form("close"), note: str = Form(""),
                  csrf: str = Form("")):
    check_csrf(request, csrf)
    user = require(request, "security")
    try:
        security.resolve(aid, user, action, note)
        flash(request, "ok", "Alert resolved.")
    except Exception as exc:
        flash(request, "error", str(exc))
    return back("/security")


@app.get("/audit", response_class=HTMLResponse)
def audit_page(request: Request, q: str = "", action: str = ""):
    user = require(request, "security", "hr")
    sql, args = "SELECT * FROM audit_log WHERE 1=1", []
    if q:
        sql += " AND (lower(actor) LIKE ? OR lower(target) LIKE ? OR lower(details) LIKE ?)"
        args += [f"%{q.lower()}%"] * 3
    if action:
        sql += " AND action = ?"
        args.append(action)
    rows = db.q(sql + " ORDER BY id DESC LIMIT 300", args)
    actions = [r["action"] for r in db.q("SELECT DISTINCT action FROM audit_log ORDER BY action")]
    return render(request, "audit.html", user, rows=rows, q=q, action=action, actions=actions)


@app.get("/audit.csv")
def audit_csv(request: Request):
    require(request, "security", "hr")
    return _csv(db.q("SELECT * FROM audit_log ORDER BY id"), "audit-log.csv")


@app.get("/catalog", response_class=HTMLResponse)
def catalog(request: Request):
    user = require(request)
    holders = {r["group_name"]: r["n"] for r in db.q("SELECT group_name, COUNT(DISTINCT employee_id) n FROM "
                                                     "entitlements WHERE revoked_at IS NULL GROUP BY group_name")}
    return render(request, "catalog.html", user, holders=holders)


def _csv(rows, filename):
    buf = io.StringIO()
    if rows:
        w = csv.DictWriter(buf, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f"attachment; filename={filename}"})


# ------------------------------------------------------------------ metrics & health
@app.get("/metrics", response_class=PlainTextResponse)
def metrics():
    lines = []

    def g(name, help_, value, labels=""):
        lines.append(f"# HELP {name} {help_}")
        lines.append(f"# TYPE {name} gauge")
        lines.append(f"{name}{labels} {value}")

    g("iga_employees_active", "Karyawan aktif", db.one("SELECT COUNT(*) n FROM employees WHERE status='active'")["n"])
    g("iga_requests_pending", "Permintaan menunggu keputusan",
      db.one("SELECT COUNT(*) n FROM access_requests WHERE status IN ('pending_manager','pending_security')")["n"])
    g("iga_temporary_access_active", "Akses sementara yang aktif",
      db.one("SELECT COUNT(*) n FROM entitlements WHERE revoked_at IS NULL AND expires_at IS NOT NULL")["n"])
    lines += ["# HELP iga_alerts_open Alert keamanan yang belum ditindaklanjuti", "# TYPE iga_alerts_open gauge"]
    for row in db.q("SELECT severity, COUNT(*) n FROM alerts WHERE status='open' GROUP BY severity"):
        lines.append(f'iga_alerts_open{{severity="{row["severity"]}"}} {row["n"]}')
    c = db.one("SELECT id FROM campaigns WHERE status='open' ORDER BY created_at DESC LIMIT 1")
    if c:
        g("iga_campaign_completion_percent", "Progres access review yang berjalan", cert.stats(c["id"])["pct"])
    return "\n".join(lines) + "\n"


@app.get("/healthz", response_class=PlainTextResponse)
def healthz():
    return "ok"
