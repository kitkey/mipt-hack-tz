import json
import os
import shutil
import tempfile
import uuid

from fastapi import FastAPI, File, Form, Query, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import api_ui, feedback, jobs, llm, refdata_ui
from .norms import parse_checks
from .checks import succeeded
from .pipeline import ORCHESTRATORS, check, compare
from .report import to_compare_html, to_html, write_all


def _flag(v) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


app = FastAPI(title="Проверка ТЗ на закупку", version="0.2.0")
app.include_router(refdata_ui.router)
app.include_router(api_ui.router)
app.include_router(__import__("tzcheck.gost_profiles", fromlist=["router"]).router)  # чеклисты ГОСТ: список и утверждение
WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
if os.path.isdir(WEB):
    app.mount("/static", StaticFiles(directory=WEB), name="static")
STORE = os.environ.get("TZCHECK_STORE", os.path.join(tempfile.gettempdir(), "tzcheck_runs"))
os.makedirs(STORE, exist_ok=True)

INDEX = """<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>Проверка ТЗ</title>
<style>body{font-family:system-ui,Segoe UI,Arial,sans-serif;max-width:780px;margin:48px auto;padding:0 16px;color:#1d1d1f;background:#fff}
form{border:1px solid #dcdfe5;border-radius:10px;padding:20px 24px;margin:18px 0}button{background:#3b5bdb;color:#fff;border:0;border-radius:6px;padding:10px 18px;font-size:15px;cursor:pointer}
.muted{color:#666;font-size:14px}h2{font-size:17px;margin:0 0 10px}
nav.tabs{display:flex;gap:4px;border-bottom:1px solid #dcdfe5;margin:-28px 0 22px}nav.tabs a{padding:10px 14px;color:#666;text-decoration:none;border-bottom:2px solid transparent;margin-bottom:-1px;font-size:15px}
nav.tabs a.on{color:#1d1d1f;border-bottom-color:#3b5bdb;font-weight:600}</style></head><body>
{nav}
<h1>Проверка ТЗ по 223-ФЗ</h1>
<form action="/check" method="post" enctype="multipart/form-data"><h2>Проверить ТЗ</h2>
<p>Основное ТЗ: <input type="file" name="file" accept=".docx,.doc,.rtf,.odt,.pdf,.xlsx,.txt,.png,.jpg,.jpeg,.tif,.tiff,.bmp,.webp" required></p>
<p>Приложения: <input type="file" name="attachments" accept=".docx,.doc,.rtf,.odt,.pdf,.xlsx,.txt,.png,.jpg,.jpeg,.tif,.tiff,.bmp,.webp" multiple></p>
<p><label><input type="checkbox" name="use_llm" value="1" {checked}> использовать языковую модель {model}</label></p>
<p><label>Оркестрация: <select name="orchestrator"><option value="linear">линейный конвейер</option>
<option value="graph">граф, повторная проверка при низкой уверенности</option></select></label></p>
<p>Проверки (ничего не отмечено = полная): <label><input type="checkbox" name="checks" value="logic"> Логика</label>
<label><input type="checkbox" name="checks" value="standards"> Стандарты</label> <label><input type="checkbox" name="checks" value="risks"> Риски</label></p>
<p><label>Пользователь: <input type="text" name="user" placeholder="необязательно"></label></p>
<p><label><input type="checkbox" name="public" value="1"> Документ открытый (например, опубликован в ЕИС)</label></p>
<button type="submit">Проверить</button></form>
<form action="/compare" method="post" enctype="multipart/form-data"><h2>Сравнить версии после исправления</h2>
<p>Было: <input type="file" name="old" accept=".docx,.doc,.pdf,.txt" required></p>
<p>Стало: <input type="file" name="new" accept=".docx,.doc,.pdf,.txt" required></p>
<p><label><input type="checkbox" name="use_llm" value="1"> использовать языковую модель</label></p>
<button type="submit">Сравнить</button></form>
<p class="muted"><a href="/api/feedback/stats">Решения закупщиков по шагам проверки</a></p></body></html>"""


class FeedbackIn(BaseModel):
    run_id: str
    finding_id: str
    decision: str
    comment: str = ""


@app.get("/", response_class=HTMLResponse)
def index():
    page = os.path.join(WEB, "index.html")
    if os.path.isfile(page):
        return FileResponse(page, media_type="text/html")
    return classic()


@app.get("/classic", response_class=HTMLResponse)
def classic():
    model = os.environ.get("TZCHECK_LLM_MODEL", "")
    return INDEX.replace("{nav}", refdata_ui.nav("check")).replace("{checked}", "checked" if model else "disabled").replace("{model}", f"({model})" if model else "(не настроена)")


@app.get("/health")
def health():
    return {"status": "ok", "llm": llm.enabled(), "model": os.environ.get("TZCHECK_LLM_MODEL", "")}


def _save(file: UploadFile, folder: str) -> str:
    name = os.path.basename(file.filename or "document.docx")
    src = os.path.join(folder, name)
    with open(src, "wb") as f:
        shutil.copyfileobj(file.file, f)
    return src


def _run(file: UploadFile, use_llm: bool, attachments: list[UploadFile] | None = None, orchestrator: str | None = None,
         checks=None, user: str = "", public: bool = False):
    rid = uuid.uuid4().hex[:12]
    folder = os.path.join(STORE, rid)
    os.makedirs(folder, exist_ok=True)
    src = _save(file, folder)
    paths = [src]
    for i, attachment in enumerate(attachments or [], 1):
        if not attachment.filename:
            continue
        folder_part = os.path.join(folder, f"attachment_{i}")
        os.makedirs(folder_part, exist_ok=True)
        paths.append(_save(attachment, folder_part))
    # отчёты, файл с правками (revisions.docx) и паспорт запуска для истории пишет jobs.execute_check
    res = jobs.execute_check(STORE, rid, folder, paths, use_llm and llm.enabled(), orchestrator, parse_checks(checks), (user or "").strip()[:120],
                             public=public)
    return res, folder, os.path.basename(src)


@app.post("/check", response_class=HTMLResponse)
def check_form(file: UploadFile = File(...), use_llm: str = Form("0"), attachments: list[UploadFile] = File(default=[]),
               orchestrator: str = Form("linear"), checks: list[str] = Form(default=[]), user: str = Form(""), public: str = Form("0")):
    if orchestrator not in ORCHESTRATORS:
        return HTMLResponse(f"<p>Ошибка: неизвестная оркестрация {orchestrator}</p><p><a href='/classic'>Назад</a></p>", status_code=400)
    try:
        groups = parse_checks(checks or None)
    except ValueError as e:
        return HTMLResponse(f"<p>Ошибка: {e}</p><p><a href='/classic'>Назад</a></p>", status_code=400)
    res, folder, name = _run(file, use_llm == "1", attachments, orchestrator, groups, user, _flag(public))
    if not succeeded(res):
        return HTMLResponse(f"<p>Ошибка: {res['error']}</p><p><a href='/classic'>Назад</a></p>", status_code=400)
    base = os.path.splitext(name)[0]
    links = f"<p><a href='/runs/{res['run_id']}/{base}.report.json'>JSON</a>"
    if os.path.exists(os.path.join(folder, base + ".comments.docx")):
        links += f"&emsp;<a href='/runs/{res['run_id']}/{base}.comments.docx'>ТЗ с комментариями (docx)</a>"
    if os.path.exists(os.path.join(folder, base + ".revisions.docx")):
        links += f"&emsp;<a href='/runs/{res['run_id']}/{base}.revisions.docx'>ТЗ с предлагаемыми правками (docx)</a>"
    for i, _ in enumerate(attachments or [], 1):
        attachment_name = f"attachment_{i}.comments.docx"
        if os.path.exists(os.path.join(folder, attachment_name)):
            links += f"&emsp;<a href='/runs/{res['run_id']}/{attachment_name}'>Приложение {i} с комментариями</a>"
    links += "&emsp;<a href='/classic'>Проверить другой</a></p>"
    page = to_html(res, run_id=res["run_id"])
    return page.replace("<body>", "<body>" + links, 1)


@app.post("/api/check")
def check_api(file: UploadFile = File(...), use_llm: bool = True, attachments: list[UploadFile] = File(default=[]),
              orchestrator: str = "linear", checks: list[str] | None = Query(None), checks_form: list[str] | None = Form(None, alias="checks"),
              user: str = "", user_form: str | None = Form(None, alias="user"),
              public: bool = False, public_form: str | None = Form(None, alias="public")):
    if orchestrator not in ORCHESTRATORS:
        return JSONResponse({"error": f"orchestrator must be one of {list(ORCHESTRATORS)}"}, status_code=400)
    try:
        groups = parse_checks((checks_form or []) + (checks or []) or None)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    res, _, _ = _run(file, use_llm, attachments, orchestrator, groups, user_form or user, public or _flag(public_form))
    return JSONResponse({k: v for k, v in res.items() if not k.startswith("_")}, status_code=200 if succeeded(res) else 400)


def _compare(old: UploadFile, new: UploadFile, use_llm: bool):
    rid = uuid.uuid4().hex[:12]
    folder = os.path.join(STORE, rid)
    os.makedirs(os.path.join(folder, "old"), exist_ok=True)
    os.makedirs(os.path.join(folder, "new"), exist_ok=True)
    a, b = _save(old, os.path.join(folder, "old")), _save(new, os.path.join(folder, "new"))
    return compare(a, b, use_llm=use_llm and llm.enabled())


@app.post("/compare", response_class=HTMLResponse)
def compare_form(old: UploadFile = File(...), new: UploadFile = File(...), use_llm: str = Form("0")):
    cmp = _compare(old, new, use_llm == "1")
    if cmp["status"] != "ok":
        return HTMLResponse(f"<p>Ошибка: {cmp}</p><p><a href='/classic'>Назад</a></p>", status_code=400)
    return to_compare_html(cmp).replace("<body>", "<body><p><a href='/classic'>На главную</a></p>", 1)


@app.post("/api/compare")
def compare_api(old: UploadFile = File(...), new: UploadFile = File(...), use_llm: bool = False):
    cmp = _compare(old, new, use_llm)
    return JSONResponse({k: v for k, v in cmp.items() if not k.startswith("_")}, status_code=200 if cmp["status"] == "ok" else 400)


@app.post("/api/feedback")
def feedback_api(body: FeedbackIn):
    folder = os.path.realpath(os.path.join(STORE, body.run_id))
    if not folder.startswith(os.path.realpath(STORE)) or not os.path.isdir(folder):
        return JSONResponse({"error": "run not found"}, status_code=404)
    report = next((os.path.join(folder, f) for f in os.listdir(folder) if f.endswith(".report.json")), None)
    if report is None:
        return JSONResponse({"error": "report not found"}, status_code=404)
    res = json.load(open(report, encoding="utf-8"))
    finding = next((f for f in res.get("findings", []) if f["id"] == body.finding_id), None)
    if finding is None:
        return JSONResponse({"error": "finding not found"}, status_code=404)
    try:
        row = feedback.record(body.run_id, res.get("file", ""), finding, body.decision, body.comment, res.get("customer", ""))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return row


@app.get("/api/feedback/stats")
def feedback_stats():
    return feedback.stats()


@app.get("/runs/{rid}/{fname}")
def download(rid: str, fname: str):
    # наружу только артефакты проверки (отчёты, файлы с комментариями и правками); исходник, приложения и run.json не отдаются
    from .history import artifact_path
    path = artifact_path(STORE, rid, fname)
    if path is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(path, filename=fname)
