import os
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

BASE = Path(__file__).resolve().parent.parent

AD_HOST = os.environ.get("AD_HOST", "dc1.corp.lab")
AD_REALM = os.environ.get("AD_REALM", "CORP.LAB").lower()
BASE_DN = ",".join(f"DC={p}" for p in AD_REALM.split("."))
AD_CA_FILE = os.environ.get("AD_CA_FILE", "/tls/ca.crt")
IGA_SERVICE_USER = os.environ.get("IGA_SERVICE_USER", "svc-iga")
IGA_SERVICE_PASSWORD = os.environ.get("IGA_SERVICE_PASSWORD", "")

MYSQL = {
    "host": os.environ.get("MYSQL_HOST", "mysql"),
    "port": int(os.environ.get("MYSQL_PORT", "3306")),
    "database": os.environ.get("MYSQL_DATABASE", "iga"),
    "user": os.environ.get("MYSQL_USER", "iga_app"),
    "password": os.environ.get("MYSQL_PASSWORD", ""),
}
AUDIT_DIR = os.environ.get("AUDIT_DIR", "/audit")
SECRET_KEY = os.environ.get("IGA_SECRET_KEY", "dev-only-change-me")
TZ = ZoneInfo(os.environ.get("IGA_TIMEZONE", "Asia/Jakarta"))
WORKER_INTERVAL = int(os.environ.get("IGA_WORKER_INTERVAL", "30"))
DEFAULT_PASSWORD = os.environ.get("IGA_INITIAL_PASSWORD", "Welc0me!Lab-2026")
# Produksi: true (user wajib ganti password saat login pertama). Lab demo: false agar persona bisa langsung login.
FORCE_PASSWORD_CHANGE = os.environ.get("IGA_FORCE_PASSWORD_CHANGE", "true").lower() == "true"
OFF_HOURS = (int(os.environ.get("IGA_OFF_HOURS_START", "22")), int(os.environ.get("IGA_OFF_HOURS_END", "5")))

CORP_OU = f"OU=Corp,{BASE_DN}"
USERS_OU = f"OU=Users,{CORP_OU}"
GROUPS_OU = f"OU=Groups,{CORP_OU}"
DISABLED_OU = f"OU=Disabled Users,{CORP_OU}"

with open(os.environ.get("IGA_CATALOG", BASE / "catalog.yaml"), encoding="utf-8") as fh:
    CATALOG = yaml.safe_load(fh)

GROUPS = CATALOG["groups"]
POSITIONS = CATALOG["positions"]
DEPARTMENTS = CATALOG["departments"]
TIER0 = CATALOG["tier0"]
ACTING_MAX_DAYS = CATALOG.get("acting_max_days", 90)


def is_privileged(group):
    return bool(GROUPS.get(group, {}).get("privileged"))


def position_privileged(position_key):
    return any(is_privileged(g) for g in POSITIONS[position_key]["groups"])
