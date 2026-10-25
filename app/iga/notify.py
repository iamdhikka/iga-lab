"""Notifikasi alert ke Telegram (opsional). Aktif jika TELEGRAM_BOT_TOKEN dan TELEGRAM_CHAT_ID diisi."""
import json
import os
import urllib.parse
import urllib.request

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
MIN_SEVERITY = os.environ.get("TELEGRAM_MIN_SEVERITY", "high")
ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
LABELS = {
    "brute_force": "Brute force", "password_spray": "Password spraying", "disabled_login": "Login ke akun nonaktif",
    "off_hours": "Login di luar jam kerja", "out_of_band_grant": "Akses diberikan di luar IGA",
    "tier0_change": "Perubahan group Tier-0", "leaver_enabled": "Akun mantan karyawan aktif kembali",
}


def alert(kind, severity, subject, details):
    if not TOKEN or not CHAT_ID or ORDER.get(severity, 9) > ORDER.get(MIN_SEVERITY, 1):
        return
    icon = {"critical": "🔴", "high": "🟠"}.get(severity, "🟡")
    text = (f"{icon} <b>{LABELS.get(kind, kind)}</b> ({severity})\n"
            f"Subjek: <code>{subject}</code>\n"
            f"<pre>{json.dumps(details, ensure_ascii=False, indent=1)[:900]}</pre>")
    data = urllib.parse.urlencode({"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}).encode()
    try:
        urllib.request.urlopen(f"https://api.telegram.org/bot{TOKEN}/sendMessage", data=data, timeout=10)
    except Exception as exc:  # notifikasi tidak boleh menghentikan worker
        print(f"[notify] gagal mengirim Telegram: {exc}", flush=True)
