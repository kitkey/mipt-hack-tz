import json
import os
import re

PATH = os.environ.get("TZCHECK_CUSTOMERS", os.path.join(os.path.dirname(__file__), "data", "customers.json"))


def load(path: str = PATH) -> list[dict]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except OSError:
        return []
    return [dict(p, _rx=[re.compile(m, re.I) for m in p.get("markers", [])]) for p in data.get("profiles", [])]


PROFILES = load()


def detect(text: str) -> dict | None:
    forced = os.environ.get("TZCHECK_CUSTOMER", "").strip().lower()
    if forced in ("", "none", "off", "0"):
        return None
    if forced != "auto":
        return next((p for p in PROFILES if p["id"] == forced), None)
    return next((p for p in PROFILES if any(rx.search(text) for rx in p["_rx"])), None)
