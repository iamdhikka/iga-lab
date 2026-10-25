"""Pekerjaan latar belakang: dijalankan berkala oleh aplikasi."""
import threading
import time
import traceback

from . import certification, db, jml, requests_flow, security
from .config import WORKER_INTERVAL

_stop = threading.Event()


def cycle():
    steps = [
        ("hr_events", jml.process_due_events),
        ("activate_requests", requests_flow.activate_due),
        ("expire_requests", requests_flow.expire_due),
        ("expire_exceptions", security.expire_exceptions),
        ("close_campaigns", certification.close_overdue),
        ("ingest_audit", security.ingest),
        ("detect", security.detect),
        ("reconcile", security.reconcile),
    ]
    for name, fn in steps:
        try:
            fn()
        except Exception as exc:
            print(f"[worker] {name} gagal: {exc}", flush=True)
            traceback.print_exc()
    db.kv_set("worker_last_run", db.now_iso())


def _loop():
    while not _stop.is_set():
        cycle()
        _stop.wait(WORKER_INTERVAL)


def start():
    t = threading.Thread(target=_loop, name="iga-worker", daemon=True)
    t.start()
    return t


def stop():
    _stop.set()


if __name__ == "__main__":
    while True:
        cycle()
        time.sleep(WORKER_INTERVAL)
