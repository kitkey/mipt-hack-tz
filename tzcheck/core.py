"""Ядро проверки ТЗ: все решения «есть дефект / нет дефекта» принимает модель.

Код собирает контекст (паспорт, позиция с подписанными столбцами, общие условия, справочные записи, кандидаты поиска),
проверяет, что цитата модели дословно есть в тексте, и привязывает замечание к месту. Шаги:
find — дефекты по позиции или разделу; ref — обозначения, которые назвала модель, по справочникам; absent — «не указано»
против кандидатов поиска по всему комплекту; cross — одна характеристика позиции в разных частях; verify — независимая
вторая проверка с полным контекстом, которая может снять или понизить замечание, но не повысить."""
import json
import re

from . import confidence, passport, search, tables
from .ask import ask, pmap, unit_of, wrap
from .model import Finding
from .norms import CATEGORIES, CATEGORY_GROUP, NORMS, norm_list_for_prompt

SEV = ("low", "medium", "high")
UNIT_CHARS = 5000
TERMS_CHARS = 2500

FIND_PROMPT = """Задача: найти дефекты технического задания в одной части: {what}.
Паспорт закупки: {passport}
{practice}
Общие условия из других разделов, которые могут относиться к этой части:
{terms}
Категории (только эти), по группам:
{categories}

Нормы (id: суть):
{norms}

Проверяемая часть. Строки в формате [id] текст; у строк таблицы подписаны столбцы.
{data}

Найди то, что мешает закупке: участник не поймёт, что предложить; заказчик не сможет однозначно принять результат; описание сужает круг
участников без основания; части документа противоречат друг другу; ссылки на неверные или недействующие документы; лишнее для технического
задания. Требования к конкуренции оценивай с учётом способа закупки из паспорта. Какие характеристики нужны, решай по виду товара, работы
или услуги и не требуй того, чего у такого предмета не бывает. Не считай дефектом то, что указано в общих условиях. Корректные
формулировки не помечай.

Если утверждаешь, что чего-то нет (характеристики, значения, срока, количества, условия), поставь "absent": true и в "missing" коротко назови,
чего нет: это проверят по всему комплекту документов.
В "lookups" перечисли обозначения из этой части, по которым нужна справка: стандарты и своды правил (type "standard"), коды ОКПД 2
("okpd"), коды КТРУ ("ktru"), средства измерений, для которых важно, внесены ли они в реестр утверждённых типов ("si").

Верни JSON: {{"findings": [{{"block_id": "id строки", "quote": "дословная цитата из этой строки, 3-25 слов", "category": "категория из списка",
"severity": "high|medium|low", "explanation": "почему это дефект, по тексту, 1-2 предложения", "fix": "как исправить",
"replacement": "текст на место цитаты, если исправление сводится к замене цитаты, иначе пусто",
"norm_ids": ["id из списка норм"], "needs_review": true|false, "absent": true|false, "missing": "чего нет",
"confidence": целое 0-100, уверенность, что это действительно дефект}}],
"lookups": [{{"type": "standard|okpd|ktru|si", "value": "обозначение как в тексте", "block_id": "id строки", "item": "к какой позиции относится"}}]}}
Если дефектов нет, верни {{"findings": [], "lookups": [...]}}."""

REF_PROMPT = """Задача: проверить обозначения из технического задания по справочным записям.
Паспорт закупки: {passport}

Часть технического задания:
{data}

Справочные записи (на дату записи; «не найдено» значит, что записи нет в доступной копии справочника, а не что обозначение неверно):
{records}

По каждой записи реши, есть ли дефект ТЗ: стандарт отменён или заменён; в обозначении стандарта ошибка; код ОКПД 2 относится к другому виду
продукции или общий («прочие») при наличии точного кода; код КТРУ не соответствует коду ОКПД 2 позиции; средство измерений без требования
о поверке или утверждении типа. Где записи нет или её мало для решения, ставь "needs_review": true. Категории: {categories}.

Верни JSON: {{"findings": [{{"block_id": "id строки", "quote": "дословная цитата из этой строки", "category": "...", "severity": "high|medium|low",
"explanation": "...", "fix": "...", "norm_ids": ["id из списка: {norm_ids}"], "needs_review": true|false, "confidence": 0-100}}]}}"""

ABSENT_PROMPT = """Задача: проверить утверждения «не указано» по всему комплекту документов.
Паспорт закупки: {passport}
Для каждого утверждения даны места из всего комплекта, найденные поиском: [id] (роль части; место) текст.
Реши:
- "not_found": в кандидатах этого нет, утверждение верно;
- "same": это указано в той же части или в общих условиях, которые к ней относятся, утверждение неверно;
- "other": это указано в другой части комплекта (договор, другой раздел, приложение); пропуска нет, но нужна проверка согласованности.
В "block_id" укажи id места, где указано (для same и other).

{items}

Верни JSON: {{"decisions": [{{"n": номер, "decision": "not_found|same|other", "block_id": "...", "reason": "коротко"}}]}}"""

CROSS_PROMPT = """Задача: сверка частей комплекта документов по позиции и условиям.
Паспорт закупки: {passport}
Для каждой позиции или раздела даны его строки и места из других частей комплекта (требования, спецификация, договор, приложения),
найденные поиском. Найди противоречия: одна и та же характеристика или условие одной и той же позиции в разных частях задана по-разному
(значение, единица, количество, срок, комплектность, вид работ). Разные позиции, разные характеристики или разные объекты с похожими
числами противоречием не являются.

{items}

Верни JSON: {{"findings": [{{"block_id": "id строки с ошибочным или спорным значением", "quote": "дословная цитата из неё",
"other_block_id": "id второго места", "other_quote": "дословная цитата из второго места", "category": "Противоречие|Предмет закупки или наименование",
"severity": "high|medium|low", "explanation": "что с чем не сходится", "fix": "как исправить", "confidence": 0-100}}]}}"""

VERIFY_PROMPT = """Задача: независимая вторая проверка замечаний к техническому заданию.
Паспорт закупки: {passport}
{practice}
Для каждого замечания дана вся позиция или раздел, где оно стоит, и, если найдены, пункт нормы и другие места комплекта.
Реши по тексту:
- "keep": дефект есть;
- "lower": дефект возможен, но текстом не доказан или зависит от внешних сведений: оставить на ручную проверку с меньшей важностью;
- "reject": фрагмент корректен, нужные сведения есть в контексте, довод неверен или к этой закупке не относится.
Обычные юридические и процедурные формулировки без дефекта не ошибка.

{items}

Верни JSON: {{"decisions": [{{"n": номер, "decision": "keep|lower|reject", "reason": "коротко, на чём основано решение",
"confidence": целое 0-100, уверенность, что это действительно дефект}}]}}"""


class Unit:
    def __init__(self, kind: str, title: str, block_ids: list, text: str, position=None, section: str = ""):
        self.kind, self.title, self.block_ids, self.text, self.position, self.section = kind, title, block_ids, text, position, section
        self.lookups: list = []


def _norm(s: str) -> str:
    return " ".join((s or "").split())


def _exact(q: str, blocks: list) -> tuple | None:
    for b in blocks:
        text = b.text
        flat = _norm(text)
        i = flat.lower().find(q.lower())
        if i < 0:
            continue
        # индекс в нормализованном тексте -> подстрока исходного текста
        words = q.split()
        pattern = r"\s+".join(re.escape(w) for w in words)
        m = re.search(pattern, text, re.I)
        return (b, m.group(0)) if m else (b, flat[i:i + len(q)])
    return None


def locate(quote: str, blocks: list, labels=()) -> tuple | None:
    """Цитата модели дословно в тексте блока (пробелы и регистр не важны) -> (блок, точная подстрока текста). Подстановки нет.
    labels: подписи столбцов, которыми код размечает строки позиции для модели («Значение: …»); если модель процитировала
    размеченную строку, подписи снимаются и ищутся сами значения ячеек."""
    q = _norm(quote).strip(" «»\"'“”")
    if len(q) < 3:
        return None
    hit = _exact(q, blocks)
    if hit or not labels or ("|" not in q and ":" not in q):
        return hit
    low = {x.lower() for x in labels if x}
    segs = []
    for seg in q.split("|"):
        seg = seg.strip()
        head, sep, tail = seg.partition(":")
        if sep and head.strip().lower() in low:
            seg = tail.strip()
        if seg and seg != "(пусто)":
            segs.append(seg)
    if not segs:
        return None
    for cand in [" | ".join(segs)] + sorted(segs, key=len, reverse=True):
        if len(cand) >= 3:
            hit = _exact(cand, blocks)
            if hit:
                return hit
    return None


def _labels(doc) -> set:
    m = tables.of(doc)
    return {c.label for t in m.tables for c in t.columns} | set(tables.TITLES.values())


def _drop(doc, stage: str, item, call: str, why: str):
    doc.model_dropped.append({"шаг": stage, "цитата": str((item or {}).get("quote") or "")[:120], "почему": why})
    journal = getattr(doc, "journal", None)
    if journal is not None:
        journal.dropped.append({"stage": stage, "reason": why, "category": (item or {}).get("category"), "quote": (item or {}).get("quote"),
                                 "trace": [{"step": "found", "call": call, "item": item}, {"step": "not_located", "text": why}]})


def categories_text(cats: list) -> str:
    by: dict = {}
    for c in cats:
        by.setdefault(CATEGORY_GROUP.get(c, ""), []).append(c)
    return "\n".join(f"{g}: " + "; ".join(cs) for g, cs in by.items())


def _section_units(doc, scr, model) -> list[Unit]:
    out, cur, key, size = [], [], None, 0

    def flush():
        if cur:
            text = "\n".join(f"[{b.id}] ({scr.scrub(b.section[:60])}) {scr.scrub(b.text)}" for b in cur)
            out.append(Unit("section", cur[0].section or cur[0].text[:80], [b.id for b in cur], text, section=cur[0].section))

    for b in doc.blocks:
        info = model.rows.get(b.id)
        if not passport.checked(b) or (info is not None and info.position is not None) or (info is not None and info.header and info.table.kind != "other"):
            continue
        k = (b.source, b.section)
        if cur and (k != key or size + len(b.text) > UNIT_CHARS):
            flush()
            cur, size = [], 0
        cur.append(b)
        size += len(b.text) + 20
        key = k
    flush()
    return out


def _position_units(doc, scr, model) -> list[Unit]:
    out = []
    batch, size = [], 0

    def flush():
        if batch:
            out.append(Unit("position", "; ".join(p.name[:60] for p, _ in batch), [x for p, _ in batch for x in p.block_ids],
                            "\n\n".join(t for _, t in batch), batch[0][0] if len(batch) == 1 else None))

    for p in model.positions:
        b0 = doc.block(p.block_ids[0]) if p.block_ids else None
        if b0 is None or not passport.checked(b0):
            continue
        text = tables.render(doc, p, scr, limit=10 ** 6)
        header = tables.render(doc, tables.Position(p.id, p.source, p.table, p.name, p.num, p.okpd, p.ktru, p.qty, p.unit), scr)
        if len(text) > UNIT_CHARS * 1.4:
            flush()
            batch, size = [], 0
            lines, part = text.split("\n")[1:], []
            for ln in lines:
                part.append(ln)
                if sum(len(x) for x in part) > UNIT_CHARS:
                    out.append(Unit("position", p.name[:80], p.block_ids, header + "\n" + "\n".join(part), p))
                    part = []
            if part:
                out.append(Unit("position", p.name[:80], p.block_ids, header + "\n" + "\n".join(part), p))
            continue
        if batch and (size + len(text) > UNIT_CHARS or batch[0][0].table != p.table):
            flush()
            batch, size = [], 0
        batch.append((p, text))
        size += len(text)
    flush()
    return out


def _terms(doc, scr, unit: Unit) -> str:
    blocks = [b for b in doc.blocks if b.role in ("terms", "warranty") and b.id not in set(unit.block_ids)]
    if not blocks:
        return "нет"
    if sum(len(b.text) for b in blocks) > TERMS_CHARS:
        idx = search.of(doc)
        query = unit.title + " " + " ".join(c.name for c in (unit.position.chars if unit.position else [])[:20])
        picked = [b for b, _ in idx.top(query, 10, exclude=unit.block_ids, only=lambda x: x.role in ("terms", "warranty"))]
        blocks = picked or blocks[:8]
    out, used = [], 0
    for b in blocks:
        t = f"[{b.id}] {scr.scrub(b.text[:700])}"
        if used + len(t) > TERMS_CHARS:
            break
        out.append(t)
        used += len(t)
    return "\n".join(out) or "нет"


def _finding(doc, item: dict, blocks: list, scr, source: str, cats: list, call: str = "") -> Finding | None:
    if not isinstance(item, dict):
        return None
    cat = item.get("category")
    if cat not in cats:
        _drop(doc, source, item, call, f"категории «{cat}» нет среди проверяемых")
        return None
    pref = str(item.get("block_id") or "")
    ordered = sorted(blocks, key=lambda b: b.id != pref)
    loc = locate(scr.restore(str(item.get("quote") or "")), ordered, _labels(doc))
    if loc is None:
        _drop(doc, source, item, call, "цитаты нет в тексте")
        return None
    b, quote = loc
    norms = [n for n in item.get("norm_ids") or [] if n in NORMS]
    sev = item.get("severity") if item.get("severity") in SEV else "medium"
    f = Finding(b.id, quote, cat, scr.restore(str(item.get("explanation") or "")).strip(), scr.restore(str(item.get("fix") or "")).strip(),
                sev, norms, bool(item.get("needs_review")), source, 0.7, b.location(), rule_id=source.replace(":", "."))
    rep = _norm(scr.restore(str(item.get("replacement") or "")))
    if rep and rep != _norm(quote):
        f.replacement = rep
    f._absent = bool(item.get("absent"))
    f._missing = scr.restore(str(item.get("missing") or ""))
    f.trace = [{"step": "found", "call": call, "item": item},
               {"step": "located", "text": f"цитата найдена: {b.location()} (блок {b.id})" + ("" if b.id == pref else f", модель указала блок {pref or 'не указан'}")}]
    return confidence.set_model_score(f, item.get("confidence"), confidence.STAGE_FIND)


def find(doc, scr, cats: list) -> list[Finding]:
    model = tables.of(doc)
    units = _position_units(doc, scr, model) + _section_units(doc, scr, model)
    doc.core_units = units
    pline = scr.scrub(passport.line(doc))
    practice = passport.practice(doc)
    norms = norm_list_for_prompt(bool(practice))
    ctext = categories_text(cats)

    def work(u: Unit):
        what = (f"позиция спецификации «{u.title[:120]}»" if u.kind == "position" else f"раздел «{u.title[:120]}»")
        prompt = FIND_PROMPT.format(what=scr.scrub(what), passport=pline, practice=practice + "\n" if practice else "",
                                    terms=_terms(doc, scr, u) if u.kind == "position" else "нет",
                                    categories=ctext, norms=norms, data=wrap(u.text))
        title = "Поиск дефектов: " + what
        data = ask(doc, prompt, 6000, "поиск дефектов", "find", title, unit_of(u.kind, u.title, u.block_ids)) or {}
        blocks = [doc.block(x) for x in u.block_ids if doc.block(x)]
        call = data.get("_call", "")
        out = [f for f in (_finding(doc, i, blocks, scr, "model:find", cats, call) for i in data.get("findings") or []) if f]
        for f in out:
            f._unit = u
        u.lookups = [x for x in data.get("lookups") or [] if isinstance(x, dict) and x.get("type") in ("standard", "okpd", "ktru", "si") and x.get("value")]
        return out

    found = []
    for res in pmap(work, units):
        found.extend(res)
    return found


def _record(doc, lk: dict, scr) -> str:
    from . import okpd, refdata
    kind, value = lk["type"], scr.restore(str(lk["value"]))[:120]
    item = scr.restore(str(lk.get("item") or ""))[:120]
    where = f" [{lk.get('block_id')}]" if lk.get("block_id") else ""
    try:
        if kind == "standard":
            st = refdata.gost_status(value)
            if not st:
                return f"Стандарт «{value}»{where}: не найдено в копии каталога Росстандарта"
            tail = "".join(f", {k} {st[x]}" for k, x in (("заменён на", "replaced_by"), ("действие прекращено", "date_end")) if st.get(x))
            return (f"Стандарт «{value}»{where}: в каталоге {st.get('designation')} «{(st.get('title') or '')[:120]}», статус «{st.get('status') or '?'}»{tail}"
                    )
        if kind == "okpd":
            code = value.split()[0].strip(" .,;")
            nm = okpd.name(code)
            sug = okpd.suggest(item, code) if item else []
            close = "; ".join(f"{c} «{n[:80]}»" for c, n, *_ in sug[:3])
            return (f"ОКПД 2 «{code}»{where} для позиции «{item}»: " + (f"в классификаторе ({okpd.version()}) «{nm[:150]}»" if nm else "кода нет в классификаторе")
                    + (f"; близкие по наименованию позиции коды: {close}" if close else ""))
        if kind == "ktru":
            prefix = value.split("-")[0].strip()
            items = refdata.ktru_items(prefix)
            rec = next((r for r in items if r.get("code") == value.strip()), None)
            if rec:
                return f"КТРУ «{value}»{where}: позиция «{(rec.get('name') or '')[:120]}», статус «{rec.get('status') or ''}»"
            return f"КТРУ «{value}»{where}: не найдено в копии каталога КТРУ" + (f" (для ОКПД 2 {prefix} позиций в копии: {len(items)})" if items else "")
        if kind == "si":
            types = refdata.si_type(value, value.split()[0] if value.split() else "")
            if not types:
                return f"Средство измерений «{value}»{where}: в копии реестра ФГИС «Аршин» не найдено"
            t = types[0]
            ex = "; ".join(f"№ {x.get('number')} «{(x.get('title') or '')[:60]}»" for x in types[:2])
            return f"Средство измерений «{value}»{where}: в реестре ФГИС «Аршин» найдено утверждённых типов {t.get('total')}, например {ex}"
    except Exception as e:
        return f"«{value}»{where}: справочник недоступен ({type(e).__name__})"
    return ""


def refs(doc, scr, cats: list) -> list[Finding]:
    units = [u for u in getattr(doc, "core_units", []) if u.lookups]
    pline = scr.scrub(passport.line(doc))
    ref_cats = [c for c in cats if c in ("Стандарты (ГОСТ, поверка)", "Неверный или общий код ОКПД 2", "Неполнота или комплектность", "Противоречие")]
    if not ref_cats:
        return []

    def work(u: Unit):
        seen, records = set(), []
        for lk in u.lookups[:15]:
            key = (lk["type"], str(lk["value"]).strip().lower())
            if key in seen:
                continue
            seen.add(key)
            rec = _record(doc, lk, scr)
            if rec:
                records.append(f"{len(records) + 1}. {scr.scrub(rec)}")
        if not records:
            return []
        prompt = REF_PROMPT.format(passport=pline, data=wrap(u.text), records="\n".join(records), categories="; ".join(ref_cats),
                                   norm_ids=", ".join(k for k in NORMS if k != "INTERNAL"))
        data = ask(doc, prompt, 4000, "справочники", "refs", f"Справочные записи: {u.title[:100]}", unit_of(u.kind, u.title, u.block_ids)) or {}
        blocks = [doc.block(x) for x in u.block_ids if doc.block(x)]
        call = data.get("_call", "")
        out = [f for f in (_finding(doc, i, blocks, scr, "model:ref", ref_cats, call) for i in data.get("findings") or []) if f]
        for f in out:
            f._unit = u
            f.evidence["справочные записи"] = [r.split(". ", 1)[-1] for r in records][:6]
            f.trace.insert(2, {"step": "reference", "text": "справочные записи по обозначениям, которые назвала модель", "records": f.evidence["справочные записи"]})
        return out

    found = []
    for res in pmap(work, units):
        found.extend(res)
    return found


def _role(b) -> str:
    return passport.ROLES.get(b.role, "роль не определена")


def absent(doc, scr, findings: list[Finding]) -> list[Finding]:
    claims = [f for f in findings if getattr(f, "_absent", False)]
    if not claims:
        return findings
    idx = search.of(doc)
    pline = scr.scrub(passport.line(doc))
    items, cands = [], {}
    for n, f in enumerate(claims, 1):
        u = getattr(f, "_unit", None)
        what = f._missing or f.explanation
        query = what + " " + (u.title if u else "")
        hits = idx.top(query, 6, exclude=u.block_ids if u else [f.block_id])
        cands[n] = hits
        lines = "\n".join(f"   [{b.id}] ({_role(b)}; {b.location()}) {scr.scrub(b.text[:400])}" for b, _ in hits) or "   кандидатов нет"
        items.append(f"{n}. Утверждение: {scr.scrub(f.explanation[:300])} Чего нет: {scr.scrub(what[:120])}. "
                     f"Где: [{f.block_id}] ({u.title[:80] if u else ''})\n   Кандидаты:\n{lines}")
    decided, calls = {}, {}
    for start in range(0, len(items), 8):
        part = claims[start:start + 8]
        unit = unit_of("finding", f"утверждений «не указано»: {len(part)}", [x.block_id for x in part])
        data = ask(doc, ABSENT_PROMPT.format(passport=pline, items=wrap("\n".join(items[start:start + 8]))), 3000, "проверка «не указано»",
                   "absent", "Проверка «не указано» по всему комплекту", unit) or {}
        for n in range(start + 1, start + len(part) + 1):
            calls[n] = data.get("_call", "")
        for d in data.get("decisions") or []:
            try:
                decided[int(d.get("n"))] = d
            except (TypeError, ValueError, AttributeError):
                continue
    out = []
    for f in findings:
        if f not in claims:
            out.append(f)
            continue
        n = claims.index(f) + 1
        d = decided.get(n)
        hits = cands.get(n) or []
        f.evidence["поиск по комплекту"] = [f"{b.location()}: {b.text[:160]}" for b, _ in hits[:3]] or "кандидатов не найдено"
        why = scr.restore(str((d or {}).get("reason") or ""))[:300]
        verdict = {"same": f"снято: указано в той же части или общих условиях; {why}", "other": f"понижено: указано в другом разделе; {why}",
                   "not_found": f"оставлено: не найдено по комплекту; {why}"}.get(str((d or {}).get("decision")), "решения модели нет: на ручную проверку")
        f.trace.append({"step": "absent_search", "call": calls.get(n, ""), "item": d,
                        "text": f"поиск по комплекту: кандидатов {len(hits)} ({', '.join(b.id for b, _ in hits)}); {verdict}"})
        if not d:
            f.evidence["проверка «не указано»"] = "не проведена"
            f.needs_review = True
            out.append(f)
            continue
        dec, where = str(d.get("decision")), doc.block(str(d.get("block_id") or ""))
        reason = scr.restore(str(d.get("reason") or ""))[:300]
        if dec == "same":
            f.evidence["проверка «не указано»"] = f"указано: {where.location() if where else ''}; {reason}"
            doc.llm_rejected.append(f)
            continue
        if dec == "other":
            loc = where.location() if where else "другой раздел комплекта"
            f.explanation = f"Сведения указаны в другом разделе ({loc}), проверить согласованность. Довод модели: {f.explanation}"
            f.severity = "low"
            f.needs_review = True
            f.evidence["проверка «не указано»"] = f"указано в другом разделе: {loc}; {reason}"
            if where:
                f.also_in.append(where.id)
            out.append(f)
            continue
        f.evidence["проверка «не указано»"] = f"не найдено по комплекту; {reason}"
        out.append(f)
    return out


def cross(doc, scr, cats: list) -> list[Finding]:
    cross_cats = [c for c in cats if c in ("Противоречие", "Предмет закупки или наименование")]
    if not cross_cats:
        return []
    idx = search.of(doc)
    model = tables.of(doc)
    pline = scr.scrub(passport.line(doc))
    items, ids = [], []
    for u in getattr(doc, "core_units", []):
        mine = set(u.block_ids)
        if u.kind == "position":
            ps = [u.position] if u.position else [p for p in model.positions if p.block_ids and p.block_ids[0] in mine]
            same_table = {bid for bid, info in model.rows.items() if ps and info.table.key[:2] == (ps[0].source, ps[0].table)}
            query = " ".join(p.name + " " + " ".join(c.name for c in p.chars[:15]) for p in ps)
            hits = idx.top(query, 5, exclude=mine | same_table)
        else:
            src = doc.block(u.block_ids[0]).source
            hits = idx.top(u.text[:1500], 4, exclude=mine, only=lambda b, s=src: b.source != s or b.role in ("contract", "spec"))
        if not hits:
            continue
        lines = "\n".join(f"   [{b.id}] ({_role(b)}; {b.location()}) {scr.scrub(b.text[:500])}" for b, _ in hits)
        items.append((u, f"{u.text[:3500]}\n   Места из других частей:\n{lines}", [b for b, _ in hits]))
    out = []

    def work(group):
        text = "\n\n".join(f"{n}. {t}" for n, (_, t, _) in enumerate(group, 1))
        blocks = [doc.block(x) for u, _, _ in group for x in u.block_ids if doc.block(x)] + [b for _, _, hs in group for b in hs]
        unit = unit_of("pair", "; ".join(u.title[:60] for u, _, _ in group), [b.id for b in blocks])
        data = ask(doc, CROSS_PROMPT.format(passport=pline, items=wrap(text)), 4000, "сверка частей", "cross",
                   f"Сверка частей: {unit['title'][:120]}", unit) or {}
        res = []
        call = data.get("_call", "")
        for it in data.get("findings") or []:
            f = _finding(doc, it, blocks, scr, "model:cross", cross_cats, call)
            if not f:
                continue
            other = locate(scr.restore(str(it.get("other_quote") or "")), sorted(blocks, key=lambda b: b.id != str(it.get("other_block_id"))), _labels(doc))
            if other is None or other[0].id == f.block_id:
                _drop(doc, "model:cross", it, call, "второй цитаты нет в тексте" if other is None else "вторая цитата в том же блоке")
                continue
            f.trace.append({"step": "located", "text": f"вторая цитата найдена: {other[0].location()} (блок {other[0].id})"})
            f.also_in.append(other[0].id)
            f.evidence.update({"второе место": other[0].location(), "вторая цитата": other[1], "второй блок": other[0].id})
            f._unit = next((u for u, _, _ in group if f.block_id in u.block_ids), group[0][0])
            res.append(f)
        return res

    groups_, cur, size = [], [], 0
    for it in items:
        if cur and size + len(it[1]) > 9000:
            groups_.append(cur)
            cur, size = [], 0
        cur.append(it)
        size += len(it[1])
    if cur:
        groups_.append(cur)
    for res in pmap(work, groups_):
        out.extend(res)
    return out


def _context(doc, f: Finding, scr) -> str:
    u = getattr(f, "_unit", None)
    p = tables.position(doc, f.block_id)
    if p is not None:
        ctx = tables.render(doc, p, scr, limit=6000)
    elif u is not None:
        ctx = u.text[:5000]
    else:
        b = doc.block(f.block_id)
        ctx = f"[{b.id}] {scr.scrub(b.text[:3000])}" if b else ""
    if f.evidence.get("второй блок"):
        b2 = doc.block(f.evidence["второй блок"])
        if b2:
            ctx += f"\n   Второе место [{b2.id}] ({_role(b2)}): {scr.scrub(b2.text[:800])}"
    found = f.evidence.get("поиск по комплекту")
    if isinstance(found, list):
        ctx += "\n   Найдено поиском по комплекту: " + " / ".join(scr.scrub(x) for x in found)
    return ctx


def _norm_fragment(f: Finding) -> str:
    try:
        from . import rag
        if not rag._enabled() or not rag.ensure_index():
            return ""
        mode, model = rag.default_mode()
        ev = rag.evidence_for(f.quote + " " + f.explanation, f.norm_ids, rag._searcher(mode, model))
    except Exception:
        return ""
    if not ev:
        return ""
    f.evidence["пункт нормы"] = f"{ev['document']}, {ev['number']}"
    f.evidence["цитата нормы"] = ev["quote"]
    return f"{ev['document']}, {ev['number']}: {ev['quote'][:500]}"


def verify(doc, scr, findings: list[Finding]) -> list[Finding]:
    if not findings:
        return findings
    pline = scr.scrub(passport.line(doc))
    practice = passport.practice(doc)
    blocks = []
    for n, f in enumerate(findings):
        norm = _norm_fragment(f)
        if norm:
            f.trace.append({"step": "norm", "text": f.evidence.get("пункт нормы", ""), "quote": f.evidence.get("цитата нормы", "")})
        blocks.append(f"{n}. [{f.category}; важность {f.severity}] цитата [{f.block_id}]: «{scr.scrub(f.quote[:300])}»\n"
                      f"   Довод: {scr.scrub(f.explanation[:500])}\n   Контекст:\n{_context(doc, f, scr)}"
                      + (f"\n   Пункт нормы (найден поиском): {scr.scrub(norm)}" if norm else ""))
    batches, cur, size = [], [], 0
    for n, text in enumerate(blocks):
        if cur and (size + len(text) > 12000 or len(cur) >= 8):
            batches.append(cur)
            cur, size = [], 0
        cur.append(n)
        size += len(text)
    if cur:
        batches.append(cur)
    decided = {}

    def work(batch):
        prompt = VERIFY_PROMPT.format(passport=pline, practice=practice + "\n" if practice else "", items=wrap("\n\n".join(blocks[n] for n in batch)))
        unit = unit_of("finding", f"замечаний: {len(batch)}", [findings[n].block_id for n in batch])
        return batch, ask(doc, prompt, 4000, "вторая проверка", "verify", "Вторая проверка замечаний", unit) or {}

    calls = {}
    for batch, data in pmap(work, batches):
        for n in batch:
            calls[n] = data.get("_call", "")
        for d in data.get("decisions") or []:
            try:
                n = int(d.get("n"))
            except (TypeError, ValueError, AttributeError):
                continue
            if n in batch:
                decided[n] = d
    out = []
    for n, f in enumerate(findings):
        d = decided.get(n)
        if not d:
            f.evidence["вторая проверка"] = "не проведена"
            f.needs_review = True
            f.trace.append({"step": "verify", "call": calls.get(n, ""), "item": None, "text": "решения нет: на ручную проверку"})
            out.append(f)
            continue
        dec = str(d.get("decision"))
        f.trace.append({"step": "verify", "call": calls.get(n, ""), "item": d,
                        "text": {"keep": "подтверждено", "lower": "понижено", "reject": "снято"}.get(dec, dec)
                        + (f": {scr.restore(str(d.get('reason') or ''))[:300]}" if d.get("reason") else "")})
        f.evidence["вторая проверка"] = {"keep": "подтверждено", "lower": "понижено: на ручную проверку", "reject": "снято"}.get(dec, dec)
        f.evidence["довод проверки"] = scr.restore(str(d.get("reason") or ""))[:300]
        confidence.set_model_score(f, d.get("confidence"), confidence.STAGE_VERIFY)
        if dec == "reject":
            doc.llm_rejected.append(f)
            continue
        if dec == "lower":
            f.severity = SEV[max(0, SEV.index(f.severity) - 1)]
            f.needs_review = True
        out.append(f)
    return out


def dedupe(findings: list[Finding]) -> list[Finding]:
    out = []
    for f in findings:
        dup = next((x for x in out if x.block_id == f.block_id and x.category == f.category
                    and (x.quote.lower() in f.quote.lower() or f.quote.lower() in x.quote.lower())), None)
        if dup is None:
            out.append(f)
        elif f.severity in SEV and SEV.index(f.severity) > SEV.index(dup.severity) and len(f.explanation) > len(dup.explanation):
            f.trace.append({"step": "dedupe", "text": f"объединено с дублем того же места: «{dup.quote[:80]}»"})
            out[out.index(dup)] = f
        else:
            dup.trace.append({"step": "dedupe", "text": f"объединено с дублем того же места: «{f.quote[:80]}»"})
    return out


def summary_json(doc) -> str:
    return json.dumps({"passport": getattr(doc, "passport", {}), "calls": getattr(doc, "model_calls", {})}, ensure_ascii=False)
