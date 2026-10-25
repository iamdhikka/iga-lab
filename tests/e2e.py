"""Uji end-to-end lewat browser, diverifikasi langsung ke Active Directory.

Jalankan terhadap lab yang baru dibuat (data demo standar):
    pip install playwright ldap3 && playwright install chromium
    docker compose cp dc1:/tls/ca.crt ./ca.crt
    AD_ADMIN_PASSWORD=... python tests/e2e.py ./ca.crt ./e2e-screenshots
Butuh port LDAPS 636 dari container dc1 yang dibuka ke localhost (lihat README).
"""
import os
import re
import ssl
import sys

import ldap3
from playwright.sync_api import sync_playwright

BASE = os.environ.get("IGA_URL", "http://127.0.0.1:8080")
PW = os.environ.get("IGA_INITIAL_PASSWORD", "Welc0me!Lab-2026")
AD_HOST = os.environ.get("AD_HOST", "localhost")
AD_PORT = int(os.environ.get("AD_PORT", "6360"))
CA = sys.argv[1]
OUT = sys.argv[2]
results = []


def check(name, cond, extra=""):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra else ""), flush=True)


def ad_groups(sam):
    tls = ldap3.Tls(validate=ssl.CERT_REQUIRED, ca_certs_file=CA)
    c = ldap3.Connection(ldap3.Server(AD_HOST, port=AD_PORT, use_ssl=True, tls=tls),
                         user="Administrator@corp.lab", password=os.environ["AD_ADMIN_PASSWORD"], auto_bind=True)
    c.search("DC=corp,DC=lab", f"(sAMAccountName={sam})", attributes=["memberOf", "userAccountControl", "title"])
    e = c.entries[0]
    return {g.split(",")[0][3:] for g in e.memberOf.values}, int(e.userAccountControl.value), str(e.title.value)


def login(b, user):
    ctx = b.new_context(viewport={"width": 1440, "height": 1000})
    pg = ctx.new_page()
    pg.goto(BASE + "/login")
    pg.fill("#u", user)
    pg.fill("#p", PW)
    pg.click("button:has-text('Sign in')")
    pg.wait_for_url(BASE + "/")
    return pg


def flash(pg):
    el = pg.query_selector(".flash")
    return el.inner_text() if el else ""


with sync_playwright() as p:
    b = p.chromium.launch()

    # 0. login salah
    pg = b.new_page()
    pg.goto(BASE + "/login"); pg.fill("#u", "joko.pratama"); pg.fill("#p", "salah"); pg.click("button:has-text('Sign in')")
    check("wrong password rejected", "incorrect" in flash(pg))

    joko = login(b, "joko.pratama")
    # 1. validasi: melebihi batas hari
    joko.goto(BASE + "/requests/new?target=Server-Admins")
    joko.select_option("#tgt", "Server-Admins")
    joko.fill("#ed", "2026-12-31")
    joko.fill("#why", "Deploy hotfix ERP untuk masalah posting jurnal")
    joko.click("button:has-text('Send request')")
    check("max duration enforced", "at most 7 days" in flash(joko), flash(joko))
    # 2. permintaan privileged yang valid
    joko.select_option("#tgt", "Server-Admins")
    joko.fill("#ed", "")
    joko.fill("#ed", __import__("datetime").date.today().isoformat())
    joko.fill("#why", "Deploy hotfix ERP untuk masalah posting jurnal")
    joko.click("button:has-text('Send request')")
    m = re.search(r"Request #(\d+) sent", flash(joko))
    check("privileged request created", m, flash(joko))
    rid = m.group(1)
    # 3. pemisahan akses halaman
    r = joko.goto(BASE + "/security")
    check("employee blocked from security page", r.status == 403)

    fajar = login(b, "fajar.nugroho")
    fajar.goto(BASE + "/approvals")
    form = fajar.locator(f"form[action='/requests/{rid}/decide']")
    form.locator("button[value=approve]").click()
    check("manager approval routes to IT Security", "goes to IT Security" in flash(fajar), flash(fajar))
    check("not in AD before security approval", "Server-Admins" not in ad_groups("joko.pratama")[0])

    sari = login(b, "sari.hakim")
    sari.goto(BASE + f"/requests/{rid}")
    sari.fill("#c", "Dibatasi sesuai jendela deploy")
    sari.click("button[value=approve]")
    check("security approval grants access", "granted in Active Directory" in flash(sari), flash(sari))
    check("joko in Server-Admins in AD", "Server-Admins" in ad_groups("joko.pratama")[0])
    sari.screenshot(path=f"{OUT}/request_done.png", full_page=True)

    # 4. joiner lewat form HR
    hana = login(b, "hana.wijaya")
    hana.goto(BASE + "/people/new")
    hana.fill("#fn", "Larasati"); hana.fill("#ln", "Anggraini")
    hana.select_option("#pos", "sales-exec")
    hana.select_option("#mgr", label=hana.locator("#mgr option", has_text="Rina Lestari").inner_text())
    hana.click("button:has-text('Add joiner')")
    check("joiner created", "joined as Sales Executive" in flash(hana), flash(hana))
    groups, uac, title = ad_groups("larasati.anggraini")
    check("joiner AD groups match position", groups == {"GRP-Sales", "VPN-Users", "APP-CRM"} and uac & 2 == 0, groups)
    new_url = hana.url

    # 5. mover
    hana.select_option("#mv-pos", "fin-staff")
    hana.click("button:has-text('Apply move')")
    groups, uac, title = ad_groups("larasati.anggraini")
    check("mover swaps access", groups == {"GRP-Finance", "VPN-Users", "APP-ERP-User", "SHARE-Finance"}
          and title == "Finance Staff", (groups, title))
    # 6. leaver dengan konfirmasi salah lalu benar
    hana.fill("#lv-confirm", "salah")
    hana.click("button:has-text('Process departure')")
    check("leaver needs typed confirmation", "to confirm" in flash(hana))
    hana.fill("#lv-confirm", "larasati.anggraini")
    hana.click("button:has-text('Process departure')")
    groups, uac, title = ad_groups("larasati.anggraini")
    check("leaver disabled with no groups", uac & 2 and not groups, (uac, groups))
    hana.goto(new_url); hana.screenshot(path=f"{OUT}/person_left.png", full_page=True)

    # 7. access review: cabut Server-Admins joko
    fajar.goto(BASE + "/reviews")
    has_item = fajar.locator("text=Server-Admins").count()
    check("new grant not in existing campaign (snapshot)", has_item == 0)
    fajar.locator("form[action^='/reviews/keep-role/']").first.locator("button").click()
    check("bulk keep role groups", "Kept" in flash(fajar), flash(fajar))

    # 8. kampanye baru (privileged only) lalu manager mencabut
    sari.goto(BASE + "/campaigns")
    sari.fill("#n", "Privileged access check")
    sari.select_option("#s", "privileged")
    sari.click("button:has-text('Start campaign')")
    check("privileged campaign started", "access items sent" in flash(sari), flash(sari))
    fajar.goto(BASE + "/reviews")
    row = fajar.locator("tr", has_text="Server-Admins").first
    row.locator("button[value=revoke]").click()
    check("review removal hits AD", "Server-Admins" not in ad_groups("joko.pratama")[0])
    fajar.screenshot(path=f"{OUT}/reviews.png")

    # 9. selesaikan alert out-of-band dengan menghapus akses dari AD
    sari.goto(BASE + "/security")
    li = sari.locator("li", has_text="Access granted outside the portal").first
    subject = li.locator("div").first.inner_text().split(":")[1].strip().split()[0]
    li.locator("summary").click()
    li.locator("select").select_option("remove")
    li.locator("input[name=note]").fill("Tidak ada tiket; dikonfirmasi dengan atasan")
    li.locator("button:has-text('Resolve')").click()
    check("out-of-band access removed from AD", "Server-Admins" not in ad_groups(subject)[0], subject)
    sari.goto(BASE + "/audit?q=" + subject)
    check("audit shows removal", "out of band removed" in sari.content())

    # 10. kontrol akses tambahan
    r = joko.goto(BASE + "/people/E1001")  # Hana, bukan atasan/bawahan joko
    check("employee can't open unrelated person", r.status == 403)
    other = sari.evaluate("""async () => (await fetch('/requests/new', {method:'POST',
        headers:{'Content-Type':'application/x-www-form-urlencoded'},
        body:'kind=group&target=VPN-Users&beneficiary_id=E1007&justification=xxxxxxxxxxxxxxxxxxxx&start_date=2030-01-01&end_date=2030-01-02&csrf=palsu'})).status""")
    check("forged form without CSRF token rejected", other == 400, other)
    r = joko.goto(BASE + "/campaigns")
    check("employee can't open campaigns", r.status == 403)
    r = joko.goto(BASE + "/dashboard")
    check("employee can't open dashboard", r.status == 403)
    r = sari.goto(BASE + "/dashboard?days=90")
    sari.wait_for_load_state("networkidle")
    drawn = sari.evaluate("() => [...document.querySelectorAll('canvas')].every(c => c.width > 0 && Chart.getChart(c))")
    check("dashboard renders with charts", r.status == 200 and "Open tickets by SLA" in sari.content() and drawn)
    r = fajar.goto(BASE + "/dashboard")
    check("manager dashboard is scoped to the team", r.status == 200 and "You and your direct reports" in fajar.content())
    r = joko.goto(BASE + "/metrics")
    check("metrics endpoint", r.status == 200 and "iga_employees_active" in joko.content())
    b.close()

failed = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed", "FAILED: " + ", ".join(failed) if failed else "")
sys.exit(1 if failed else 0)
