"""Akses ke Active Directory lewat LDAPS.

Semua penulisan memakai akun layanan svc-iga yang hak tulisnya dibatasi ke OU=Corp.
Group Tier-0 (Domain Admins, dst.) berada di luar jangkauan tulis dan hanya dibaca untuk pemantauan."""
import ssl
import threading
import time

import ldap3
from ldap3.utils.conv import escape_filter_chars

from .config import (AD_CA_FILE, AD_HOST, AD_REALM, BASE_DN, CORP_OU, DEFAULT_PASSWORD, DISABLED_OU,
                     FORCE_PASSWORD_CHANGE, GROUPS, GROUPS_OU, IGA_SERVICE_PASSWORD, IGA_SERVICE_USER, USERS_OU)

UAC_NORMAL = 0x0200
UAC_DISABLED = 0x0002
_lock = threading.RLock()
_conn = None


class ADError(Exception):
    pass


def _server():
    tls = ldap3.Tls(validate=ssl.CERT_REQUIRED, ca_certs_file=AD_CA_FILE)
    return ldap3.Server(AD_HOST, port=636, use_ssl=True, tls=tls, connect_timeout=5)


def upn(sam):
    return f"{sam}@{AD_REALM}"


def authenticate(sam, password):
    """Login portal: bind sebagai user. Gagal -> False (tidak membedakan alasan, agar tidak bocor info)."""
    if not sam or not password:
        return False
    try:
        c = ldap3.Connection(_server(), user=upn(sam), password=password, receive_timeout=10)
        ok = c.bind()
        c.unbind()
        return ok
    except ldap3.core.exceptions.LDAPException:
        return False


def conn():
    global _conn
    with _lock:
        if _conn is None or not _conn.bound or _conn.closed:
            _conn = ldap3.Connection(_server(), user=upn(IGA_SERVICE_USER), password=IGA_SERVICE_PASSWORD,
                                     auto_bind=True, receive_timeout=30, raise_exceptions=False)
        return _conn


def wait_until_ready(retries=60, delay=5):
    last = None
    for _ in range(retries):
        try:
            conn()
            return
        except Exception as exc:  # DC atau sertifikat belum siap
            last = exc
            time.sleep(delay)
    raise ADError(f"Can't connect to {AD_HOST}: {last}")


def _check(c, ok, what):
    if not ok and c.result.get("result") not in (68,):  # 68 = entryAlreadyExists
        raise ADError(f"{what}: {c.result.get('description')} {c.result.get('message', '')}".strip())


def group_dn(name):
    return f"CN={name},{GROUPS_OU}"


def dept_ou(department):
    return f"OU={department},{USERS_OU}"


# ------------------------------------------------------------------ struktur
def ensure_structure(departments):
    with _lock:
        c = conn()
        for ou in [USERS_OU, GROUPS_OU, DISABLED_OU] + [dept_ou(d) for d in departments]:
            _check(c, c.add(ou, ["top", "organizationalUnit"]), f"buat {ou}")
        for name, meta in GROUPS.items():
            _check(c, c.add(group_dn(name), ["top", "group"],
                            {"sAMAccountName": name, "description": meta.get("description", ""),
                             "groupType": "-2147483646"}), f"buat group {name}")


# ------------------------------------------------------------------ user
def find_user(sam, attributes=None):
    with _lock:
        c = conn()
        c.search(BASE_DN, f"(&(objectCategory=person)(sAMAccountName={escape_filter_chars(sam)}))",
                 attributes=attributes or ["distinguishedName", "userAccountControl", "memberOf", "title",
                                           "department", "description", "manager"])
        return c.entries[0] if c.entries else None


def sam_exists(sam):
    return find_user(sam, ["sAMAccountName"]) is not None


def create_user(emp, title, manager_dn=None):
    dn = f"CN={emp['first_name']} {emp['last_name']} ({emp['sam']}),{dept_ou(emp['department'])}"
    attrs = {
        "sAMAccountName": emp["sam"], "userPrincipalName": upn(emp["sam"]),
        "givenName": emp["first_name"], "sn": emp["last_name"],
        "displayName": f"{emp['first_name']} {emp['last_name']}",
        "department": emp["department"], "title": title, "employeeID": emp["id"],
        "employeeType": "Employee",
        "unicodePwd": f'"{DEFAULT_PASSWORD}"'.encode("utf-16-le"),
        "userAccountControl": str(UAC_NORMAL),
    }
    if FORCE_PASSWORD_CHANGE:
        attrs["pwdLastSet"] = "0"  # wajib ganti password saat login pertama
    if manager_dn:
        attrs["manager"] = manager_dn
    with _lock:
        c = conn()
        ok = c.add(dn, ["top", "person", "organizationalPerson", "user"], attrs)
        if not ok:
            raise ADError(f"create user {emp['sam']}: {c.result.get('description')} {c.result.get('message')}")
    return dn


def update_user(dn, changes):
    """changes: {atribut: nilai atau None untuk menghapus}"""
    mods = {}
    for attr, value in changes.items():
        mods[attr] = [(ldap3.MODIFY_REPLACE, [value] if value not in (None, "") else [])]
    with _lock:
        c = conn()
        _check(c, c.modify(dn, mods), f"ubah {dn}")


def move_user(dn, new_parent):
    rdn = dn.split(",", 1)[0]
    if dn.split(",", 1)[1].lower() == new_parent.lower():
        return dn
    with _lock:
        c = conn()
        _check(c, c.modify_dn(dn, rdn, new_superior=new_parent), f"pindah {dn}")
    return f"{rdn},{new_parent}"


def set_enabled(dn, enabled):
    with _lock:
        c = conn()
        c.search(dn, "(objectClass=*)", search_scope=ldap3.BASE, attributes=["userAccountControl"])
        uac = int(c.entries[0].userAccountControl.value)
        uac = (uac & ~UAC_DISABLED) if enabled else (uac | UAC_DISABLED)
        _check(c, c.modify(dn, {"userAccountControl": [(ldap3.MODIFY_REPLACE, [str(uac)])]}), f"status {dn}")


def add_member(group, user_dn):
    with _lock:
        c = conn()
        ok = c.modify(group_dn(group), {"member": [(ldap3.MODIFY_ADD, [user_dn])]})
        if not ok and c.result.get("result") != 68:  # sudah anggota
            raise ADError(f"add {user_dn} to {group}: {c.result.get('description')}")


def remove_member(group, user_dn):
    with _lock:
        c = conn()
        ok = c.modify(group_dn(group), {"member": [(ldap3.MODIFY_DELETE, [user_dn])]})
        if not ok and c.result.get("result") not in (16, 53):  # bukan anggota
            raise ADError(f"remove {user_dn} from {group}: {c.result.get('description')}")


# ------------------------------------------------------------------ baca untuk rekonsiliasi
def managed_memberships():
    """{group: set(user_dn lowercase)} untuk group yang dikelola IGA."""
    out = {}
    with _lock:
        c = conn()
        c.search(GROUPS_OU, "(objectClass=group)", attributes=["sAMAccountName", "member"])
        for e in c.entries:
            out[str(e.sAMAccountName.value)] = {m.lower() for m in e.member.values}
    return out


def group_members_by_name(name):
    with _lock:
        c = conn()
        c.search(BASE_DN, f"(&(objectClass=group)(sAMAccountName={escape_filter_chars(name)}))",
                 attributes=["member"])
        if not c.entries:
            return set()
        return {m.lower() for m in c.entries[0].member.values}


def corp_users():
    """{dn lowercase: {sam, enabled}} untuk semua user di OU=Corp."""
    out = {}
    with _lock:
        c = conn()
        c.search(CORP_OU, "(&(objectCategory=person)(objectClass=user))",
                 attributes=["sAMAccountName", "userAccountControl"], paged_size=1000)
        for e in c.entries:
            out[e.entry_dn.lower()] = {"sam": str(e.sAMAccountName.value),
                                       "enabled": not int(e.userAccountControl.value) & UAC_DISABLED}
    return out


def sam_for_dn(dn):
    with _lock:
        c = conn()
        c.search(dn, "(objectClass=*)", search_scope=ldap3.BASE, attributes=["sAMAccountName"])
        return str(c.entries[0].sAMAccountName.value) if c.entries else dn.split(",")[0][3:]
