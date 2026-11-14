"""Управление обновлением справочников: расписание, запрос «обновить сейчас», состояние запуска.

Всё лежит в <data_dir>/control/ (в Docker это общий том refdata), поэтому веб-приложение и сервис updater
обмениваются только файлами: app пишет schedule.json и request.json, updater читает их, пишет scheduler.json
(пульс и следующий запуск), run.json (текущий запуск), run.log (вывод) и history.jsonl (прошлые запуски).
"""
import datetime
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid

from .. import refdata

WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
OUTCOME = {0: "все источники обновлены или без изменений", 1: "часть источников недоступна, оставлены прежние копии",
           2: "ошибка в параметрах запуска", 3: "уже шёл другой запуск, этот не выполнен"}
HEARTBEAT_FRESH = 30
LOCK_TTL = 6 * 3600
TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_lock = threading.Lock()


def _registered() -> list[str]:
    """Справочники в порядке обхода, как их знает python -m tzcheck.updater; новый источник появится на вкладке сам."""
    try:
        from .__main__ import SOURCES
        return list(SOURCES)
    except Exception:
        return ["okpd2", "laws", "eaeu", "gost", "arshin", "ktru"]


SOURCE_ORDER = _registered()


def control_dir() -> str:
    return os.path.join(refdata.data_dir(), "control")


def _path(name: str) -> str:
    return os.path.join(control_dir(), name)


def _read(name: str, default=None):
    for attempt in range(20):
        try:
            with open(_path(name), encoding="utf-8") as f:
                return json.load(f)
        except PermissionError:
            time.sleep(0.05)
        except (OSError, ValueError):
            return default
    return default


def _write(name: str, obj):
    os.makedirs(control_dir(), exist_ok=True)
    tmp = _path(f"{name}.{os.getpid()}.{threading.get_ident()}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    for attempt in range(40):
        try:
            os.replace(tmp, _path(name))
            return
        except PermissionError:
            # Windows не даёт заменить файл, пока его читает вкладка; в Linux этого не бывает
            if attempt == 39:
                raise
            time.sleep(0.05)


def now() -> datetime.datetime:
    return datetime.datetime.now()


def stamp(dt: datetime.datetime | None = None) -> str:
    return (dt or now()).strftime("%Y-%m-%dT%H:%M:%S")


def defaults() -> dict:
    at = os.environ.get("TZCHECK_UPDATE_AT", "04:30")
    try:
        weekday = int(os.environ.get("TZCHECK_UPDATE_FULL_WEEKDAY", "6"))
    except ValueError:
        weekday = 6
    return {"enabled": os.environ.get("TZCHECK_UPDATE_ENABLED", "1") != "0", "at": at if TIME_RE.match(at) else "04:30",
            "full_weekday": weekday if -1 <= weekday <= 6 else 6, "sources": list(SOURCE_ORDER)}


def validate(data: dict) -> dict:
    out = defaults()
    if "enabled" in data:
        out["enabled"] = bool(data["enabled"])
    if "at" in data:
        at = str(data["at"]).strip()
        if not TIME_RE.match(at):
            raise ValueError("время обновления в формате ЧЧ:ММ, например 04:30")
        out["at"] = at
    if "full_weekday" in data:
        try:
            wd = int(data["full_weekday"])
        except (TypeError, ValueError):
            raise ValueError("день полного обхода: число от 0 (понедельник) до 6 (воскресенье) или -1 (не делать)")
        if not -1 <= wd <= 6:
            raise ValueError("день полного обхода: число от 0 (понедельник) до 6 (воскресенье) или -1 (не делать)")
        out["full_weekday"] = wd
    if "sources" in data:
        src = [s for s in SOURCE_ORDER if s in set(data["sources"] or [])]
        bad = [s for s in data["sources"] or [] if s not in SOURCE_ORDER]
        if bad:
            raise ValueError("неизвестные источники: " + ", ".join(map(str, bad)))
        if not src:
            raise ValueError("выберите хотя бы один справочник для ночного обновления")
        out["sources"] = src
    return out


def load_settings() -> dict:
    """Настройки из control/schedule.json; переменные окружения задают значения по умолчанию."""
    saved = _read("schedule.json")
    base = defaults()
    if not isinstance(saved, dict):
        base["saved_at"] = None
        return base
    try:
        out = validate({k: saved[k] for k in ("enabled", "at", "full_weekday", "sources") if k in saved})
    except ValueError:
        out = base
    out["saved_at"] = saved.get("saved_at")
    return out


def save_settings(data: dict) -> dict:
    s = validate(data)
    s["saved_at"] = stamp()
    _write("schedule.json", s)
    return s


def next_run(current: datetime.datetime, at: str) -> datetime.datetime:
    h, m = (int(x) for x in at.split(":"))
    nxt = current.replace(hour=h, minute=m, second=0, microsecond=0)
    return nxt if nxt > current else nxt + datetime.timedelta(days=1)


def plan(settings: dict, current: datetime.datetime | None = None) -> dict | None:
    if not settings.get("enabled"):
        return None
    nxt = next_run(current or now(), settings["at"])
    return {"at": stamp(nxt), "full": nxt.weekday() == settings.get("full_weekday", 6)}


def lock_path() -> str:
    return os.path.join(refdata.data_dir(), "updates", ".lock")


def lock_active() -> bool:
    try:
        return time.time() - os.path.getmtime(lock_path()) < LOCK_TTL
    except OSError:
        return False


def heartbeat() -> dict | None:
    hb = _read("scheduler.json")
    if not isinstance(hb, dict):
        return None
    try:
        age = (now() - datetime.datetime.fromisoformat(hb["alive_at"])).total_seconds()
    except (KeyError, ValueError):
        return None
    hb["age"] = round(age)
    hb["fresh"] = age < HEARTBEAT_FRESH
    return hb


def write_heartbeat(info: dict):
    _write("scheduler.json", dict(info, alive_at=stamp(), pid=os.getpid()))


def run_state() -> dict | None:
    st = _read("run.json")
    if not isinstance(st, dict):
        return None
    if st.get("state") == "running" and not lock_active():
        try:
            started = datetime.datetime.fromisoformat(st.get("started_at") or "")
            if (now() - started).total_seconds() > 60:
                st["state"] = "lost"
                st["outcome"] = "запуск прервался: процесс обновления остановлен до завершения"
        except ValueError:
            st["state"] = "lost"
    return st


def busy() -> str | None:
    st = run_state()
    if st and st.get("state") == "running":
        return f"уже идёт обновление ({', '.join(st.get('sources') or ['все'])}), начато {st.get('started_at', '')[11:16]}"
    if lock_active():
        return "уже идёт обновление (занят data/updates/.lock, например запуск из командной строки)"
    return None


def log_tail(lines: int = 60) -> list[str]:
    try:
        with open(_path("run.log"), encoding="utf-8", errors="replace") as f:
            return [x.rstrip("\n") for x in f.readlines()[-lines:]]
    except OSError:
        return []


def history(limit: int = 15) -> list[dict]:
    try:
        with open(_path("history.jsonl"), encoding="utf-8") as f:
            rows = [json.loads(x) for x in f if x.strip()]
    except (OSError, ValueError):
        return []
    return rows[-limit:][::-1]


def make_request(sources: list[str], full: bool, trigger: str = "manual") -> dict:
    bad = [s for s in sources if s not in SOURCE_ORDER]
    if bad:
        raise ValueError("неизвестные источники: " + ", ".join(bad))
    return {"id": uuid.uuid4().hex[:10], "sources": [s for s in SOURCE_ORDER if s in sources] or list(SOURCE_ORDER),
            "full": bool(full), "trigger": trigger, "requested_at": stamp()}


def queue_request(req: dict) -> dict:
    """Запрос для сервиса updater: он заберёт control/request.json при следующем такте (раз в несколько секунд)."""
    _write("request.json", req)
    _write("run.json", dict(req, state="queued"))
    return req


def take_request() -> dict | None:
    path = _path("request.json")
    taken = path + f".{os.getpid()}.taken"
    try:
        os.replace(path, taken)
    except OSError:
        return None
    try:
        with open(taken, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None
    finally:
        try:
            os.remove(taken)
        except OSError:
            pass


def pending_request() -> dict | None:
    return _read("request.json")


def command(req: dict) -> list[str]:
    args = [sys.executable, "-m", "tzcheck.updater"]
    if set(req["sources"]) != set(SOURCE_ORDER):
        args += ["--only", ",".join(req["sources"])]
    if req.get("full"):
        args.append("--full")
    return args


def execute(req: dict, runner: str, echo: bool = False) -> dict:
    """Запускает python -m tzcheck.updater, пишет вывод в control/run.log и итог в run.json и history.jsonl."""
    with _lock:
        state = dict(req, state="running", runner=runner, started_at=stamp(), command=" ".join(command(req)[1:]))
        _write("run.json", state)
    os.makedirs(control_dir(), exist_ok=True)
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    t0 = time.time()
    code = None
    with open(_path("run.log"), "w", encoding="utf-8") as log:
        log.write(f"{stamp()[11:19]} запуск: {state['command']}\n")
        log.flush()
        try:
            proc = subprocess.Popen(command(req), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=root, env=env,
                                    text=True, encoding="utf-8", errors="replace")
            for line in proc.stdout:
                log.write(line)
                log.flush()
                if echo:
                    print(line, end="", flush=True)
            code = proc.wait()
        except OSError as e:
            log.write(f"не удалось запустить обновление: {e}\n")
            code = -1
        log.write(f"{stamp()[11:19]} завершено, код {code}\n")
    state.update(state="done", finished_at=stamp(), seconds=round(time.time() - t0, 1), code=code,
                 outcome=OUTCOME.get(code, f"обновление упало с кодом {code}, подробности в журнале запуска"))
    with _lock:
        _write("run.json", state)
        with open(_path("history.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({k: state.get(k) for k in ("id", "trigger", "runner", "sources", "full", "requested_at", "started_at",
                                                          "finished_at", "seconds", "code", "outcome")}, ensure_ascii=False) + "\n")
    return state


def start_local(req: dict) -> dict:
    """Без сервиса updater (uvicorn локально): обновление идёт в фоне в процессе веб-приложения."""
    state = dict(req, state="running", runner="app", started_at=stamp(), command=" ".join(command(req)[1:]))
    _write("run.json", state)
    threading.Thread(target=execute, args=(req, "app"), daemon=True).start()
    return state
