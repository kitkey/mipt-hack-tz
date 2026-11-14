"""Вкладка «Справочники»: состояние локальных копий, каталог Росстандарта, запуск и расписание обновления."""
import collections
import datetime
import gzip
import html
import json
import os
import re
import threading

from fastapi import APIRouter, Body
from fastapi.responses import HTMLResponse, JSONResponse

from . import refdata
from .updater import arshin, changes, control, eaeu, gost, ktru, laws, okpd2
from .updater.__main__ import SOURCES
from .updater.base import Context

router = APIRouter()
_start_lock = threading.Lock()

PURPOSE = {
    "okpd2": "проверка кода ОКПД 2 и подсказка верного кода",
    "laws": "действующие редакции законов для ссылок в основаниях замечаний",
    "eaeu": "перечни стандартов к техрегламентам ЕАЭС, в замечаниях о ГОСТ",
    "gost": "статус стандарта, заменяющий стандарт и дата замены",
    "arshin": "утверждённые типы средств измерений для позиций-приборов",
    "ktru": "позиции КТРУ и их обязательные характеристики",
}
VIA_RU = {"rst_api": "API каталога Росстандарта", "rst_browser": "API Росстандарта через браузер", "protect": "карточка protect.gost.ru",
          "protect_feed": "лента поступлений protect.gost.ru", "rst_full": "полный обход каталога Росстандарта"}
GROUPS_RU = {"watch": "постоянный список наблюдения", "tz_online": "стандарты из ТЗ, проверенных с доступом к сети",
             "tz_offline": "запрошены без сети при проверке ТЗ или сборке корпуса норм",
             "manual": "добавлены вручную на этой вкладке", "watched_before": "уже отмечены как наблюдаемые в копии"}
RUN_STATE_RU = {"queued": "ждёт сервис обновления", "running": "идёт обновление", "done": "завершено", "lost": "прервано"}
TRIGGER_RU = {"manual": "вручную", "schedule": "по расписанию", "start": "при запуске сервиса"}


def _how() -> dict:
    """Какие запросы уходят на каждом звене цепочки; адреса берутся из констант модулей обновления."""
    yy = datetime.datetime.now().year % 100
    return {
        "okpd2": {
            "sends": "ничего, кроме скачивания файлов классификатора",
            "links": {
                "rosstat": [f"GET {okpd2.ROSSTAT_PAGE}: страница классификаторов, на ней ищется ссылка на архив OKPD2*.rar",
                            f"GET архива (по умолчанию {okpd2.ROSSTAT_ARCHIVE}) с If-None-Match и If-Modified-Since; "
                            "распаковка bsdtar, 7z или unrar, разбор docx"],
                "classifikators": [f"GET {okpd2.CLASSIF_XLSX}; заменяет файл Росстата, только если тому больше "
                                   f"{okpd2.MIRROR_AFTER_DAYS} дней (TZCHECK_OKPD2_MIRROR_AFTER_DAYS)"],
            },
            "always": [f"GET {okpd2.PROTECT_CARD}: номер последнего официального изменения ОКПД 2 для сверки"],
        },
        "laws": {
            "sends": "номер и дата принятия каждого акта (" + ", ".join(a["act"] for a in laws.ACTS) + ")",
            "links": {
                "actual_api": [f"GET {laws.ACTUAL_API}/attrsearch/?bpa=ebpi&q=<номер и дата акта>",
                               f"GET {laws.ACTUAL_API}/redactions/?bpa=ebpi&t=<hash документа>: действующая и будущие редакции"],
                "actual_browser": [f"браузер открывает {laws.ACTUAL}/, вводит номер акта и перехватывает ответы того же API"],
                "consultant": [f"GET {laws.CONSULTANT}<номер документа>/: «Список изменяющих документов» для сверки"],
            },
        },
        "eaeu": {
            "sends": "ничего, кроме даты, с которой искать новые акты",
            "links": {
                "protect_trlist": [f"GET {eaeu.PROTECT}/TrList и {eaeu.PROTECT}/TrList/<номер>: около 94 перечней стандартов",
                                   f"GET {eaeu.EEC_GENERAL} и страниц техрегламентов на {eaeu.EEC}",
                                   f"GET {eaeu.EAEU_API}?page_size=100&page_number=N&date_published_from=<дата>"],
                "eec_portal": [f"те же запросы к {eaeu.EEC} и {eaeu.EAEU_API}, без таблиц перечней; перечни остаются из прежней копии"],
            },
        },
        "gost": {
            "sends": "только обозначения стандартов (например «ГОСТ Р 53780-2010»), номера стандартов и месяцы лент",
            "links": {
                "rst_api": [f"GET {gost.LIBS['national']['page']} и {gost.LIBS['interstate']['page']}: со страницы каталога "
                            "читаются заголовки App-Id (блок adminsHeaders), кэш 12 часов",
                            f"GET {gost.API}?query=<обозначение>&page=1&size=20 с этими заголовками, по одному запросу на стандарт",
                            f"полный обход (--full): GET {gost.API}?query=<год, например -2015>&size=1000 по всем годам, около часа"],
                "rst_browser": [f"браузер (Playwright) открывает страницу каталога и из неё вызывает {gost.API_PATH}?query=<обозначение>"],
                "protect": [f"GET {gost.PROTECT}/gost?year=0&month=0&search=<номер> и карточка {gost.PROTECT}/gost/details/…; "
                            f"своды правил всегда через {gost.PROTECT}/sp"],
            },
            "always": [f"GET {gost.PROTECT}/gost?year=Г&month=М: новые стандарты за текущий и прошлый месяц, до 40 страниц",
                       f"GET {gost.PROTECT}/gost/changes?year=Г&month=М: изменения и поправки; изменённые стандарты из копии перепроверяются"],
        },
        "arshin": {
            "sends": f"маски номеров типов текущего и прошлого года (*-{yy:02d}, *-{(yy - 1) % 100:02d}); в полном обходе только сортировку",
            "links": {
                "eapi": [f"GET {arshin.EAPI}/mit?number=*-{yy:02d}&rows={arshin.ROWS}&start=N, затем *-{(yy - 1) % 100:02d}",
                         f"полный обход (--full): GET {arshin.EAPI}/mit?sort=number asc и desc, весь реестр"],
                "eapi_browser": [f"браузер открывает {arshin.SPA} и из неё вызывает тот же {arshin.EAPI}/mit"],
            },
        },
        "ktru": {
            "sends": "коды ОКПД 2 из списка наблюдения (" + ", ".join(ktru.WATCH[:3]) + " и др.) и коды позиций КТРУ",
            "links": {
                "zakupki_http": [f"GET {ktru.RSS}?searchString=<код ОКПД 2>&active=on&recordsPerPage=_50",
                                 f"GET {ktru.COMMON}?itemId=<код позиции> и {ktru.DESC}?itemId=<код позиции>, "
                                 f"не больше {ktru.MAX_CARDS} карточек (TZCHECK_KTRU_MAX_CARDS)",
                                 "между запросами к zakupki.gov.ru пауза " + os.environ.get("TZCHECK_ZAKUPKI_DELAY", "60") + " с (TZCHECK_ZAKUPKI_DELAY)"],
                "zakupki_browser": [f"браузер открывает {ktru.SEARCH} и из неё вызывает те же адреса"],
            },
        },
    }


def _chains() -> dict:
    ctx = Context(offline=True, browser=False)
    out = {}
    for name, cls in SOURCES.items():
        src = cls(ctx)
        out[name] = {"title": cls.title, "file": cls.filename, "chain": [(i, t) for i, t, _ in src.chain()]}
    return out


def _count_file(path: str) -> int | None:
    try:
        opener = gzip.open if path.endswith(".gz") else open
        with opener(path, "rt", encoding="utf-8") as f:
            data = json.load(f)
        return len(data.get("records", {})) if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _meta(name: str, filename: str) -> dict:
    base = filename.split(".")[0]
    try:
        with open(os.path.join(refdata.data_dir(), f"{base}_meta.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _msk(iso: str | None) -> str:
    return refdata.msk(iso, True) if iso else ""


def source_rows() -> list[dict]:
    chains = _chains()
    rows = []
    for name in control.SOURCE_ORDER:
        info = chains[name]
        path = os.path.join(refdata.data_dir(), info["file"])
        meta = _meta(name, info["file"])
        _, doc = changes._latest(name)
        run = (doc or {}).get("runs", [None])[-1] if doc else None
        _, changed_run = changes._last_with_changes(name)
        exists = os.path.exists(path)
        count = meta.get("records_count")
        if count is None and exists:
            count = _count_file(path)
        status = run.get("status") if run else None
        errors = (run or {}).get("errors") or meta.get("last_errors") or []
        reason = ""
        if status == "keep":
            reason = "; ".join(f"{e.get('title')}: {e.get('error')}" for e in errors[:3])
        rows.append({
            "name": name, "title": info["title"], "purpose": PURPOSE.get(name, ""), "file": info["file"], "exists": exists,
            "records": count, "checked_at": (run or {}).get("at") or meta.get("checked_at") or meta.get("fetched_at"),
            "changed_at": (changed_run or {}).get("at") or meta.get("fetched_at"),
            "changed_from_journal": bool(changed_run),
            "via": meta.get("via") or meta.get("source_type"), "via_title": meta.get("via_title") or meta.get("provenance") or meta.get("source_type"),
            "source_url": meta.get("source_url"), "version": meta.get("version_label") or meta.get("source_version") or meta.get("version"),
            "status": status, "status_ru": changes.STATUS_RU.get(status, "нет сведений об обновлении") if status else "нет сведений об обновлении",
            "reason": reason, "counts": (run or {}).get("counts"), "seconds": (run or {}).get("seconds"), "mode": (run or {}).get("mode"),
            "notes": ((run or {}).get("notes") or meta.get("notes") or [])[:8], "warning": meta.get("lag_warning"),
            "chain": info["chain"], "errors": errors,
        })
    return rows


def _gost_records() -> dict:
    return refdata._mirror("gost.json.gz")


def gost_overview() -> dict:
    recs = _gost_records()
    groups = gost.target_groups(recs)
    targets = set().union(*groups.values())
    watched = {k: r for k, r in recs.items() if r.get("watch")}
    by_status = collections.Counter((r.get("status") or "не указан") for r in recs.values())
    by_status_watch = collections.Counter((r.get("status") or "не указан") for r in watched.values())
    feed = collections.Counter(r.get("feed_month") for r in recs.values() if r.get("via") == "protect_feed")
    by_via = collections.Counter(r.get("via") for r in recs.values())
    meta = _meta("gost", "gost.json.gz")
    recent, full_at = [], None
    for path in sorted(changes.glob.glob(os.path.join(changes.updates_dir(), "????-??-??_gost.json")), reverse=True):
        try:
            with open(path, encoding="utf-8") as f:
                doc = json.load(f)
        except (OSError, ValueError):
            continue
        for run in reversed(doc.get("runs", [])):
            if run.get("mode") == "full" and run.get("status") != "keep" and not full_at:
                full_at = run.get("at")
            for line in run.get("lines") or []:
                if len(recent) < 40:
                    recent.append({"at": run.get("at"), "line": line})
    return {
        "total": len(recs), "targets": len(targets), "watched": len(watched),
        "groups": [{"id": k, "title": GROUPS_RU[k], "count": len(v)} for k, v in groups.items()],
        "by_status": by_status.most_common(), "by_status_watch": dict(by_status_watch),
        "feed": sorted(((m, n) for m, n in feed.items() if m), key=lambda x: x[0][3:] + x[0][:2], reverse=True),
        "by_via": [(VIA_RU.get(k, k or "не указан"), n) for k, n in by_via.most_common()],
        "known": meta.get("records_count") if meta.get("records_count") is not None else len(recs),
        "full_records": by_via.get("rst_full", 0),
        "missing": meta.get("missing") or [], "full_at": full_at, "checked_at": meta.get("checked_at"), "recent": recent,
        "notes": [n for n in meta.get("notes") or [] if not n.startswith("Поправка") and not n.startswith("Изменение")][:6],
    }


def gost_search(q: str, limit: int = 25) -> dict:
    q = (q or "").strip()
    if not q:
        return {"query": q, "items": []}
    recs = _gost_records()
    key = refdata.norm_designation(q)
    hits = []
    if key in recs:
        hits.append(key)
    low = q.lower()
    for k, r in recs.items():
        if len(hits) >= limit:
            break
        if k in hits:
            continue
        if key and key in k or low in (r.get("title") or "").lower():
            hits.append(k)
    requested = refdata.gost_requested()
    items = []
    for k in hits:
        r = recs[k]
        items.append({"designation": r.get("designation") or k, "key": k, "title": r.get("title"), "status": r.get("status"),
                      "inactive": r.get("status") in refdata.INACTIVE, "replaced_by": r.get("replaced_by"), "use_instead": r.get("use_instead"),
                      "date_intro": r.get("date_intro"), "date_end": r.get("date_end"), "checked_at": r.get("checked_at"),
                      "checked_msk": _msk(r.get("checked_at")), "via": VIA_RU.get(r.get("via"), r.get("via")),
                      "card_url": r.get("check_url") or r.get("source_url"), "watch": bool(r.get("watch")) or k in requested})
    return {"query": q, "key": key, "items": items, "requested": key in requested, "can_watch": bool(key and any(c.isdigit() for c in key))}


LEX_STATUS = {"candidate": "кандидаты", "кандидат": "кандидаты", "active": "активные", "активный": "активные", "активная": "активные",
              "retired": "снятые", "removed": "снятые", "disabled": "снятые", "снят": "снятые", "снятый": "снятые"}


def lexicon_path() -> str | None:
    """Внутренний словарь фраз и брендов (lexicon.jsonl): TZCHECK_LEXICON, затем каталог справочников, затем tzcheck/data."""
    for path in (os.environ.get("TZCHECK_LEXICON"), os.path.join(refdata.data_dir(), "lexicon.jsonl"),
                 os.path.join(refdata.DATA_DIR, "lexicon.jsonl")):
        if path and os.path.exists(path):
            return path
    return None


def _lex_status(r: dict) -> str:
    return LEX_STATUS.get(str(r.get("status", "")).lower(), str(r.get("status") or "без статуса"))


def lexicon_overview(limit: int = 60) -> dict:
    path = lexicon_path()
    if not path:
        return {"path": None, "items": [], "counts": {}, "total": 0, "bad": 0}
    items, bad = [], 0
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    bad += 1
                    continue
                if isinstance(row, dict):
                    items.append(row)
    except OSError:
        return {"path": path, "items": [], "counts": {}, "total": 0, "bad": 0}
    counts = collections.Counter(_lex_status(r) for r in items)
    rows = []
    for r in items[:limit]:
        sup = r.get("support", r.get("supports", r.get("evidence", r.get("опоры"))))
        rows.append({"text": r.get("phrase") or r.get("term") or r.get("text") or r.get("brand") or "",
                     "kind": r.get("kind") or r.get("type") or "", "rule_id": r.get("rule_id") or "", "status": _lex_status(r),
                     "support": len(sup) if isinstance(sup, list) else sup,
                     "support_examples": [str(x)[:120] for x in sup[:3]] if isinstance(sup, list) else []})
    return {"path": path, "items": rows, "counts": dict(counts), "total": len(items), "bad": bad}


def mode() -> dict:
    hb = control.heartbeat()
    runner = os.environ.get("TZCHECK_UPDATE_RUNNER", "app")
    if hb and hb["fresh"]:
        via = "updater"
    elif runner == "updater":
        via = "updater_down"
    else:
        via = "app"
    return {"via": via, "heartbeat": hb, "runner_env": runner, "offline": refdata.offline(), "data_dir": refdata.data_dir(),
            "control_dir": control.control_dir()}


def schedule_info() -> dict:
    s = control.load_settings()
    p = control.plan(s)
    hb = control.heartbeat()
    seen = None
    if hb and hb.get("fresh"):
        hs = hb.get("settings") or {}
        seen = all(hs.get(k) == s.get(k) for k in ("enabled", "at", "full_weekday", "sources"))
    return {"settings": s, "next": p, "updater_next": (hb or {}).get("next_at") if hb and hb.get("fresh") else None,
            "updater_seen": seen, "weekdays": control.WEEKDAYS}


def run_info() -> dict:
    return {"state": control.run_state(), "log": control.log_tail(), "pending": control.pending_request(), "busy": control.busy()}


def status() -> dict:
    return {"sources": source_rows(), "gost": gost_overview(), "mode": mode(), "schedule": schedule_info(), "run": run_info(),
            "history": control.history(), "lexicon": lexicon_overview()}


@router.get("/api/refdata/status")
def api_status():
    return status()


@router.get("/api/refdata/run")
def api_run():
    return run_info()


@router.post("/api/refdata/update")
def api_update(body: dict = Body(default={})):
    sources = body.get("sources") or []
    if isinstance(sources, str):
        sources = [s for s in sources.split(",") if s]
    try:
        req = control.make_request(sources, bool(body.get("full")))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    with _start_lock:
        msg = control.busy()
        if msg:
            return JSONResponse({"error": msg, "run": run_info()}, status_code=409)
        m = mode()
        if m["via"] == "app":
            state = control.start_local(req)
            note = "обновление запущено в процессе веб-приложения"
        else:
            control.queue_request(req)
            state = control.run_state()
            note = ("запрос передан сервису updater через control/request.json" if m["via"] == "updater" else
                    "сервис updater не отвечает: запрос сохранён и выполнится, когда он запустится")
    return JSONResponse({"ok": True, "note": note, "state": state}, status_code=202)


@router.get("/api/refdata/schedule")
def api_schedule():
    return schedule_info()


@router.post("/api/refdata/schedule")
def api_schedule_save(body: dict = Body(...)):
    try:
        control.save_settings(body)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return schedule_info()


@router.get("/api/refdata/gost")
def api_gost(q: str = ""):
    return gost_search(q)


@router.post("/api/refdata/gost/watch")
def api_gost_watch(body: dict = Body(...)):
    key = refdata.norm_designation(str(body.get("designation") or ""))
    if not key or not any(c.isdigit() for c in key):
        return JSONResponse({"error": "нужно обозначение стандарта с номером, например ГОСТ Р 53780-2010"}, status_code=400)
    refdata.note_gost(key, by="manual")
    return {"ok": True, "key": key, "note": "будет проверен поштучно при следующем обновлении каталога Росстандарта"}


# ---------- страница ----------

def e(s) -> str:
    return html.escape("" if s is None else str(s)).replace("«—»", "«нет»")


ICON = {
    "refresh": '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12a9 9 0 1 1-2.64-6.36"/><path d="M21 3v6h-6"/></svg>',
    "clock": '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>',
    "search": '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>',
    "plus": '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg>',
    "link": '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M14 4h6v6"/><path d="M20 4 10 14"/><path d="M19 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1h5"/></svg>',
    "save": '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12l5 5L20 7"/></svg>',
}

CSS = """
@font-face{font-family:Manrope;font-weight:400;font-display:swap;src:url(/static/fonts/manrope-cyrillic-400.woff2) format("woff2");unicode-range:U+0301,U+0400-045F,U+0490-0491,U+04B0-04B1,U+2116}
@font-face{font-family:Manrope;font-weight:400;font-display:swap;src:url(/static/fonts/manrope-latin-400.woff2) format("woff2");unicode-range:U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+2000-206F,U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,U+FEFF,U+FFFD}
@font-face{font-family:Manrope;font-weight:500;font-display:swap;src:url(/static/fonts/manrope-cyrillic-500.woff2) format("woff2");unicode-range:U+0301,U+0400-045F,U+0490-0491,U+04B0-04B1,U+2116}
@font-face{font-family:Manrope;font-weight:500;font-display:swap;src:url(/static/fonts/manrope-latin-500.woff2) format("woff2");unicode-range:U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+2000-206F,U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,U+FEFF,U+FFFD}
@font-face{font-family:Manrope;font-weight:600;font-display:swap;src:url(/static/fonts/manrope-cyrillic-600.woff2) format("woff2");unicode-range:U+0301,U+0400-045F,U+0490-0491,U+04B0-04B1,U+2116}
@font-face{font-family:Manrope;font-weight:600;font-display:swap;src:url(/static/fonts/manrope-latin-600.woff2) format("woff2");unicode-range:U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+2000-206F,U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,U+FEFF,U+FFFD}
:root{--fg:#111418;--muted:#4B5563;--line:#D5D9DF;--bg:#fff;--soft:#F6F7F9;--accent:#1F4FD1;--ink:#111418;--hi:#c92a2a;--mid:#d9480f;--ok:#2b8a3e;--top-h:64px}
*{box-sizing:border-box}body{margin:0;font:400 15px/22px Manrope,system-ui,"Segoe UI",Arial,sans-serif;color:var(--fg);background:var(--bg)}
.wrap{max-width:1180px;margin:0 auto;padding:0 16px 48px}
.top{position:sticky;top:0;z-index:20;height:var(--top-h);display:flex;align-items:center;gap:24px;padding:0 32px;background:rgba(255,255,255,.92);backdrop-filter:saturate(1.4) blur(12px);border-bottom:1px solid var(--line)}
.brand{display:flex;align-items:baseline;gap:12px;text-decoration:none;color:var(--fg);min-width:0}
.brand-name{font-weight:600;font-size:18px;letter-spacing:-.01em}.brand-sub{color:var(--muted);font-size:14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.nav{margin-left:auto;display:flex;gap:4px;overflow-x:auto}
.nav a{display:flex;align-items:center;height:40px;padding:0 14px;border-radius:999px;color:var(--muted);text-decoration:none;font-weight:500;font-size:15px;white-space:nowrap}
.nav a:hover{color:var(--fg);background:var(--soft)}.nav a[aria-current="page"]{color:#fff;background:var(--ink)}
@media(max-width:760px){.top{padding:0 16px;gap:12px}.brand-sub{display:none}.nav a{padding:0 10px;font-size:14px}}
h1{font-size:32px;line-height:40px;font-weight:600;letter-spacing:-.01em;margin:32px 0 8px}h2{font-size:17px;font-weight:600;margin:0 0 12px}h3{font-size:15px;font-weight:600;margin:18px 0 8px}
.muted{color:var(--muted)}.small{font-size:13px}p{line-height:1.45}
section{border:1px solid var(--line);border-radius:12px;padding:18px 22px;margin:16px 0}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:12px;margin:14px 0}
.cell{background:var(--soft);border-radius:8px;padding:12px 14px}.cell .k{font-size:12px;color:var(--muted);margin-bottom:4px}.cell .v{font-size:15px}
table{border-collapse:collapse;width:100%;font-size:14px}th,td{border:1px solid var(--line);padding:7px 8px;vertical-align:top;text-align:left}
th{background:#f0f2f5;font-weight:600}td.n{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;vertical-align:1px;background:var(--muted)}
.s-updated .dot,.s-ok .dot{background:var(--ok)}.s-unchanged .dot{background:#8c96a5}.s-keep .dot,.s-warn .dot{background:var(--mid)}.s-err .dot{background:var(--hi)}.s-run .dot{background:var(--accent)}
.s-keep,.s-warn{color:var(--mid)}.s-err{color:var(--hi)}
button,.btn{display:inline-flex;align-items:center;gap:7px;background:var(--ink);color:#fff;border:0;border-radius:10px;padding:9px 16px;font-size:14px;font-weight:600;cursor:pointer;font-family:inherit}
button.ghost{background:#fff;color:var(--fg);border:1px solid var(--line)}button:disabled{opacity:.55;cursor:default}
select,input[type=text],input[type=time]{font:inherit;font-size:14px;padding:7px 9px;border:1px solid var(--line);border-radius:6px;background:#fff}
.row{display:flex;gap:12px;flex-wrap:wrap;align-items:center;margin:10px 0}label{font-size:14px}
pre.log{background:#f7f8fa;border:1px solid var(--line);border-radius:6px;padding:10px 12px;font-size:12.5px;line-height:1.45;max-height:340px;overflow:auto;white-space:pre-wrap;margin:10px 0 0}
details{margin:8px 0}details summary{cursor:pointer;font-size:14px}details ol,details ul{margin:6px 0 6px;padding-left:22px;font-size:13.5px;line-height:1.5}
code{background:var(--soft);padding:1px 5px;border-radius:4px;font-size:12.5px}
.msg{font-size:14px;margin-top:8px}.spin{animation:sp 1s linear infinite;display:inline-flex}@keyframes sp{to{transform:rotate(360deg)}}
.chain{font-size:13px;color:var(--muted)}a{color:var(--accent)}.srcs{display:flex;gap:14px;flex-wrap:wrap}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:18px}@media(max-width:760px){.cols{grid-template-columns:1fr}section{padding:14px}}
.tablewrap{overflow-x:auto}.nw{white-space:nowrap}code,.url{overflow-wrap:anywhere}table.src td.d{white-space:nowrap}table.src td.d .small{white-space:normal;max-width:150px}
"""

NAV = ('<nav class="tabs"><a href="/#/"{a}>Проверка</a><a href="/#/history">История</a><a href="/#/norms">Нормативная база</a>'
       '<a href="/refdata"{b}>Справочники</a></nav>')


TOP = ('<header class="top"><a class="brand" href="/#/"><span class="brand-name">Чекни!</span><span class="brand-sub">Проверка ТЗ по 223-ФЗ</span></a>'
       '<nav class="nav"><a href="/#/">Проверка</a><a href="/#/history">История</a><a href="/#/norms">Нормативная база</a>'
       '<a href="/refdata" aria-current="page">Справочники</a></nav></header>')


def nav(active: str) -> str:
    return NAV.format(a=' class="on"' if active == "check" else "", b=' class="on"' if active == "refdata" else "")


def _status_cls(status: str | None) -> str:
    return {"updated": "s-updated", "unchanged": "s-unchanged", "keep": "s-keep"}.get(status or "", "")


def _mode_block(m: dict, sch: dict) -> str:
    hb = m["heartbeat"]
    if m["via"] == "updater":
        upd = '<span class="s-ok"><span class="dot"></span></span>сервис обновления на связи.'
    elif m["via"] == "updater_down":
        ago = f", последний ответ {hb['age']} с назад" if hb else ""
        upd = (f'<span class="s-warn"><span class="dot"></span></span>сервис обновления не отвечает{ago}. '
               'Запрос «Обновить сейчас» выполнится после его запуска.')
    else:
        upd = '<span class="s-warn"><span class="dot"></span></span>только вручную: сервис обновления не запущен, расписание не работает.'
    check = ("только локальные копии справочников, без запросов в сеть" if m["offline"] else
             "если стандарта или прибора нет в копии, приложение само спрашивает rst.gov.ru и ФГИС «Аршин»")
    return (f'<div class="grid"><div class="cell"><div class="k">Обновление справочников</div><div class="v small">{upd}</div></div>'
            f'<div class="cell"><div class="k">Проверка ТЗ</div><div class="v small">{check}.</div></div></div>')


def _next_text(sch: dict) -> str:
    p = sch["next"]
    if not p:
        return "ночное обновление выключено"
    dt = datetime.datetime.fromisoformat(p["at"])
    return f"{dt:%d.%m.%Y %H:%M}" + (", полный обход" if p["full"] else "")


def _sources_table(rows: list[dict]) -> str:
    out = ['<div class="tablewrap"><table class="src"><thead><tr><th style="width:24%">Справочник</th><th>Проверено</th><th>Данные менялись</th>'
           '<th style="width:22%">Источник</th><th>Записей</th><th>Изменения</th>'
           '<th style="width:20%">Итог</th></tr></thead><tbody>']
    for r in rows:
        c = r["counts"] or {}
        if not r["exists"]:
            result = '<span class="s-keep"><span class="dot"></span>не загружен</span>'
        else:
            result = f'<span class="{_status_cls(r["status"])}"><span class="dot"></span>{e(r["status_ru"])}</span>'
            if r["reason"]:
                result += f'<div class="small muted">{e(r["reason"][:300])}</div>'
            if r["seconds"] is not None:
                result += f'<div class="small muted">{_sec(r["seconds"])} с' + (", полный обход" if r["mode"] == "full" else "") + "</div>"
        if r["warning"]:
            result += f'<div class="small s-warn">{e(r["warning"])}</div>'
        changed = _msk(r["changed_at"]) if r["changed_at"] else "нет данных"
        if r["changed_at"] and not r["changed_from_journal"]:
            changed += '<div class="small muted">время получения файла</div>'
        via = e(_via(r["via_title"]) or "нет данных")
        if r["source_url"]:
            via += f'<div class="small url"><a href="{e(r["source_url"])}" target="_blank" rel="noopener">{e(r["source_url"])}</a></div>'
        out.append(f'<tr><td><b>{e(r["title"])}</b><div class="small muted">{e(r["purpose"])}</div></td>'
                   f'<td class="d">{_msk(r["checked_at"]) or "нет данных"}</td><td class="d">{changed}</td><td>{via}</td>'
                   f'<td class="n">{e(r["records"]) if r["records"] is not None else "0"}</td>'
                   f'<td class="n">{_counts(c)}</td>'
                   f'<td>{result}</td></tr>')
    out.append("</tbody></table></div>")
    return "".join(out)


def _local(stamp: str | None) -> str:
    try:
        return f"{datetime.datetime.fromisoformat(stamp):%d.%m.%Y %H:%M}"
    except (TypeError, ValueError):
        return ""


def _counts(c: dict) -> str:
    if not c:
        return '<span class="muted">нет</span>'
    parts = [f"{w} {c.get(k)}" for k, w in (("added", "добавлено"), ("removed", "удалено"), ("changed", "изменено")) if c.get(k)]
    return "<br>".join(parts) or '<span class="muted">нет</span>'


def _sec(v) -> str:
    try:
        return f"{float(v):.1f}".rstrip("0").rstrip(".").replace(".", ",")
    except (TypeError, ValueError):
        return str(v)


def _via(s: str | None) -> str:
    """Подпись источника без служебных деталей: протокол, порт, имена библиотек; «·» заменяется запятой."""
    s = re.sub(r"\s*\((?:HTTP|Playwright[^)]*)\)", "", str(s or ""))
    return re.sub(r",?\s*API :\d+", "", s).replace(" API eapi", " API").replace(" · ", ", ").strip()


def plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(int(n or 0))
    if n % 10 == 1 and n % 100 != 11:
        return one
    return few if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else many


def _coverage(g: dict) -> str:
    if g["full_records"]:
        return (f'<span class="s-ok"><span class="dot"></span></span>Копия собрана полным обходом каталога '
                f'{_msk(g["full_at"]) or "(дата в журнале не найдена)"}, записей: {g["full_records"]}.')
    return ('<span class="s-warn"><span class="dot"></span></span>Копия покрывает не весь каталог: только отслеживаемые '
            'стандарты и новые из лент protect.gost.ru.')


def _gost_block(g: dict) -> str:
    groups = "".join(f'<tr><td>{e(x["title"])}</td><td class="n">{x["count"]}</td></tr>' for x in g["groups"])
    statuses = "".join(f'<tr><td>{e(s)}</td><td class="n">{n}</td><td class="n">{g["by_status_watch"].get(s, 0)}</td></tr>' for s, n in g["by_status"])
    vias = "".join(f'<tr><td>{e(v)}</td><td class="n">{n}</td></tr>' for v, n in g["by_via"])
    feed = ", ".join(f"{m}: {n}" for m, n in g["feed"]) or "нет"
    recent = "".join(f'<tr><td class="n">{_msk(x["at"])}</td><td>{e(x["line"])}</td></tr>' for x in g["recent"][:25])
    if not recent:
        recent = '<tr><td colspan="2" class="muted">в журнале пока нет изменений</td></tr>'
    missing = ", ".join(e(x) for x in g["missing"]) or "нет"
    notes = "".join(f"<li>{e(n)}</li>" for n in g["notes"])
    full = _msk(g["full_at"]) if g["full_at"] else "ещё не выполнялся"
    return f"""
<section id="gost"><h2>Каталог Росстандарта</h2>
<p>Известно {g["known"]} {plural(g["known"], "стандарт", "стандарта", "стандартов")}.</p><p class="small">{_coverage(g)}</p>
<p class="small muted">Проверено {_msk(g["checked_at"]) or "нет данных"}. Отслеживается {g["targets"]} {plural(g["targets"], "обозначение", "обозначения", "обозначений")}.
Полный обход каталога: {full}.</p>
<div class="cols"><div><h3>Что отслеживается</h3><table><thead><tr><th>Источник обозначения</th><th>Обозначений</th></tr></thead><tbody>{groups}
<tr><td>новые стандарты из лент protect.gost.ru ({feed})</td><td class="n">{sum(n for _, n in g["feed"])}</td></tr></tbody></table>
<p class="small muted">Нет в каталоге при последней проверке: {missing}.</p>
<h3>Как получены записи</h3><table><tbody>{vias}</tbody></table>
<ul class="small muted">{notes}</ul></div>
<div><h3>Статусы</h3><table><thead><tr><th>Статус</th><th>Всего в копии</th><th>Из отслеживаемых</th></tr></thead><tbody>{statuses}</tbody></table></div></div>
<h3>Поиск стандарта</h3>
<form id="gq" class="row" onsubmit="return false"><input type="text" id="gq-q" placeholder="ГОСТ Р 53780-2010 или часть названия" style="min-width:320px">
<button type="submit" id="gq-go">{ICON["search"]}Найти</button></form><div id="gq-out"></div>
<h3>Последние изменения</h3><div class="tablewrap" style="max-height:340px;overflow:auto"><table><tbody>{recent}</tbody></table></div>
</section>"""


def _how_block(rows: list[dict]) -> str:
    how = _how()
    parts = []
    for r in rows:
        h = how.get(r["name"], {})
        steps = []
        for i, (link_id, title) in enumerate(r["chain"], 1):
            reqs = "".join(f"<li>{e(_no_env(x))}</li>" for x in h.get("links", {}).get(link_id, []))
            steps.append(f'<li><b>{e(title)}</b><ul>{reqs}</ul></li>')
        steps.append("<li><b>прежняя копия</b>, если пути выше не сработали или данные не прошли проверку</li>")
        always = "".join(f"<li>{e(_no_env(x))}</li>" for x in h.get("always", []))
        always = f'<p class="small">При каждом запуске:</p><ul>{always}</ul>' if always else ""
        parts.append(f"""<details><summary><b>{e(r["title"])}</b> <span class="chain">{" → ".join(e(_via(t.split(" (")[0])) for _, t in r["chain"])} → прежняя копия</span></summary>
<ol>{"".join(steps)}</ol>{always}
<p class="small"><b>Что уходит наружу:</b> {e(h.get("sends", ""))}.</p></details>""")
    return '<section id="how"><h2>Как обновляется</h2>' + "".join(parts) + "</section>"


def _no_env(text: str) -> str:
    return re.sub(r"\s*\((?:[^()]*\s)?TZCHECK_[A-Z0-9_]+\)", "", text)


def _schedule_block(sch: dict) -> str:
    s = sch["settings"]
    wd = "".join(f'<option value="{i}"{" selected" if s["full_weekday"] == i else ""}>{d}</option>' for i, d in enumerate(sch["weekdays"]))
    wd += f'<option value="-1"{" selected" if s["full_weekday"] == -1 else ""}>не делать</option>'
    srcs = "".join(f'<label><input type="checkbox" name="src" value="{n}"{" checked" if n in s["sources"] else ""}> {e(t)}</label>'
                   for n, t in _short_names().items())
    if sch["updater_next"]:
        seen = ("сервис обновления принял настройки" if sch["updater_seen"] else "сервис обновления ещё не перечитал настройки")
        confirm = f'<div class="small muted" id="sch-seen">{seen}; по его часам следующее обновление {_local(sch["updater_next"])}</div>'
    else:
        confirm = '<div class="small muted" id="sch-seen">Сервис обновления не запущен, настройки применятся после его запуска.</div>'
    saved = f'Сохранено {_local(s["saved_at"])}' if s.get("saved_at") else "Настройки по умолчанию"
    return f"""<section id="schedule"><h2>{ICON["clock"]} Расписание</h2>
<form id="sch" class="row" onsubmit="return false">
<label><input type="checkbox" id="sch-on"{" checked" if s["enabled"] else ""}> обновлять каждый день</label>
<label>в <input type="time" id="sch-at" value="{e(s["at"])}" required> по Москве</label>
<label>полный обход каталогов <select id="sch-wd">{wd}</select></label></form>
<div class="row srcs small" id="sch-src">{srcs}</div>
<div class="row"><button type="button" id="sch-save">{ICON["save"]}Сохранить</button><span class="msg" id="sch-msg"></span></div>
<div class="small">{"Следующее обновление" if sch["updater_next"] else "По расписанию"}: <b id="sch-next">{_next_text(sch)}</b>. <span class="muted">{saved}.</span></div>
{confirm}</section>"""


SHORT = {"okpd2": "ОКПД 2", "laws": "законы", "eaeu": "перечни ЕАЭС", "gost": "каталог Росстандарта", "arshin": "типы СИ «Аршин»", "ktru": "КТРУ", "gost_texts": "тексты редакций ГОСТ"}


def _short_names() -> dict:
    return {n: SHORT.get(n) or SOURCES[n].title for n in control.SOURCE_ORDER if n in SOURCES or n in SHORT}


def _run_block(run: dict) -> str:
    opts = '<option value="">все справочники</option>' + "".join(f'<option value="{n}">{e(t)}</option>' for n, t in _short_names().items())
    return f"""<section id="run"><h2>Ручное обновление</h2>
<div class="row"><label>Что обновить <select id="run-src">{opts}</select></label>
<label><input type="checkbox" id="run-full"> полный обход, около часа</label>
<button type="button" id="run-go">{ICON["refresh"]}Обновить сейчас</button></div>
<div id="run-state" class="msg"></div><pre class="log" id="run-log" hidden></pre></section>"""


def _lexicon_block(lx: dict) -> str:
    head = '<section id="lexicon"><h2>Словарь фраз и брендов</h2>'
    if not lx.get("total"):
        return head + '<p class="small muted">Словарь пуст.</p></section>'
    order = ["кандидаты", "активные", "снятые"]
    counts = ", ".join(f"{k} {lx['counts'].get(k, 0)}" for k in order)
    other = {k: v for k, v in lx["counts"].items() if k not in order}
    if other:
        counts += ", " + ", ".join(f"{e(k)} {v}" for k, v in other.items())
    rows = "".join(f'<tr><td>{e(r["text"])}</td><td>{e(r["kind"])}</td><td>{e(r["status"])}</td>'
                   f'<td class="n">{e(r["support"] if r["support"] is not None else "")}</td>'
                   f'<td class="small muted">{"<br>".join(e(x) for x in r["support_examples"])}</td></tr>' for r in lx["items"])
    more = f'<p class="small muted">Показаны первые {len(lx["items"])} из {lx["total"]}.</p>' if lx["total"] > len(lx["items"]) else ""
    bad = f'<p class="small s-warn">Строк, которые не удалось прочитать: {lx["bad"]}.</p>' if lx["bad"] else ""
    return (head + f'<p class="small">Записей {lx["total"]}: {counts}.</p>{bad}'
            '<div class="tablewrap"><table><thead><tr><th>Фраза или бренд</th><th>Вид</th><th>Статус</th><th>Опор</th>'
            f'<th>Примеры опор</th></tr></thead><tbody>{rows}</tbody></table></div>{more}</section>')


def _history_block(hist: list[dict]) -> str:
    if not hist:
        return ""
    rows = []
    for h in hist:
        cls = "s-ok" if h.get("code") == 0 else ("s-keep" if h.get("code") in (1, 3) else "s-err")
        rows.append(f'<tr><td class="n">{_local(h.get("started_at"))}</td><td>{e(TRIGGER_RU.get(h.get("trigger"), h.get("trigger")))}</td>'
                    f'<td>{e(", ".join(_short_names().get(s, s) for s in h.get("sources") or []))}{", полный обход" if h.get("full") else ""}</td>'
                    f'<td>{e("сервис обновления" if h.get("runner") == "updater" else "веб-приложение")}</td><td class="n">{_sec(h.get("seconds"))} с</td>'
                    f'<td><span class="{cls}"><span class="dot"></span>{e(h.get("outcome"))}</span></td></tr>')
    return ('<section id="history"><h2>Журнал запусков</h2><div class="tablewrap"><table><thead><tr><th>Начало</th><th>Как запущено</th><th>Что</th>'
            '<th>Где выполнялось</th><th>Длилось</th><th>Итог</th></tr></thead><tbody>' + "".join(rows) + "</tbody></table></div></section>")


JS = """<script>
const SHORT = %SHORT%;
const TRIGGER = {manual: 'вручную', schedule: 'по расписанию', start: 'при запуске сервиса'};
const ICON_SPIN = '<span class="spin"><svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M21 12a9 9 0 1 1-6.2-8.56"/></svg></span>';
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])).replace(/«—»/g, '«нет»');
let polling = null, wasActive = false;
function hhmm(s) { return s ? s.slice(11, 16) : ''; }
function dmy(s) { return s ? s.slice(8, 10) + '.' + s.slice(5, 7) + '.' + s.slice(0, 4) + ' ' + s.slice(11, 16) : ''; }
function secsSince(s) { return s ? Math.max(0, Math.round((Date.now() - new Date(s).getTime()) / 1000)) : 0; }
function renderRun(info) {
  const st = info.state, box = $('run-state'), log = $('run-log'), go = $('run-go');
  if (!st) { box.innerHTML = '<span class="muted">Ручных запусков ещё не было.</span>'; return false; }
  const what = (st.sources || []).map(s => SHORT[s] || s).join(', ') + (st.full ? ', полный обход' : '');
  let html = '';
  const active = st.state === 'running' || st.state === 'queued';
  if (st.state === 'running') html = '<span class="s-run">' + ICON_SPIN + '</span> Идёт обновление: ' + esc(what) + ', начато ' + hhmm(st.started_at) + ', ' + secsSince(st.started_at) + ' с';
  else if (st.state === 'queued') html = '<span class="s-run">' + ICON_SPIN + '</span> Запрос передан сервису обновления (' + esc(what) + ').';
  else if (st.state === 'lost') html = '<span class="s-err"><span class="dot"></span>' + esc(st.outcome) + '</span>';
  else {
    const cls = st.code === 0 ? 's-ok' : (st.code === 1 || st.code === 3 ? 's-keep' : 's-err');
    html = '<span class="' + cls + '"><span class="dot"></span>Последний запуск ' + (TRIGGER[st.trigger] || '') + ' (' + esc(what) + '), ' + esc(dmy(st.finished_at)) + ', ' + esc(String(st.seconds).replace('.', ',')) + ' с: ' + esc(st.outcome) + '</span>';
  }
  box.innerHTML = html;
  if (info.log && info.log.length) { log.hidden = false; const atEnd = log.scrollTop + log.clientHeight >= log.scrollHeight - 8; log.textContent = info.log.join('\\n'); if (atEnd || active) log.scrollTop = log.scrollHeight; }
  go.disabled = active;
  return active;
}
async function poll() {
  const r = await fetch('/api/refdata/run'); const info = await r.json();
  const active = renderRun(info);
  if (active) { wasActive = true; polling = setTimeout(poll, 1500); }
  else if (wasActive) { wasActive = false; $('run-state').insertAdjacentHTML('beforeend', ' <span class="muted">Таблица обновится через 2 с.</span>'); setTimeout(() => location.reload(), 2000); }
}
$('run-go').addEventListener('click', async () => {
  const src = $('run-src').value;
  const body = {sources: src ? [src] : [], full: $('run-full').checked};
  $('run-go').disabled = true;
  const r = await fetch('/api/refdata/update', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  const j = await r.json();
  if (!r.ok) { $('run-state').innerHTML = '<span class="s-keep"><span class="dot"></span>' + esc(j.error) + '</span>'; $('run-go').disabled = false; if (r.status === 409) { wasActive = true; setTimeout(poll, 1500); } return; }
  wasActive = true; clearTimeout(polling); poll();
});
$('sch-save').addEventListener('click', async () => {
  const sources = [...document.querySelectorAll('#sch-src input:checked')].map(x => x.value);
  const body = {enabled: $('sch-on').checked, at: $('sch-at').value, full_weekday: parseInt($('sch-wd').value, 10), sources};
  const r = await fetch('/api/refdata/schedule', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  const j = await r.json();
  if (!r.ok) { $('sch-msg').innerHTML = '<span class="s-err">' + esc(j.error) + '</span>'; return; }
  const p = j.next;
  $('sch-next').textContent = p ? (p.at.slice(8, 10) + '.' + p.at.slice(5, 7) + '.' + p.at.slice(0, 4) + ' ' + p.at.slice(11, 16) + (p.full ? ', полный обход' : '')) : 'ночное обновление выключено';
  $('sch-msg').innerHTML = '<span class="s-ok"><span class="dot"></span>Сохранено</span>';
  if (j.updater_next !== null || j.updater_seen !== null) setTimeout(checkSeen, 4000);
});
async function checkSeen() {
  const j = await (await fetch('/api/refdata/schedule')).json();
  if (j.updater_seen === null) return;
  $('sch-seen').textContent = (j.updater_seen ? 'сервис обновления принял настройки' : 'сервис обновления ещё не перечитал настройки') + (j.updater_next ? '; по его часам следующее обновление ' + dmy(j.updater_next) : '');
  if (!j.updater_seen) setTimeout(checkSeen, 3000);
}
async function search() {
  const q = $('gq-q').value.trim(); if (!q) return;
  const j = await (await fetch('/api/refdata/gost?q=' + encodeURIComponent(q))).json();
  const out = $('gq-out');
  if (!j.items.length) {
    out.innerHTML = '<p class="small">В локальной копии нет «' + esc(q) + '».' + (j.can_watch ? (j.requested ? ' Уже в списке наблюдения: проверится при следующем обновлении.' : ' <button type="button" class="ghost" id="gq-watch">' + %PLUS% + 'Проверять при обновлении</button>') : '') + '</p>';
    const b = $('gq-watch'); if (b) b.addEventListener('click', () => watch(j.key || q, b));
    return;
  }
  let h = '<div class="tablewrap"><table><thead><tr><th>Обозначение</th><th>Статус</th><th>Заменяющий</th><th>Введён</th><th>Прекращён</th><th>Проверено</th><th>Откуда</th></tr></thead><tbody>';
  for (const it of j.items) {
    const cls = it.inactive ? 's-err' : (it.status === 'Действует' ? 's-ok' : '');
    h += '<tr><td><b>' + esc(it.designation) + '</b>' + (it.card_url ? ' <a href="' + esc(it.card_url) + '" target="_blank" rel="noopener" title="карточка в каталоге">' + %LINK% + '</a>' : '') + '<div class="small muted">' + esc((it.title || '').slice(0, 160)) + '</div></td>'
      + '<td><span class="nw ' + cls + '"><span class="dot"></span>' + esc(it.status || 'не указан') + '</span></td>'
      + '<td>' + esc(it.replaced_by || '') + (it.use_instead && it.use_instead !== it.replaced_by ? '<div class="small muted">на территории РФ пользоваться: ' + esc(it.use_instead) + '</div>' : '') + '</td><td class="n">' + esc(it.date_intro || '') + '</td><td class="n">' + esc(it.date_end || '') + '</td>'
      + '<td class="n">' + esc(it.checked_msk) + '</td><td class="small">' + esc(it.via || '') + (it.watch ? '<div class="muted">отслеживается</div>' : '') + '</td></tr>';
  }
  out.innerHTML = h + '</tbody></table></div>';
}
async function watch(key, btn) {
  const r = await fetch('/api/refdata/gost/watch', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({designation: key})});
  const j = await r.json();
  btn.outerHTML = r.ok ? '<span class="s-ok"><span class="dot"></span>' + esc(j.key) + ': ' + esc(j.note) + '</span>' : '<span class="s-err">' + esc(j.error) + '</span>';
}
$('gq-go').addEventListener('click', search);
poll();
</script>"""


def page() -> str:
    st = status()
    rows, g, m, sch = st["sources"], st["gost"], st["mode"], st["schedule"]
    last = max((r["checked_at"] for r in rows if r["checked_at"]), default=None)
    js = (JS.replace("%SHORT%", json.dumps(_short_names(), ensure_ascii=False)).replace("%PLUS%", json.dumps(ICON["plus"]))
          .replace("%LINK%", json.dumps(ICON["link"])))
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Справочники</title><style>{CSS}</style></head><body>{TOP}<main class="wrap">
<h1>Справочники</h1>
<p class="muted small">Последняя проверка источников {_msk(last) or "не выполнялась"}{f", следующее обновление {_next_text(sch)}" if m["via"] == "updater" else ""}.</p>
{_mode_block(m, sch)}
{_run_block(st["run"])}
<section id="sources"><h2>Все справочники</h2>{_sources_table(rows)}</section>
{_gost_block(g)}
{_schedule_block(sch)}
{_how_block(rows)}
{_lexicon_block(st["lexicon"])}
{_history_block(st["history"])}
</main>{js}</body></html>"""


@router.get("/refdata", response_class=HTMLResponse)
def refdata_page():
    return page()
