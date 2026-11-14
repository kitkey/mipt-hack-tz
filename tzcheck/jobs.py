"""Асинхронные задачи проверки (форма 2): запуск в фоне, ход по шагам конвейера, результат по готовности.

job_id совпадает с run_id: файлы задачи лежат в STORE/<run_id>, и после перезапуска сервиса готовая задача
отдаётся из сохранённого report.json. Ход хранится в памяти процесса: шаги (статус, начало, конец, счётчики) и
события для потока SSE."""
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from . import history, revisions
from .checks import succeeded
from .pipeline import check
from .progress import STATE_RU, STEP_TITLES, now_iso, reporter
from .report import write_all

TAIL_STEPS = ("report", "check_old", "req_pairing")  # шаги задачи после конвейера, в порядке выполнения
_JOBS: dict[str, "Job"] = {}
_LOCK = threading.Lock()
_POOL = ThreadPoolExecutor(max_workers=max(1, int(os.environ.get("TZCHECK_JOB_WORKERS", "2"))), thread_name_prefix="tzcheck-job")


# состояние шага по-русски: выполнен / неприменим / не выполнен / ошибка, для ещё не завершённых ожидает / идёт
STEP_STATE = dict(STATE_RU, pending="ожидает", running="идёт")
FINISHED = ("done", "skipped", "error", "not_done")


def describe(step: str, d: dict) -> str:
    """Строка хода для человека: «фрагмент 3 из 12, найдено 5»."""
    parts = []
    if d.get("blocks") is not None:
        parts.append(f"фрагментов: {d['blocks']}" + (f", файлов: {d['files']}" if d.get("files", 1) > 1 else ""))
    if d.get("warnings") is not None:
        parts.append(f"предупреждений: {d['warnings']}")
    if d.get("stage"):
        parts.append(str(d["stage"]))
    if d.get("fragment") is not None:
        parts.append(f"фрагмент {d['fragment']} из {d['fragments']}")
    if d.get("batch") is not None:
        parts.append(f"пакет {d['batch']} из {d['batches']}, замечаний: {d.get('items', 0)}")
    if d.get("candidates") is not None:
        parts.append(f"замечаний: {d['candidates']}")
    if d.get("found") is not None:
        parts.append(f"найдено замечаний: {d['found']}")
    if d.get("confirmed") is not None:
        parts.append(f"подтверждено: {d['confirmed']}")
    if d.get("findings") is not None:
        parts.append(f"замечаний: {d['findings']}")
    if d.get("model"):
        parts.append(f"на проверку: {d['model']}")
    if d.get("unchecked"):
        parts.append(f"не проверено: {d['unchecked']}")
    if d.get("revisions") is not None:
        parts.append(f"правок в файле: {d['revisions']}")
    if d.get("pairs") is not None:
        parts.append(f"изменено {d['pairs'].get('changed', 0)}, новых {d['pairs'].get('added', 0)}, убрано {d['pairs'].get('removed', 0)}")
    if d.get("inner"):
        parts.append(str(d["inner"]))
    if d.get("ошибка"):
        parts.append(f"ошибка: {d['ошибка']}")
    if d.get("причина"):
        parts.append(str(d["причина"]))
    return "; ".join(parts)


class Job:
    def __init__(self, job_id: str, kind: str, meta: dict):
        self.id, self.kind, self.meta = job_id, kind, meta
        self.status = "queued"
        self.created_at, self.started_at, self.finished_at = now_iso(), None, None
        self.steps: list[dict] = []
        self.events: list[dict] = []
        self.result: dict | None = None
        self.error: str | None = None
        self.cond = threading.Condition()

    def _step(self, sid: str) -> dict:
        st = next((s for s in self.steps if s["id"] == sid), None)
        if st is None:
            st = {"id": sid, "title": STEP_TITLES.get(sid.split(":")[-1], sid), "status": "pending", "state": "ожидает", "started_at": None,
                  "finished_at": None, "seconds": None, "details": {}, "text": ""}
            rank = TAIL_STEPS.index(sid) if sid in TAIL_STEPS else -1
            pos = next((i for i, s in enumerate(self.steps) if s["id"] in TAIL_STEPS and TAIL_STEPS.index(s["id"]) > rank), len(self.steps))
            self.steps.insert(pos, st)
        return st

    def _push(self, kind: str, data: dict):
        self.events.append({"seq": len(self.events) + 1, "event": kind, "data": data})

    def on_event(self, ev: dict):
        with self.cond:
            if ev["event"] == "plan":
                for s in ev["steps"]:
                    self._step(s["id"])
                self._push("plan", {"steps": [self.public_step(s) for s in self.steps]})
            else:
                st = self._step(ev["step"])
                st["details"].update(ev.get("details") or {})
                if ev["event"] == "step_started":
                    st.update(status="running", started_at=ev["ts"], _t=time.time())
                elif ev["event"] == "step_done":
                    st.update(status="done", finished_at=ev["ts"])
                elif ev["event"] == "step_skipped":
                    st.update(status="skipped", finished_at=ev["ts"])
                elif ev["event"] == "step_failed":
                    st.update(status="error", finished_at=ev["ts"])
                elif ev["event"] == "step_incomplete":
                    st.update(status="not_done", finished_at=ev["ts"])
                st["state"] = STEP_STATE.get(st["status"], "")
                if st["status"] in FINISHED and st.get("_t"):
                    st["seconds"] = round(time.time() - st.pop("_t"), 1)
                st["text"] = describe(st["id"], st["details"])
                self._push("step", self.public_step(st))
            self.cond.notify_all()

    @staticmethod
    def public_step(st: dict) -> dict:
        return {k: v for k, v in st.items() if not k.startswith("_")}

    def snapshot(self, with_result: bool = True) -> dict:
        with self.cond:
            done = sum(1 for s in self.steps if s["status"] in FINISHED)
            cur = next((s for s in self.steps if s["status"] == "running"), None)
            out = {"job_id": self.id, "run_id": self.id, "kind": self.kind, "status": self.status, "created_at": self.created_at,
                   "started_at": self.started_at, "finished_at": self.finished_at, "user": self.meta.get("user", ""),
                   "files": self.meta.get("files", []), "checks": self.meta.get("checks", []),
                   "progress": {"done": done, "total": len(self.steps), "percent": round(100 * done / len(self.steps)) if self.steps else 0,
                                "current": cur["id"] if cur else None, "current_title": cur["title"] if cur else None,
                                "current_text": cur["text"] if cur else ""},
                   "steps": [self.public_step(s) for s in self.steps], "error": self.error,
                   "result_url": f"/api/runs/{self.id}" if self.status == "done" else None}
            if with_result and self.status == "done":
                out["result"] = self.result
            return out

    def finish(self, status: str, result: dict | None = None, error: str | None = None):
        with self.cond:
            self.status, self.result, self.error, self.finished_at = status, result, error, now_iso()
            self._push(status, {"job_id": self.id, "status": status, "error": error, "result_url": f"/api/runs/{self.id}" if status == "done" else None,
                                "summary": (result or {}).get("summary") if result else None})
            self.cond.notify_all()


def public(res: dict) -> dict:
    return {k: v for k, v in res.items() if not k.startswith("_")}


def new_run(store: str) -> tuple[str, str]:
    rid = uuid.uuid4().hex[:12]
    folder = os.path.join(store, rid)
    os.makedirs(folder, exist_ok=True)
    return rid, folder


TRACE = "trace.json"


def write_trace(res: dict, folder: str):
    """Журнал вызовов модели прогона: промпты и ответы как ушли наружу (с маской ПД) и снятые кандидаты. Отдаётся только /api/runs/{id}/trace."""
    import json
    with open(os.path.join(folder, TRACE), "w", encoding="utf-8") as fh:
        json.dump(res.get("_trace") or {"calls": [], "rejected": [], "dropped": []}, fh, ensure_ascii=False, indent=1)


def _write_outputs(res: dict, src: str, folder: str, rep) -> dict:
    rep.start("report")
    paths = write_all(res, src, folder)
    write_trace(res, folder)
    rev = revisions.write(res, src, folder)
    res["revisions"] = {k: {"applied": v["applied"], "skipped": v["skipped"], "file": os.path.basename(v["path"]) if v.get("path") else None}
                        for k, v in rev.items()}
    rep.done("report", revisions=len((rev.get("main") or {}).get("applied", [])))
    return paths


def execute_check(store: str, rid: str, folder: str, paths: list[str], use_llm: bool, orchestrator: str | None, checks: list[str],
                  user: str = "", progress=None, kind: str = "check", public: bool = False) -> dict:
    """Проверка с сохранением результата, файлов и паспорта запуска (run.json) для истории."""
    t0 = time.time()
    history.write_meta(folder, {"run_id": rid, "kind": kind, "created_at": now_iso(), "user": user, "checks": checks,
                                "files": [os.path.basename(p) for p in paths], "use_llm": use_llm, "orchestrator": orchestrator or "linear",
                                "public": bool(public), "status": "running"})
    rep = reporter(progress)
    rep.plan(["report"])
    res = check(paths, use_llm=use_llm, orchestrator=orchestrator, checks=checks, progress=progress, public=public)
    res["run_id"] = rid
    if succeeded(res):
        _write_outputs(res, paths[0], folder, rep)
        base = os.path.splitext(os.path.basename(paths[0]))[0]
        history.write_meta(folder, {"report": base + ".report.json"})
    else:
        rep.skip("report", "документ не разобран")
    history.write_meta(folder, {"status": res["status"], "finished_at": now_iso(), "seconds": round(time.time() - t0, 1),
                                "error": res.get("error")})
    res["links"] = history.links(rid, folder, res)
    return res


def execute_req_compare(store: str, rid: str, folder: str, old_path: str, new_paths: list[str], use_llm: bool, orchestrator: str | None,
                        checks: list[str], user: str = "", progress=None, public: bool = False) -> dict:
    """Сопоставление требований: полная проверка текущего документа, проверка прошлого, сведение требований попарно."""
    from . import req_compare
    import json
    t0 = time.time()
    rep = reporter(progress)
    rep.plan(["check_old", "req_pairing"])
    res = execute_check(store, rid, folder, new_paths, use_llm, orchestrator, checks, user, progress, kind="requirements_compare",
                        public=public)
    history.write_meta(folder, {"previous_file": os.path.basename(old_path)})
    if not succeeded(res):
        rep.skip("check_old", "текущий документ не разобран")
        rep.skip("req_pairing", "текущий документ не разобран")
        return res
    rep.start("check_old")

    def inner(ev):
        if ev.get("event") in ("step_started", "step_progress"):
            text = describe(ev.get("step", ""), ev.get("details") or {})
            rep.update(inner=ev.get("title", "") + (f": {text}" if text else ""))

    old = check(old_path, use_llm=use_llm, orchestrator=orchestrator, checks=checks, progress=inner, public=public)
    if not succeeded(old):
        rep.fail("check_old", old.get("error", ""))
        rep.skip("req_pairing", "прошлый документ не разобран")
        res["requirements_compare"] = {"status": "error", "error": f"прошлый документ не разобран: {old.get('error')}"}
        return res
    rep.done("check_old", findings=sum(1 for f in old["findings"] if f.get("section") == "main"), inner="")
    rep.start("req_pairing")
    req = req_compare.compare_requirements(old["_doc"], res["_doc"])
    summary = req_compare.summarize(old, res, req)
    out = {"status": "ok", "old_file": old["file"], "new_file": res["file"], "requirements": req_compare.public(req),
           "findings_summary": summary, "summary": summary["summary"],
           "old_check": {"file": old["file"], "summary": old["summary"], "stats": old["stats"], "findings": old["findings"]}}
    with open(os.path.join(folder, history.REQ), "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
    rep.done("req_pairing", pairs=req["counts"])
    res["requirements_compare"] = out
    history.write_meta(folder, {"seconds": round(time.time() - t0, 1), "finished_at": now_iso()})
    res["links"] = history.links(rid, folder, res)
    return res


def submit(job_id: str, kind: str, meta: dict, fn, *args) -> Job:
    """fn(*args, progress=callback) -> результат; задача выполняется в пуле потоков."""
    job = Job(job_id, kind, meta)
    with _LOCK:
        _JOBS[job_id] = job

    def run():
        job.status, job.started_at = "running", now_iso()
        try:
            res = fn(*args, progress=job.on_event)
            ok = succeeded(res)
            job.finish("done" if ok else "error", public(res), None if ok else str(res.get("error") or "ошибка проверки"))
        except Exception as e:  # сбой задачи отдаётся в состоянии, а не роняет сервис
            job.finish("error", None, f"{type(e).__name__}: {e}")

    _POOL.submit(run)
    return job


def get(job_id: str) -> Job | None:
    with _LOCK:
        return _JOBS.get(job_id)


def list_jobs() -> list[dict]:
    with _LOCK:
        jobs = list(_JOBS.values())
    return [j.snapshot(with_result=False) for j in sorted(jobs, key=lambda j: j.created_at, reverse=True)]
