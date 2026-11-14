import glob
import json
import os
from datetime import datetime, timedelta, timezone

from .. import refdata
from .base import write_atomic, write_json

MSK = timezone(timedelta(hours=3))
STATUS_RU = {"updated": "обновлено", "unchanged": "без изменений", "keep": "источники недоступны, оставлен текущий файл"}
MAX_LINES = 40


def updates_dir() -> str:
    return os.path.join(refdata.data_dir(), "updates")


def today_msk() -> str:
    return datetime.now(MSK).strftime("%Y-%m-%d")


def write_run(source: str, title: str, run: dict) -> str:
    path = os.path.join(updates_dir(), f"{today_msk()}_{source}.json")
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        doc = {"source": source, "title": title, "date": today_msk(), "runs": []}
    doc["runs"].append(run)
    write_json(path, doc)
    return path


def _latest(source: str):
    files = sorted(glob.glob(os.path.join(updates_dir(), f"????-??-??_{source}.json")))
    for path in reversed(files):
        try:
            with open(path, encoding="utf-8") as f:
                doc = json.load(f)
        except (OSError, ValueError):
            continue
        if doc.get("runs"):
            return path, doc
    return None, None


def _last_with_changes(source: str):
    files = sorted(glob.glob(os.path.join(updates_dir(), f"????-??-??_{source}.json")))
    for path in reversed(files):
        try:
            with open(path, encoding="utf-8") as f:
                doc = json.load(f)
        except (OSError, ValueError):
            continue
        for run in reversed(doc.get("runs", [])):
            if run.get("lines"):
                return path, run
    return None, None


def write_latest(order: list[str], titles: dict) -> str:
    rows, sections = [], []
    last_at = None
    for name in order:
        path, doc = _latest(name)
        if not doc:
            continue
        run = doc["runs"][-1]
        last_at = max(last_at or run["at"], run["at"])
        c = run.get("counts", {})
        title = doc.get("title") or titles.get(name, name)
        rows.append(f"| {title} | {refdata.msk(run['at'], True)} | {run.get('via_title', '')} | {c.get('after', 0)} | "
                    f"{c.get('added', 0)} | {c.get('removed', 0)} | {c.get('changed', 0)} | {STATUS_RU.get(run.get('status'), run.get('status'))} |")
        sec = [f"## {title}", ""]
        src = run.get("source_url")
        sec.append(f"Проверено {refdata.msk(run['at'], True)} МСК. Откуда взято: {run.get('via_title', '')}" + (f", {src}" if src else "") + ".")
        if run.get("version_after"):
            sec.append(f"Версия источника: {run['version_after']}" + (f" (была {run['version_before']})" if run.get("version_before") and run.get("version_before") != run.get("version_after") else "") + ".")
        sec.append("")
        lines = run.get("lines") or []
        rel = os.path.relpath(path, refdata.data_dir()).replace("\\", "/")
        if lines:
            sec.append(f"Изменения ({len(lines)}):")
            sec += [f"- {x}" for x in lines[:MAX_LINES]]
            if len(lines) > MAX_LINES:
                sec.append(f"- … ещё {len(lines) - MAX_LINES}, полный список в `{rel}`")
        else:
            prev_path, prev_run = _last_with_changes(name)
            if prev_run and prev_run is not run:
                prev_rel = os.path.relpath(prev_path, refdata.data_dir()).replace("\\", "/")
                sec.append(f"Изменений нет. Последние изменения были {refdata.msk(prev_run['at'], True)} МСК, см. `{prev_rel}`.")
            else:
                sec.append("Изменений нет.")
        notes = run.get("notes") or []
        if notes:
            sec += ["", "Заметки:"] + [f"- {x}" for x in notes[:20]]
        errs = run.get("errors") or []
        if errs:
            sec += ["", "Не сработали звенья цепочки:"] + [f"- {e['title']}: {e['error']}" for e in errs]
        sections.append("\n".join(sec))
    head = ["# Обновление справочников", "",
            f"Последний запуск: {refdata.msk(last_at, True) if last_at else '?'} МСК. Данные лежат в `tzcheck/data/`, журнал изменений по дням в `tzcheck/data/updates/`.",
            "", "| Справочник | Проверено (МСК) | Откуда взято | Записей | Добавлено | Удалено | Изменено | Итог |",
            "|---|---|---|---|---|---|---|---|"] + rows
    text = "\n".join(head) + "\n\n" + "\n\n".join(sections) + "\n"
    path = os.path.join(updates_dir(), "LATEST.md")
    write_atomic(path, text.encode("utf-8"))
    return path
