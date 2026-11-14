"""Проверка правки: замену цитаты (поле replacement) у замечаний основного списка модель проверяет отдельным вызовом до того,
как замена попадёт в файл Word в режиме рецензирования. По каждой правке модель видит цитату, место, довод, как исправить,
замену и текст вокруг цитаты и решает, устраняет ли замена дефект и не создаёт ли новое противоречие с соседним текстом.

Неподтверждённая замена в правки Word не идёт (replacement очищается, предложенный текст остаётся в evidence), замечание
и комментарий остаются. Без ответа модели правки остаются как были. TZCHECK_FIXCHECK=0 выключает шаг; своя модель шага
задаётся как у других шагов ядра: TZCHECK_FIXCHECK_MODEL (llm.stage_setting)."""
import os
import re

from . import chunks, core, passport
from . import progress as prog
from .ask import ask, pmap, unit_of, wrap
from .model import SECTION_MAIN

STAGE = "fixcheck"
STEP = "проверка правок"
BATCH = 20
CONTEXT_CHARS = 1500

PROMPT = """Задача: проверить предложенные правки к техническому заданию до того, как их внесут в документ.
Паспорт закупки: {passport}
Каждая правка заменяет цитату из замечания предлагаемым текстом. К правке даны: цитата и где она стоит, довод замечания (в чём
дефект), как исправить, предлагаемая замена и текст вокруг цитаты: строка замечания отмечена знаком », рядом соседние строки той
же позиции или соседние абзацы. Текст получен разбором файла: [id] в начале строк и подписи столбцов добавил разбор.

Подставь замену на место цитаты и прочитай строку целиком вместе с соседними. По каждой правке реши:
1. Устраняет ли замена дефект из довода: после неё довод уже неверен.
2. Не создаёт ли она новый дефект: расхождение с соседними строками, позицией или вторым местом (значение, единица, количество,
   срок, наименование, комплектность); потерю сведений цитаты, которые к дефекту не относятся; опечатку, ошибку в числе или
   единице; фразу, которая не согласуется с текстом вокруг; указание на одного производителя без слов «или эквивалент».
Значение, которое замена вводит впервые, само по себе не дефект, если оно не расходится с показанным текстом.

"ok": true, только если замена устраняет дефект и нового дефекта не создаёт; иначе "ok": false и в "reason" коротко, что не так.

{items}

Правок в запросе: {count}, их номера: {numbers}. Дай по одному решению на каждый номер правки и больше ничего.
Верни JSON: {{"decisions": [{{"n": номер, "ok": true|false, "reason": "коротко, на чём основано решение"}}]}}"""


def enabled() -> bool:
    return os.environ.get("TZCHECK_FIXCHECK", "1").strip().lower() not in ("0", "false", "no", "off")


def _known(scr, text: str) -> str:
    """Значения, которые маска уже заменила метками, остаются метками и там, где распознавание их не нашло: в тексте модели
    после scr.restore они стоят открыто."""
    for tok, val in sorted(list(scr.mapping.items()), key=lambda x: -len(x[1])):
        if val and val in text:
            text = re.sub(r"(?<!\w)" + re.escape(val) + r"(?!\w)", lambda _m: tok, text)
    return text


def _mask(scr, text: str) -> str:
    return _known(scr, scr.scrub(text or ""))


def _item(doc, scr, n: int, f) -> str:
    b = doc.block(f.block_id)
    where = _mask(scr, b.location() if b is not None else f.location)
    lines = [f"{n}. [{f.category}] цитата [{f.block_id}] ({where}): «{_mask(scr, chunks.clip(f.quote, 600))}»",
             f"   Довод: {_mask(scr, chunks.clip(f.explanation, 600))}"]
    if f.fix:
        lines.append(f"   Как исправить: {_mask(scr, chunks.clip(f.fix, 400))}")
    lines.append(f"   Предлагаемая замена: «{_mask(scr, chunks.clip(f.replacement, 800))}»")
    b2 = doc.block(f.evidence["второй блок"]) if f.evidence.get("второй блок") else None
    if b2 is not None:
        near = chunks.window(b2.text, str(f.evidence.get("вторая цитата") or f.quote), 800)
        lines.append(f"   Второе место [{b2.id}] ({_mask(scr, b2.location())}): {_mask(scr, near)}")
    around, _ = core._around(doc, scr, f, CONTEXT_CHARS)
    if around:
        around = _known(scr, around)
    elif b is not None:
        around = f"» [{b.id}] {_mask(scr, chunks.window(b.text, f.quote, CONTEXT_CHARS // 2))}"
    lines.append("   Текст вокруг цитаты:\n" + (around or "   не найден"))
    return "\n".join(lines)


def _ok(v) -> bool | None:
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    return True if s == "true" else False if s == "false" else None


def _decide(scr, f, d: dict, ok: bool, call: str):
    reason = " ".join(scr.restore(str(d.get("reason") or "")).split())
    if ok:
        f.evidence["проверка правки"] = "правку модель подтвердила" + (f": {reason}" if reason else "")
        f.trace.append({"step": "fixcheck", "call": call, "item": d, "text": "правку модель подтвердила"})
        return
    text = "правку модель не подтвердила" + (f": {reason}" if reason else "")
    f.evidence["проверка правки"] = f"{text}; замена «{f.replacement}» в правки Word не внесена"
    f.trace.append({"step": "fixcheck", "call": call, "item": d, "text": text})
    f.replacement = ""


def apply(doc, scr, findings: list) -> list:
    """Проверить замены у замечаний основного списка; замечания меняются на месте. Возвращает замечания, у которых была замена."""
    cand = [f for f in findings if f.section == SECTION_MAIN and f.replacement and f.quote.strip()]
    if not cand or not enabled():
        return []
    prog.tick(doc, stage=f"проверка предложенных правок: {len(cand)}")
    pline = _mask(scr, passport.line(doc))
    texts = {n: _item(doc, scr, n, f) for n, f in enumerate(cand, 1)}
    batches = [list(range(i + 1, min(i + BATCH, len(cand)) + 1)) for i in range(0, len(cand), BATCH)]

    def work(ns):
        prompt = PROMPT.format(passport=pline, items=wrap("\n\n".join(texts[n] for n in ns)), count=len(ns),
                               numbers=", ".join(str(n) for n in ns))
        unit = unit_of("finding", f"правки ({len(ns)})", [cand[n - 1].block_id for n in ns])
        return ns, ask(doc, prompt, min(4000, 400 + 150 * len(ns)), STEP, STAGE, "Проверка предложенных правок", unit) or {}

    seen = len(doc.warnings)
    results = pmap(work, batches)
    # шаг необязательный: сбой вызова не делает отчёт неполным, правки остаются как были (вызов с ошибкой лежит в журнале)
    del doc.warnings[seen:]
    for ns, data in results:
        done = set()
        for d in data.get("decisions") or []:
            if not isinstance(d, dict):
                continue
            try:
                n = int(d.get("n"))
            except (TypeError, ValueError):
                continue
            ok = _ok(d.get("ok"))
            if n in ns and n not in done and ok is not None:
                done.add(n)
                _decide(scr, cand[n - 1], d, ok, data.get("_call", ""))
    return cand
