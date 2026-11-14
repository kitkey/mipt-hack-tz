"""Вызов модели для ядра проверки: один вход на документ (маскирование и запрет внешней отправки внутри llm._call),
бюджет времени на документ, учёт вызовов, параллельный прогон кусков."""
import os
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext

from . import llm


def _scope(doc):
    scope = getattr(llm, "doc_scope", None)
    return scope(doc) if scope is not None else nullcontext()


def ask(doc, prompt: str, max_tokens: int = 4000, step: str = "", stage: str = "", title: str = "", unit: dict | None = None) -> dict | None:
    """Ответ модели словарём или None (бюджет исчерпан, сбой, не JSON); причина пропуска пишется в doc.warnings.
    Вызов пишется в журнал прогона (doc.journal); id записи кладётся в ответ полем "_call"."""
    deadline = getattr(doc, "llm_deadline", None)
    if deadline is not None and time.time() > deadline:
        note = f"Модель: бюджет {llm.doc_budget():.0f} с на документ исчерпан, оставшиеся вызовы пропущены."
        if note not in doc.warnings:
            doc.warnings.append(note)
        return None
    journal = getattr(doc, "journal", None)
    try:
        with llm.journal_scope(journal, stage or step, title or step, unit), _scope(doc):
            # шаг со своей моделью (TZCHECK_VERIFY_MODEL и подобные) идёт в неё, остальные в общую
            own = bool(stage and os.environ.get(f"TZCHECK_{stage.upper()}_MODEL"))
            data = llm._call(prompt, max_tokens, stage=stage) if own else llm._call(prompt, max_tokens)
            call = llm.last_call_id()
    except Exception as e:
        doc.warnings.append(f"Модель: шаг «{step}» пропущен ({type(e).__name__}: {str(e)[:120]})")
        return None
    calls = getattr(doc, "model_calls", None)
    if calls is not None:
        calls[step] = calls.get(step, 0) + 1
    if not isinstance(data, dict):
        return None
    if data:
        data["_call"] = call
    return data


def rows(data, key: str) -> list[dict]:
    """Список словарей из поля ответа модели. Поле другого типа (число, строка, null, словарь) значит, что модель на этот шаг
    не ответила; элементы списка, которые не словари, отбрасываются."""
    v = data.get(key) if isinstance(data, dict) else None
    return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []


def unit_of(kind: str, title: str, blocks) -> dict:
    return {"kind": kind, "title": title, "blocks": list(blocks)[:200]}


def pmap(fn, items: list) -> list:
    workers = max(1, int(os.environ.get("TZCHECK_LLM_WORKERS", "1")))
    if workers == 1 or len(items) <= 1:
        return [fn(x) for x in items]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(fn, items))


def wrap(text: str) -> str:
    """Текст ТЗ между метками данных; метки внутри текста заменяются, чтобы документ не закрыл блок данных."""
    return "<<<ТЗ>>>\n" + text.replace("<<<", "«").replace(">>>", "»") + "\n<<<КОНЕЦ>>>"
