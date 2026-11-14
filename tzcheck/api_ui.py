"""API под интерфейс проверки ТЗ (формы 1-5): задачи с ходом по шагам и SSE, сопоставление требований,
нормативная база, история проверок. Описание для фронта: docs/API_UI.md."""
import asyncio
import json
import os
from functools import partial

from fastapi import APIRouter, File, Form, Header, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from . import history, jobs, llm, norms_admin
from .checks import OK_STATUSES, succeeded
from .norms import CATEGORIES, CATEGORY_GROUP, CHECK_GROUPS, parse_checks
from .progress import STEP_TITLES

router = APIRouter()
TRUE = ("1", "true", "yes", "on")


def store() -> str:
    from . import api
    return api.STORE


def _flag(q, f, default: bool) -> bool:
    v = f if f is not None else q
    if v is None:
        return default
    return v if isinstance(v, bool) else str(v).strip().lower() in TRUE


def _checks(q: list[str] | None, f: list[str] | None) -> list[str]:
    return parse_checks((f or []) + (q or []) or None)


def _save(file: UploadFile, folder: str) -> str:
    from .api import _save as save
    return save(file, folder)


def _bad(msg: str, code: int = 400):
    return JSONResponse({"error": msg}, status_code=code)


def _opts(use_llm_q, use_llm_f, orch_q, orch_f, checks_q, checks_f, user_q, user_f, llm_default: bool):
    from .pipeline import ORCHESTRATORS
    orchestrator = orch_f or orch_q or "linear"
    if orchestrator not in ORCHESTRATORS:
        raise ValueError(f"orchestrator must be one of {list(ORCHESTRATORS)}")
    use_llm = _flag(use_llm_q, use_llm_f, llm_default) and llm.enabled()
    return use_llm, orchestrator, _checks(checks_q, checks_f), (user_f or user_q or "").strip()[:120]


@router.get("/api/meta")
def meta():
    """Справочник интерфейса: группы и категории, ключи checks, названия шагов, статусы."""
    return {"groups": [{"key": k, "title": g, "categories": [c for c in CATEGORIES if CATEGORY_GROUP[c] == g]} for k, g in CHECK_GROUPS.items()],
            "category_group": CATEGORY_GROUP, "steps": STEP_TITLES,
            "statuses": ["ошибка", "неясно", "ручная проверка", "рекомендация"], "severities": ["high", "medium", "low"],
            "sections": {"main": "основной список", "llm_review": "на проверку", "unchecked": "не проверено"},
            "verdict_codes": {"blocked": "публиковать без исправлений нельзя", "review_high": "проверить вручную замечания высокого риска",
                              "fix": "исправить замечания со статусом «ошибка»", "review": "ошибок нет, есть места на ручную проверку",
                              "clean": "явных проблем не найдено", "partial": "проверены не все группы, вывод о публикации не делается",
                              "incomplete": "шаг проверки упал или не выполнен, вывод о публикации не делается"},
            "step_states": ["выполнен", "неприменим", "не выполнен", "ошибка"],
            "llm": llm.enabled(), "model": os.environ.get("TZCHECK_LLM_MODEL", ""), "max_files": 10}


# ---------- форма 2: задачи с ходом по шагам ----------

@router.post("/api/jobs", status_code=202)
def create_job(file: UploadFile = File(...), attachments: list[UploadFile] = File(default=[]), previous: UploadFile | None = File(default=None),
               use_llm: str | None = Query(None), use_llm_form: str | None = Form(None, alias="use_llm"),
               orchestrator: str | None = Query(None), orchestrator_form: str | None = Form(None, alias="orchestrator"),
               checks: list[str] | None = Query(None), checks_form: list[str] | None = Form(None, alias="checks"),
               user: str | None = Query(None), user_form: str | None = Form(None, alias="user"),
               public: str | None = Query(None), public_form: str | None = Form(None, alias="public")):
    try:
        use, orch, groups, who = _opts(use_llm, use_llm_form, orchestrator, orchestrator_form, checks, checks_form, user, user_form, True)
        pub = _flag(public, public_form, False)
    except ValueError as e:
        return _bad(str(e))
    files = [file] + [a for a in attachments or [] if a.filename]
    if len(files) > 10:
        return _bad("не больше 10 файлов в одной проверке")
    rid, folder = jobs.new_run(store())
    paths = [_save(file, folder)]
    for i, a in enumerate(files[1:], 1):
        sub = os.path.join(folder, f"attachment_{i}")
        os.makedirs(sub, exist_ok=True)
        paths.append(_save(a, sub))
    meta = {"user": who, "files": [os.path.basename(p) for p in paths], "checks": groups, "public": pub}
    if previous is not None and previous.filename:
        sub = os.path.join(folder, "previous")
        os.makedirs(sub, exist_ok=True)
        old = _save(previous, sub)
        meta["previous_file"] = os.path.basename(old)
        job = jobs.submit(rid, "requirements_compare", meta, partial(jobs.execute_req_compare, public=pub), store(), rid, folder, old, paths,
                          use, orch, groups, who)
    else:
        job = jobs.submit(rid, "check", meta, partial(jobs.execute_check, public=pub), store(), rid, folder, paths, use, orch, groups, who)
    return {"job_id": job.id, "run_id": job.id, "kind": job.kind, "status": job.status, "use_llm": use, "orchestrator": orch, "checks": groups,
            "public": pub,
            "links": {"self": f"/api/jobs/{job.id}", "events": f"/api/jobs/{job.id}/events", "result": f"/api/runs/{job.id}"}}


@router.get("/api/jobs")
def list_jobs(user: str | None = None):
    return {"items": [j for j in jobs.list_jobs() if history.visible(j.get("user", ""), user)]}


def _job_from_history(job_id: str) -> dict | None:
    if not history.RID_RE.match(job_id or ""):
        return None
    folder = os.path.join(store(), job_id)
    meta = history.read_meta(folder)
    if meta is None:
        return None
    res = history.load_run(store(), job_id) if meta.get("status") in OK_STATUSES else None
    status = "done" if res else ("error" if meta.get("status") in ("error",) else "lost")
    return {"job_id": job_id, "run_id": job_id, "kind": meta.get("kind", "check"), "status": status, "created_at": meta.get("created_at"),
            "finished_at": meta.get("finished_at"), "user": meta.get("user", ""), "files": meta.get("files", []), "checks": meta.get("checks", []),
            "steps": [], "progress": None, "error": meta.get("error") or (None if res else "ход задачи не сохранился: сервис перезапускался"),
            "result_url": f"/api/runs/{job_id}" if res else None, "result": res}


@router.get("/api/jobs/{job_id}")
def get_job(job_id: str, result: bool = True, user: str | None = None):
    job = jobs.get(job_id)
    if job is not None:
        if not history.visible(job.meta.get("user", ""), user):
            return _bad("job not found", 404)
        return job.snapshot(with_result=result)
    snap = _job_from_history(job_id)
    if snap is None or not history.visible(snap.get("user", ""), user):
        return _bad("job not found", 404)
    if not result:
        snap.pop("result", None)
    return snap


def _sse(event: str, data: dict, seq: int | None = None) -> str:
    head = f"id: {seq}\n" if seq is not None else ""
    return head + f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.get("/api/jobs/{job_id}/events")
async def job_events(job_id: str, after: int = 0, last_event_id: str | None = Header(None, alias="Last-Event-ID"), user: str | None = None):
    """Поток SSE: plan (список шагов), step (шаг сменил состояние или счётчики), done / error (конец задачи).
    Переподключение: ?after=<последний seq> или заголовок Last-Event-ID."""
    job = jobs.get(job_id)
    if job is not None and not history.visible(job.meta.get("user", ""), user):
        return _bad("job not found", 404)
    if job is None:
        snap = _job_from_history(job_id)
        if snap is None or not history.visible(snap.get("user", ""), user):
            return _bad("job not found", 404)

        async def once():
            yield _sse("done" if snap["status"] == "done" else "error",
                       {"job_id": job_id, "status": snap["status"], "error": snap["error"], "result_url": snap["result_url"]})
        return StreamingResponse(once(), media_type="text/event-stream")
    start = after
    try:
        start = max(start, int(last_event_id or 0))
    except ValueError:
        pass

    async def stream():
        pos, idle = start, 0.0
        yield _sse("snapshot", job.snapshot(with_result=False))
        while True:
            events = job.events[pos:]
            for ev in events:
                yield _sse(ev["event"], ev["data"], ev["seq"])
            pos += len(events)
            if job.status in ("done", "error") and pos >= len(job.events):
                return
            await asyncio.sleep(0.3)
            idle = 0.0 if events else idle + 0.3
            if idle >= 15:
                idle = 0.0
                yield ": keep-alive\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------- форма 1: «Сопоставить требования» ----------

@router.post("/api/requirements_compare")
def requirements_compare(old: UploadFile = File(...), new: UploadFile = File(...), attachments: list[UploadFile] = File(default=[]),
                         use_llm: str | None = Query(None), use_llm_form: str | None = Form(None, alias="use_llm"),
                         orchestrator: str | None = Query(None), orchestrator_form: str | None = Form(None, alias="orchestrator"),
                         checks: list[str] | None = Query(None), checks_form: list[str] | None = Form(None, alias="checks"),
                         user: str | None = Query(None), user_form: str | None = Form(None, alias="user"),
                         public: str | None = Query(None), public_form: str | None = Form(None, alias="public")):
    """old: прошлый документ (например, прошлогоднее ТЗ), new: текущее ТЗ, attachments: приложения текущего ТЗ."""
    try:
        use, orch, groups, who = _opts(use_llm, use_llm_form, orchestrator, orchestrator_form, checks, checks_form, user, user_form, False)
        pub = _flag(public, public_form, False)
    except ValueError as e:
        return _bad(str(e))
    rid, folder = jobs.new_run(store())
    paths = [_save(new, folder)]
    for i, a in enumerate([a for a in attachments or [] if a.filename], 1):
        sub = os.path.join(folder, f"attachment_{i}")
        os.makedirs(sub, exist_ok=True)
        paths.append(_save(a, sub))
    sub = os.path.join(folder, "previous")
    os.makedirs(sub, exist_ok=True)
    old_path = _save(old, sub)
    res = jobs.execute_req_compare(store(), rid, folder, old_path, paths, use, orch, groups, who, public=pub)
    out = jobs.public(res)
    if not succeeded(res):
        return JSONResponse(out, status_code=400)
    rc = out.pop("requirements_compare")
    if rc.get("status") != "ok":
        return JSONResponse(dict(rc, run_id=rid), status_code=400)
    return dict(rc, run_id=rid, links=out.get("links"), result=out)


# ---------- форма 1, шестерёнка: нормативная база ----------

@router.get("/api/norms")
def norms_list():
    return norms_admin.list_documents()


@router.get("/api/norms/search")
def norms_search(q: str, limit: int = 5, internal_only: bool = False):
    return {"query": q, "items": norms_admin.search(q, limit=max(1, min(limit, 20)), internal_only=internal_only)}


@router.get("/api/norms/{doc_id}")
def norms_get(doc_id: str):
    d = norms_admin.get_document(doc_id)
    return d if d is not None else _bad("document not found", 404)


@router.get("/api/norms/{doc_id}/file")
def norms_file(doc_id: str):
    p = norms_admin.original_file(doc_id)
    if p is None:
        return _bad("file not found", 404)
    d = norms_admin.get_document(doc_id) or {}
    return FileResponse(str(p), filename=d.get("file") or p.name)


@router.post("/api/norms", status_code=201)
def norms_upload(file: UploadFile = File(...), title: str = Form(""), customer: str = Form("")):
    data = file.file.read()
    if not data:
        return _bad("файл пустой")
    try:
        return norms_admin.upload_bytes(data, os.path.basename(file.filename or "standard.docx"), title, customer)
    except FileExistsError as e:
        return _bad(str(e), 409)
    except ValueError as e:
        return _bad(str(e))


@router.delete("/api/norms/{doc_id}")
def norms_delete(doc_id: str):
    try:
        ok = norms_admin.delete(doc_id)
    except PermissionError as e:
        return _bad(str(e), 403)
    return {"deleted": doc_id} if ok else _bad("document not found", 404)


# ---------- форма 5: история проверок ----------

@router.get("/api/runs")
def runs_list(user: str | None = None, limit: int = 50, offset: int = 0):
    return history.list_runs(store(), user=user, limit=max(1, min(limit, 500)), offset=max(0, offset))


@router.get("/api/runs/{rid}")
def runs_get(rid: str, user: str | None = None):
    res = history.load_run(store(), rid)
    if res is None or not history.visible((res.get("run") or {}).get("user", ""), user):
        return _bad("run not found", 404)
    return res


@router.get("/api/runs/{rid}/trace")
def runs_trace(rid: str, user: str | None = None):
    """Журнал вызовов модели: промпты (с маской ПД), сырые ответы, снятые кандидаты. Видимость та же, что у /api/runs/{rid}."""
    run = history.load_run(store(), rid)
    if run is None or not history.visible((run.get("run") or {}).get("user", ""), user):
        return _bad("run not found", 404)
    res = history.load_trace(store(), rid)
    return res if res is not None else _bad("run not found", 404)
