"""История проверок (форма 5): карточки запусков из папки STORE и полный результат запуска.

Каждый запуск лежит в STORE/<run_id>: исходные файлы, <base>.report.json, файлы с комментариями и правками, run.json
с паспортом запуска (дата, пользователь, длительность, группы проверок). Старые запуски без run.json тоже попадают
в историю: дата берётся по времени отчёта, пользователь пустой, группы проверок все три."""
import json
import os
import re
import time
from collections import Counter
from types import SimpleNamespace

from . import feedback
from .norms import CHECK_GROUPS, group_of

META = "run.json"
REQ = "req_compare.json"
RID_RE = re.compile(r"^[0-9a-f]{8,32}$")


def write_meta(folder: str, meta: dict) -> dict:
    path = os.path.join(folder, META)
    old = read_meta(folder) or {}
    old.update(meta)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(old, fh, ensure_ascii=False, indent=1)
    return old


def read_meta(folder: str) -> dict | None:
    try:
        with open(os.path.join(folder, META), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def report_path(folder: str) -> str | None:
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return None
    meta = read_meta(folder) or {}
    if meta.get("report") and meta["report"] in names:
        return os.path.join(folder, meta["report"])
    return next((os.path.join(folder, n) for n in names if n.endswith(".report.json")), None)


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts))


def _decisions(rows: list[dict]) -> dict:
    """Последнее решение закупщика по каждому замечанию запуска."""
    last = {}
    for r in rows:
        last[r.get("finding_id")] = r.get("decision")
    c = Counter(last.values())
    return {"принято": c.get("accept", 0), "отклонено": c.get("reject", 0), "ручная проверка": c.get("review", 0), "всего решений": len(last)}


def _verdict_code(main: list[dict], full: bool) -> str:
    from .checks import verdict_code
    return verdict_code([SimpleNamespace(**{k: f.get(k) for k in ("severity", "status")}) for f in main],
                        list(CHECK_GROUPS) if full else ["logic"])


def _verdict_text(res: dict, main: list[dict]) -> str:
    if res.get("verdict") is not None:
        return res["verdict"]
    from .pipeline import _verdict
    return _verdict([SimpleNamespace(id=f.get("id"), severity=f.get("severity"), status=f.get("status"), needs_review=f.get("needs_review"))
                     for f in main])


def links(rid: str, folder: str, res: dict | None = None) -> dict:
    names = set(os.listdir(folder)) if os.path.isdir(folder) else set()
    out = {}
    rp = report_path(folder)
    if rp:
        base = os.path.basename(rp)[:-len(".report.json")]
        out["report_json"] = f"/runs/{rid}/{base}.report.json"
        if f"{base}.report.html" in names:
            out["report_html"] = f"/runs/{rid}/{base}.report.html"
        if f"{base}.comments.docx" in names:
            out["comments_docx"] = f"/runs/{rid}/{base}.comments.docx"
        if f"{base}.revisions.docx" in names:
            out["revisions_docx"] = f"/runs/{rid}/{base}.revisions.docx"
    for n in sorted(names):
        m = re.fullmatch(r"attachment_(\d+)\.(comments|revisions)\.docx", n)
        if m:
            out[f"attachment_{m.group(1)}_{m.group(2)}_docx"] = f"/runs/{rid}/{n}"
    if REQ in names:
        out["requirements_compare_json"] = f"/runs/{rid}/{REQ}"
    out["result"] = f"/api/runs/{rid}"
    return out


def card(store: str, rid: str, fb_rows: list[dict] | None = None) -> dict | None:
    folder = os.path.join(store, rid)
    rp = report_path(folder)
    if rp is None:
        return None
    try:
        with open(rp, encoding="utf-8") as fh:
            res = json.load(fh)
    except (OSError, ValueError):
        return None
    meta = read_meta(folder) or {}
    findings = res.get("findings", [])
    main = [f for f in findings if f.get("section", "main") == "main"]
    full = res.get("full_check", True)
    groups = res.get("check_groups") or [CHECK_GROUPS[k] for k in (meta.get("checks") or CHECK_GROUPS)]
    by_group = Counter(f.get("group") or group_of(f.get("category", "")) for f in main)
    created = meta.get("created_at") or _iso(os.path.getmtime(rp))
    if fb_rows is None:
        fb_rows = [r for r in feedback.load() if r.get("run_id") == rid]
    return {
        "run_id": rid, "kind": meta.get("kind") or ("requirements_compare" if os.path.exists(os.path.join(folder, REQ)) else "check"),
        "created_at": created, "finished_at": meta.get("finished_at", ""), "user": meta.get("user", ""),
        "seconds": meta.get("seconds", res.get("seconds")), "files": meta.get("files") or (res.get("document") or {}).get("files") or [res.get("file", "")],
        "previous_file": meta.get("previous_file", ""),
        "checks": meta.get("checks") or res.get("checks") or list(CHECK_GROUPS), "check_groups": groups, "full_check": full,
        "mode": res.get("mode", ""), "model": res.get("model", ""), "orchestrator": res.get("orchestrator", ""),
        "counts": {"total": len(main), "by_group": {g: by_group.get(g, 0) for g in groups},
                   "by_severity": dict(Counter(f.get("severity") for f in main)),
                   "high": sum(1 for f in main if f.get("severity") == "high"),
                   "errors": sum(1 for f in main if f.get("status") == "ошибка"),
                   "model_block": sum(1 for f in findings if f.get("section") == "llm_review"),
                   "unchecked": sum(1 for f in findings if f.get("section") == "unchecked"),
                   "revisions": sum(1 for f in main if f.get("replacement"))},
        "verdict": _verdict_text(res, main), "verdict_code": res.get("verdict_code") or _verdict_code(main, full),
        "decisions": _decisions(fb_rows), "links": links(rid, folder, res),
    }


def list_runs(store: str, user: str | None = None, limit: int = 50, offset: int = 0) -> dict:
    """Карточки по времени, новые сверху; number = порядковый номер запуска от самого раннего."""
    try:
        rids = [d for d in os.listdir(store) if RID_RE.match(d) and os.path.isdir(os.path.join(store, d))]
    except OSError:
        rids = []
    fb = {}
    for r in feedback.load():
        fb.setdefault(r.get("run_id"), []).append(r)
    cards = [c for c in (card(store, rid, fb.get(rid, [])) for rid in rids) if c]
    cards.sort(key=lambda c: (c["created_at"], c["run_id"]))
    for i, c in enumerate(cards, 1):
        c["number"] = i
    if user:
        cards = [c for c in cards if c["user"] == user]
    cards.reverse()
    return {"total": len(cards), "items": cards[offset:offset + limit]}


def load_trace(store: str, rid: str) -> dict | None:
    """Журнал вызовов модели запуска (trace.json): те же правила, что у load_run."""
    if not RID_RE.match(rid or ""):
        return None
    folder = os.path.join(store, rid)
    if report_path(folder) is None:
        return None
    path = os.path.join(folder, "trace.json")
    if not os.path.exists(path):
        return {"calls": [], "rejected": [], "dropped": []}
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def load_run(store: str, rid: str) -> dict | None:
    """Полный результат запуска (как у /api/check) из report.json плюс карточка и, для сопоставления, сводка требований."""
    if not RID_RE.match(rid or ""):
        return None
    folder = os.path.join(store, rid)
    rp = report_path(folder)
    if rp is None:
        return None
    with open(rp, encoding="utf-8") as fh:
        res = json.load(fh)
    res["run_id"] = rid
    res["run"] = next((c for c in list_runs(store, limit=10 ** 6)["items"] if c["run_id"] == rid), None) or card(store, rid)
    res["links"] = links(rid, folder, res)
    fb_rows = [r for r in feedback.load() if r.get("run_id") == rid]
    last = {}
    for r in fb_rows:
        last[r.get("finding_id")] = {"decision": r.get("decision"), "comment": r.get("comment", ""), "ts": r.get("ts")}
    res["decisions"] = last
    req = os.path.join(folder, REQ)
    if os.path.exists(req):
        with open(req, encoding="utf-8") as fh:
            res["requirements_compare"] = json.load(fh)
    return res
