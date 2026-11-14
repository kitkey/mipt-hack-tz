"""Ядро проверки ТЗ: все решения «есть дефект / нет дефекта» принимает модель.

Код собирает контекст (паспорт, позиция с подписанными столбцами, общие условия, справочные записи, кандидаты поиска),
проверяет, что цитата модели дословно есть в тексте, и привязывает замечание к месту. Шаги:
find — дефекты по позиции или разделу; ref — обозначения, которые назвала модель, по справочникам; absent — «не указано»
против кандидатов поиска по всему комплекту (найденное в комплекте снимает замечание); cross — одна характеристика позиции
в разных частях; verify — независимая вторая проверка: часть комплекта целиком, места из других частей, найденные поиском
по доводу, и всё, на чём довод стоит. Она оставляет замечание, только если дефект виден из текста, понижает то, что зависит
от сведений вне комплекта, и снимает остальное; повысить замечание она не может."""
import json
import os
import re

from . import chunks, confidence, passport, search, tables
from .ask import ask, pmap, unit_of, wrap
from .model import Finding
from .norms import CATEGORIES, CATEGORY_GROUP, CATEGORY_NOTES, NORMS, category_notes, norm_list_for_prompt

SEV = ("low", "medium", "high")
UNIT_CHARS = 5000
TERMS_CHARS = 2500
CROSS_PLACE_CHARS = 800
REF_RECORDS = 15

FIND_PROMPT = """Задача: найти дефекты технического задания в одной части: {what}.
Паспорт закупки: {passport}
{practice}
Общие условия из других разделов, которые могут относиться к этой части:
{terms}
Категории (только эти), по группам:
{categories}

Что относится к категории:
{notes}

Нормы (id: суть):
{norms}

Проверяемая часть. Строки в формате [id] текст; у строк таблицы подписаны столбцы.
{data}

Найди то, что мешает закупке: участник не поймёт, что предложить; заказчик не сможет однозначно принять результат; описание сужает круг
участников без основания; части документа противоречат друг другу; ссылки на неверные или недействующие документы; лишнее для технического
задания. Требования к конкуренции оценивай с учётом способа закупки из паспорта. Какие характеристики нужны, решай по виду товара, работы
или услуги и не требуй того, чего у такого предмета не бывает. Не считай дефектом то, что указано в общих условиях. Корректные
формулировки не помечай.
В одной строке или ячейке может быть несколько дефектов разных категорий: каждый назови отдельным замечанием со своей цитатой, не
останавливайся на первом. Если в строке или ячейке перечислено несколько изделий, подпунктов или характеристик, проверь каждое:
наименование, значение и единицу каждой характеристики, полноту состава, обозначения. Замечание о товарном знаке не заменяет проверку
характеристик и комплектности того же изделия.

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
Для каждого утверждения дана исходная часть вокруг замечания (строка замечания отмечена знаком ») и места из всего комплекта,
найденные поиском: [id] (роль части; место) текст вокруг слов запроса.
Реши по показанному тексту:
- "not_found": показанные места явно относятся к этой теме (там такие сведения и должны стоять), и нужных сведений в них нет, утверждение верно;
- "same": это указано в исходной части или в общих условиях, которые к ней относятся, утверждение неверно;
- "other": это указано в другой части комплекта (договор, другой раздел, приложение), утверждение неверно;
- "unclear": показанного текста мало для решения: место, где такие сведения обычно стоят, не показано или обрезано.
Для same и other в "block_id" укажи id показанного места, где это указано, в "quote" дословную цитату из него (3-25 слов) с самими
сведениями (значение, срок, перечень, условие), а не повтор фрагмента, о котором утверждение: без такой цитаты решение не принимается.
Место, где сказано так же общо, как в замечании, сведениями не считается.

{items}

Верни JSON: {{"decisions": [{{"n": номер, "decision": "not_found|same|other|unclear", "block_id": "...", "quote": "...", "reason": "коротко"}}]}}"""

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
Ниже части комплекта и замечания к ним. К части дан её текст и, если есть, другие места комплекта: соседние с цитатой и найденные поиском
по доводу (они могут не относиться к делу). Текст получен разбором файла: [id] в начале строк и подписи столбцов добавил разбор; оборванные
и повторённые слова, склеенные строки, потерянные номера и маркеры списков, служебные строки шаблона таблицы и поля формы, которые
заполняют при заключении договора, не дефекты ТЗ.

Замечание остаётся, только если дефект виден из текста. По каждому замечанию:
1. Найди цитату в тексте части и проверь, что довод передаёт её верно с учётом соседних строк: строку-заголовок группы без значения
   раскрывают её подпункты, значение может стоять в соседней строке или столбце.
2. Если довод в том, что чего-то нет (не указано, не определено, не расшифровано, не раскрыто, не обосновано), ищи это в тексте части и
   в других местах комплекта: в списке сокращений и терминов, общих условиях, приложениях, договоре. Отсылка к другому разделу, приложению,
   договору или нормативному документу сама по себе не дефект.
3. Если довод в противоречии, найди оба значения и проверь, что они об одном и том же и на самом деле расходятся.
4. Реши, исправил бы это закупщик до публикации. Дефект: участник не поймёт, что предложить; у требования к товару, работе, услуге или
   их результату нет измеримого значения или проверяемого критерия, и заказчик не сможет однозначно принять результат; круг участников
   сужен без основания (товарный знак, производитель или параметры одной модели без слов «или эквивалент» и без обоснования); части
   комплекта противоречат друг другу; в тексте явная ошибка (опечатка в слове, числе или единице, повтор абзаца, значение от другой
   характеристики). Правка по вкусу без такого последствия не дефект.

Решение:
- "keep": дефект доказан текстом; в "reason" назови, что именно не так. Явная ошибка в тексте (опечатка, не то слово, число или
  единица) остаётся и тогда, когда смысл угадывается: её исправляют до публикации;
- "lower": довод по существу верен, но вывод зависит от сведений вне комплекта (статус или действующая редакция стандарта, реестр,
  классификатор, оборудование заказчика) или от сведений по памяти, которые могли измениться (ставки, редакции законов), а справочной
  записи нет: на ручную проверку;
- "reject": всё остальное: фрагмент корректен; нужное есть в тексте части или в других местах комплекта; довод читает текст неверно;
  противоречия нет; след разбора или незаполненное поле формы; обычная формулировка о правах, обязанностях и ответственности сторон,
  расчётах и претензиях; выбор заказчика, который не указывает на одного производителя; пожелание подробностей, без которых закупку
  можно провести и результат принять; предположение («возможно», «может быть») без подтверждения в тексте.
Сомнение без подтверждения в тексте это "reject", а не "lower".

{items}

Замечаний в запросе: {count}, их номера: {numbers}. Строки частей и других мест комплекта не замечания. Дай по одному решению на каждый
номер замечания и больше ничего.
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
        if len(b.text) > UNIT_CHARS:
            # один блок длиннее предела: куски по границам строк и фраз, каждый своей частью, хвост не теряется
            flush()
            cur, size, key = [], 0, k
            for piece in chunks.split(scr.scrub(b.text), UNIT_CHARS):
                if piece.strip():
                    out.append(Unit("section", b.section or chunks.clip(b.text, 80), [b.id],
                                    f"[{b.id}] ({scr.scrub(b.section[:60])}) {piece.strip()}", section=b.section))
            continue
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
            part, ids = [], []
            for ln in text.split("\n")[1:]:
                bid = ln[1:ln.index("]")] if ln.startswith("[") and "]" in ln else ""
                for piece in chunks.lines(ln, UNIT_CHARS):
                    if part and sum(len(x) for x in part) + len(piece) > UNIT_CHARS:
                        out.append(Unit("position", p.name[:80], ids, header + "\n" + "\n".join(part), p))
                        part, ids = [], []
                    part.append(piece)
                    if bid and bid not in ids:
                        ids.append(bid)
            if part:
                out.append(Unit("position", p.name[:80], ids, header + "\n" + "\n".join(part), p))
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
    query = unit.title + " " + " ".join(c.name for c in (unit.position.chars if unit.position else [])[:20])
    if sum(len(b.text) for b in blocks) > TERMS_CHARS:
        idx = search.of(doc)
        picked = [b for b, _ in idx.top(query, 10, exclude=unit.block_ids, only=lambda x: x.role in ("terms", "warranty"))]
        blocks = picked or blocks[:8]
    out, used = [], 0
    for b in blocks:
        t = f"[{b.id}] {scr.scrub(chunks.window(b.text, query, 700))}"
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
                                    categories=ctext, notes=category_notes(cats), norms=norms, data=wrap(u.text))
        title = "Поиск замечаний: " + what
        data = ask(doc, prompt, 6000, "поиск замечаний", "find", title, unit_of(u.kind, u.title, u.block_ids)) or {}
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
        for lk in u.lookups:
            key = (lk["type"], str(lk["value"]).strip().lower())
            if key in seen:
                continue
            seen.add(key)
            rec = _record(doc, lk, scr)
            if rec:
                records.append(rec)
        blocks = [doc.block(x) for x in u.block_ids if doc.block(x)]
        out = []
        for start in range(0, len(records), REF_RECORDS):
            part = records[start:start + REF_RECORDS]
            prompt = REF_PROMPT.format(passport=pline, data=wrap(u.text), records="\n".join(f"{n}. {scr.scrub(r)}" for n, r in enumerate(part, 1)),
                                       categories="; ".join(ref_cats), norm_ids=", ".join(k for k in NORMS if k != "INTERNAL"))
            data = ask(doc, prompt, 4000, "справочники", "refs", f"Справочные записи: {u.title[:100]}", unit_of(u.kind, u.title, u.block_ids)) or {}
            call = data.get("_call", "")
            for f in (_finding(doc, i, blocks, scr, "model:ref", ref_cats, call) for i in data.get("findings") or []):
                if not f:
                    continue
                f._unit, f._records = u, part
                f.evidence["справочные записи"] = list(part)
                f.trace.insert(2, {"step": "reference", "text": "", "records": f.evidence["справочные записи"]})
                out.append(f)
        return out

    found = []
    for res in pmap(work, units):
        found.extend(res)
    return found


def _role(b) -> str:
    return passport.ROLES.get(b.role, "роль не определена")


def _rows_of(doc, scr, p) -> tuple[str, list, list]:
    """Позиция построчно: строка-заголовок, id строк и строки с подписями столбцов (как в запросе поиска дефектов)."""
    lines = tables.render(doc, p, scr, limit=10 ** 6).split("\n")
    rows = [ln for ln in lines[1:] if ln.startswith("[") and "] " in ln]
    return lines[0], [ln[1:ln.index("] ")] for ln in rows], rows


def _place(doc, scr, b, query: str, size: int) -> str:
    """Найденное место: роль части, где стоит, и текст вокруг слов запроса; у строки таблицы заголовок её позиции и подписи столбцов."""
    p = tables.position(doc, b.id)
    if p is not None:
        head, ids, rows = _rows_of(doc, scr, p)
        if b.id in ids:
            row = rows[ids.index(b.id)].split("] ", 1)[1]
            return f"[{b.id}] ({_role(b)}; {b.location()}; {head[:150]}) {chunks.window(row, query, size)}"
    return f"[{b.id}] ({_role(b)}; {b.location()}) {scr.scrub(chunks.window(b.text, query, size))}"


def _around(doc, scr, f: Finding, size: int) -> tuple[str, list]:
    """Исходная часть вокруг замечания: строки его позиции с подписями столбцов или соседние блоки того же файла по обе стороны
    от блока замечания, строка замечания отмечена «»». Возвращает текст и показанные блоки."""
    p = tables.position(doc, f.block_id)
    head, ids, rows = _rows_of(doc, scr, p) if p is not None else ("", [], [])
    if f.block_id in ids:
        def line(j):
            bid, body = rows[j][1:].split("] ", 1)
            return f"[{bid}] ", body
    else:
        b0 = doc.block(f.block_id)
        same = [b for b in doc.blocks if b0 is not None and b.source == b0.source]
        head, ids = "", [b.id for b in same]

        def line(j):
            return f"[{same[j].id}] ({scr.scrub(same[j].section[:60])}) ", scr.scrub(same[j].text)
    if f.block_id not in ids:
        return "", []
    c = ids.index(f.block_id)
    pre, body = line(c)
    picked = {c: "» " + pre + chunks.window(body, f.quote, size // 2)}
    used = len(head) + len(picked[c])
    for d in range(1, len(ids)):
        for j in (c + d, c - d):
            if 0 <= j < len(ids) and used < size:
                pre, body = line(j)
                t = pre + chunks.clip(body, 500)
                if used + len(t) <= size:
                    picked[j] = t
                used += len(t)
        if used >= size:
            break
    order = sorted(picked)
    text = "\n".join(([head] if head else []) + [picked[j] for j in order])
    return text, [doc.block(ids[j]) for j in order if doc.block(ids[j])]


def _proof(doc, scr, f: Finding, d: dict, blocks: list) -> tuple | None:
    """Цитата модели, где сведения указаны, дословно в одном из показанных ей мест (сначала в названном блоке). Цитата из блока
    замечания, которая целиком входит в его же цитату, доказательством не считается: это сам спорный фрагмент. Обрывок короче
    трёх слов тоже: в нём нет самих сведений."""
    pref = str(d.get("block_id") or "")
    quote = scr.restore(str(d.get("quote") or "")).strip(" …»")
    if len(quote.split()) < 3:
        return None
    hit = locate(quote, sorted(blocks, key=lambda b: b.id != pref), _labels(doc))
    if hit and hit[0].id == f.block_id and _norm(hit[1]).lower() in _norm(f.quote).lower():
        return None
    return hit


ABSENT_BATCH = 6
ABSENT_SOURCE_CHARS = 2000


def absent(doc, scr, findings: list[Finding]) -> list[Finding]:
    """Утверждения «не указано»: модель видит исходную часть вокруг замечания (своя часть из поиска не исключается) и места всего
    комплекта вокруг слов запроса. Снять замечание можно только с дословной цитатой показанного места, где сведения указаны;
    «мало контекста» оставляет замечание на ручную проверку с меньшей важностью."""
    claims = [f for f in findings if getattr(f, "_absent", False)]
    if not claims:
        return findings
    idx = search.of(doc)
    pline = scr.scrub(passport.line(doc))
    items, cands, shown = [], {}, {}
    for n, f in enumerate(claims, 1):
        u = getattr(f, "_unit", None)
        p = tables.position(doc, f.block_id)
        what = getattr(f, "_missing", "") or f.explanation
        obj = p.name if p is not None and p.name else (u.title if u else "")
        src, src_blocks = _around(doc, scr, f, ABSENT_SOURCE_CHARS)
        seen = {b.id for b in src_blocks} | {f.block_id}
        query = what + " " + obj
        hits = idx.top(query, 6, exclude=seen)
        hits += idx.top(query, 2, exclude=seen | {b.id for b, _ in hits}, only=lambda b: b.role in ("terms", "warranty"))
        cands[n] = hits
        shown[n] = src_blocks + [b for b, _ in hits]
        lines = "\n".join("   " + _place(doc, scr, b, what, 400) for b, _ in hits) or "   кандидатов нет"
        items.append(f"{n}. Утверждение: {scr.scrub(chunks.clip(f.explanation, 400))} Чего нет: {scr.scrub(chunks.clip(what, 200))}. "
                     f"Где: [{f.block_id}] ({scr.scrub(obj[:80])})\n   Исходная часть:\n{src or '   не найдена'}\n   Кандидаты:\n{lines}")
    batches = [list(range(s, min(s + ABSENT_BATCH, len(claims)))) for s in range(0, len(claims), ABSENT_BATCH)]

    def work(batch):
        unit = unit_of("finding", f"утверждения «не указано» ({len(batch)})", [claims[i].block_id for i in batch])
        return batch, ask(doc, ABSENT_PROMPT.format(passport=pline, items=wrap("\n\n".join(items[i] for i in batch))), 3000,
                          "проверка «не указано»", "absent", "Проверка «не указано» по всему комплекту", unit) or {}

    decided, calls = {}, {}
    for batch, data in pmap(work, batches):
        for i in batch:
            calls[i + 1] = data.get("_call", "")
        for d in data.get("decisions") or []:
            try:
                n = int(d.get("n"))
            except (TypeError, ValueError, AttributeError):
                continue
            if n - 1 in batch:
                decided[n] = d
    out, number = [], {id(f): n for n, f in enumerate(claims, 1)}
    for f in findings:
        n = number.get(id(f))
        if n is None:
            out.append(f)
            continue
        d = decided.get(n)
        hits = cands.get(n) or []
        f.evidence["поиск по комплекту"] = [f"{b.location()}: {b.text[:160]}" for b, _ in hits[:3]] or "кандидатов не найдено"
        why = scr.restore(str((d or {}).get("reason") or ""))
        tail = f". {why}" if why else ""
        dec = str((d or {}).get("decision"))
        if dec not in ("same", "other", "not_found", "unclear"):
            d = None
        # «указано» принимается только с дословной цитатой из показанного места: место, которого модель не видела, пересказ или повтор
        # спорного фрагмента утверждение не снимают
        proof = _proof(doc, scr, f, d, shown.get(n) or []) if d and dec in ("same", "other") else None
        if d and dec in ("same", "other") and proof is None:
            dec = "unproved"
        where, quote = proof or (None, "")
        verdict = {"same": f"снято: указано в той же части или общих условиях{tail}", "other": f"снято: указано в другой части комплекта{tail}",
                   "unproved": f"модель не назвала место, где это указано: на проверку{tail}",
                   "unclear": f"на проверку с меньшей важностью: мало контекста{tail}",
                   "not_found": f"оставлено: не найдено по комплекту{tail}"}.get(dec if d else "", "решения модели нет, на проверку")
        if where is not None:
            verdict += f"; цитата: {where.location()} (блок {where.id})"
        f.trace.append({"step": "absent_search", "call": calls.get(n, ""), "item": d,
                        "text": f"поиск по комплекту: кандидатов {len(hits)} ({', '.join(b.id for b, _ in hits)}); {verdict}"})
        if not d or dec == "unproved":
            f.evidence["проверка «не указано»"] = "не проведена" if not d else f"не подтверждена: модель не назвала место{tail}"
            f.needs_review = True
            out.append(f)
            continue
        if dec in ("same", "other"):
            # утверждение «не указано» опровергнуто текстом комплекта; расхождения между местами ищет сверка частей
            f.evidence["проверка «не указано»"] = (("указано: " if dec == "same" else "указано в другой части комплекта: ")
                                                   + f"{where.location()}: «{chunks.clip(quote, 200)}»{tail}")
            doc.llm_rejected.append(f)
            continue
        if dec == "unclear":
            f.severity = SEV[max(0, SEV.index(f.severity) - 1)] if f.severity in SEV else "low"
            f.needs_review = True
            f.evidence["проверка «не указано»"] = f"мало контекста для решения{tail}"
            out.append(f)
            continue
        f.evidence["проверка «не указано»"] = f"не найдено по комплекту{tail}"
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
            query = u.text
            hits = idx.top(query, 4, exclude=mine, only=lambda b, s=src: b.source != s or b.role in ("contract", "spec"))
        if not hits:
            continue
        lines = "\n".join(f"   [{b.id}] ({_role(b)}; {b.location()}) {scr.scrub(chunks.window(b.text, query, CROSS_PLACE_CHARS))}" for b, _ in hits)
        items.append((u, f"{u.text}\n   Места из других частей:\n{lines}", [b for b, _ in hits]))
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


VERIFY_CHARS = 12000
VERIFY_ITEMS = 8
NEAR_BLOCKS = 2
SEARCH_HITS = 2
NAMED_HITS = 2
PLACE_CHARS = 350


def _index(doc) -> dict:
    order = getattr(doc, "_block_order", None)
    if order is None:
        order = doc._block_order = {b.id: i for i, b in enumerate(doc.blocks)}
    return order


def _part(doc, f: Finding, scr) -> tuple:
    """Часть комплекта для второй проверки: (ключ, подпись, текст, id блоков). Часть, где замечание найдено, идёт целиком и содержит
    блок цитаты; без неё берётся позиция, если строка цитаты попала в её текст, иначе блоки вокруг цитаты."""
    u = getattr(f, "_unit", None)
    if u is not None and f.block_id in u.block_ids:
        return ("unit", id(u)), u.title, u.text, set(u.block_ids)
    p = tables.position(doc, f.block_id)
    if p is not None:
        text = tables.render(doc, p, scr, limit=6000)
        if f"[{f.block_id}]" in text:
            return ("position", p.id), p.name, text, set(p.block_ids)
    i = _index(doc).get(f.block_id)
    if i is None:
        return ("block", f.block_id), "", "", set()
    near = doc.blocks[max(0, i - 4):i + 5]
    text = "\n".join(f"[{b.id}] ({scr.scrub(b.section[:60])}) {scr.scrub(chunks.window(b.text, f.quote, 3000 if b.id == f.block_id else 1200))}"
                     for b in near)
    return ("block", f.block_id), doc.blocks[i].section, text, {b.id for b in near}


def _elsewhere(doc, f: Finding, own: set) -> list:
    """Места вне части, которые могут снять или подтвердить довод: блоки рядом с цитатой и найденное поиском по доводу.
    Относится ли место к делу, решает модель."""
    out = []
    i = _index(doc).get(f.block_id)
    if i is not None:
        out += [b for b in doc.blocks[max(0, i - NEAR_BLOCKS):i + NEAR_BLOCKS + 1] if b.id not in own]
    idx = search.of(doc)
    # сначала то, что довод называет сам: чего нет и слова в кавычках (термин, сокращение, документ); затем довод целиком
    terms = _quoted(f.explanation)
    out += _defined(doc, terms, own | {b.id for b in out})
    named = " ".join([getattr(f, "_missing", "")] + terms).strip()
    for query, k, need in ((named, NAMED_HITS, 0), (f"{f.quote} {f.explanation}", SEARCH_HITS, 1)):
        if query:
            out += [b for b, _ in idx.top(query, k, exclude=own | {b.id for b in out}, min_match=need)]
    return out


def _defined(doc, terms: list[str], skip: set) -> list:
    """Блоки, где названный в доводе термин стоит отдельной ячейкой таблицы или открывает абзац перед тире или двоеточием:
    так оформляют списки сокращений и определений. Это подбор мест; есть ли там определение, решает модель."""
    want = {_norm(t).lower() for t in terms if 1 < len(_norm(t)) <= 40}
    out = []
    for b in doc.blocks if want else []:
        if b.id in skip:
            continue
        head = _norm(b.text).lower()
        if any(_norm(c).lower() in want for c in b.cells or []) or any(head.startswith(t + x) for t in want for x in (" –", " —", " -", ":")):
            out.append(b)
            if len(out) >= NAMED_HITS:
                break
    return out


def _quoted(text: str) -> list[str]:
    """Фрагменты в кавычках «…» из довода модели: о чём именно она говорит."""
    out, i = [], 0
    while True:
        a = text.find("«", i)
        b = text.find("»", a + 1) if a >= 0 else -1
        if b < 0:
            return out
        if 1 < b - a - 1 <= 80:
            out.append(text[a + 1:b])
        i = b + 1


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
    return f"{ev['document']}, {ev['number']}: {ev['quote']}"


def _claim(doc, n: int, f: Finding, scr, places: list) -> str:
    """Замечание для второй проверки: цитата, довод и всё, на чём довод стоит (справочные записи, пункт стандарта, итог поиска)."""
    lines = [f"{n}. [{f.category}; важность {f.severity}] цитата [{f.block_id}]: «{scr.scrub(chunks.clip(f.quote, 400))}»",
             f"   Довод: {scr.scrub(chunks.clip(f.explanation, 600))}"]
    if getattr(f, "_missing", ""):
        lines.append(f"   Чего нет по доводу: {scr.scrub(chunks.clip(f._missing, 200))}")
    if isinstance(f.evidence.get("проверка «не указано»"), str):
        lines.append(f"   Поиск по комплекту: {scr.scrub(chunks.clip(f.evidence['проверка «не указано»'], 600))}")
    b2 = doc.block(f.evidence["второй блок"]) if f.evidence.get("второй блок") else None
    if b2:
        lines.append(f"   Второе место [{b2.id}] ({_role(b2)}): {scr.scrub(chunks.window(b2.text, str(f.evidence.get('вторая цитата') or f.quote), 800))}")
    records = getattr(f, "_records", None) or f.evidence.get("справочные записи")
    if isinstance(records, list) and records:
        lines.append("   Справочные записи: " + " / ".join(scr.scrub(str(r)) for r in records))
    gost = f.evidence.get("пункт ГОСТ")
    if isinstance(gost, dict):
        lines.append(f"   Пункт стандарта: {gost.get('designation')} п. {gost.get('clause')}: {scr.scrub(str(gost.get('quote') or ''))}")
    norm = _norm_fragment(f)
    if norm:
        f.trace.append({"step": "norm", "text": f.evidence.get("пункт нормы", ""), "quote": f.evidence.get("цитата нормы", "")})
        lines.append(f"   Пункт нормы (найден поиском): {scr.scrub(norm)}")
    if places:
        lines.append("   Другие места по этому замечанию: " + ", ".join(f"[{b.id}]" for b in places))
    return "\n".join(lines)


def _verify_chunks(doc, scr, findings: list[Finding]) -> list[tuple]:
    """Куски второй проверки: [(текст, номера замечаний)]. Замечания одной части идут вместе, часть показывается один раз;
    кусок закрывается на VERIFY_ITEMS замечаниях или когда текст с местами комплекта перерастает VERIFY_CHARS."""
    parts: dict = {}
    for n, f in enumerate(findings):
        key, title, text, own = _part(doc, f, scr)
        parts.setdefault(key, {"title": title, "text": text, "own": own, "items": []})["items"].append(n)
    chunks_ = []
    for g in parts.values():
        head = f"Часть: {scr.scrub(chunks.clip(g['title'], 120))}\n{g['text']}"
        ns, pool, claims = [], {}, []

        def close():
            if not ns:
                return
            norms = sorted({k for n in ns for k in findings[n].norm_ids if k in NORMS and k != "INTERNAL"})
            text = head
            if pool:
                text += "\nДругие места комплекта:\n" + "\n".join(pool.values())
            if norms:
                text += "\nНормы из замечаний:\n" + "\n".join(f"{NORMS[k]['title']}: {NORMS[k]['text']}" for k in norms)
            chunks_.append((text + "\nЗамечания к части:\n" + "\n".join(claims), list(ns)))

        for n in g["items"]:
            f = findings[n]
            places = _elsewhere(doc, f, g["own"])
            query = " ".join([getattr(f, "_missing", ""), f.quote, f.explanation])
            fresh = {b.id: f"[{b.id}] ({_role(b)}; {b.location()}) {scr.scrub(chunks.window(b.text, query, PLACE_CHARS))}"
                     for b in places if b.id not in pool}
            size = len(head) + sum(len(x) for x in pool.values()) + sum(len(x) for x in claims)
            if ns and (len(ns) >= VERIFY_ITEMS or size + sum(len(x) for x in fresh.values()) > VERIFY_CHARS):
                close()
                ns, pool, claims = [], {}, []
                fresh = {b.id: f"[{b.id}] ({_role(b)}; {b.location()}) {scr.scrub(chunks.window(b.text, query, PLACE_CHARS))}" for b in places}
            pool.update(fresh)
            if places:
                f.evidence["другие места комплекта"] = [b.location() for b in places]
            claims.append(_claim(doc, n, f, scr, places))
            ns.append(n)
        close()
    return chunks_


def verify(doc, scr, findings: list[Finding]) -> list[Finding]:
    if not findings:
        return findings
    pline = scr.scrub(passport.line(doc))
    practice = passport.practice(doc)
    batches, cur, size = [], [], 0
    for text, ns in _verify_chunks(doc, scr, findings):
        if cur and (size + len(text) > VERIFY_CHARS or sum(len(x[1]) for x in cur) + len(ns) > VERIFY_ITEMS):
            batches.append(cur)
            cur, size = [], 0
        cur.append((text, ns))
        size += len(text)
    if cur:
        batches.append(cur)
    def run(stage: str, step: str, title: str) -> tuple[dict, dict]:
        """Один проход второй проверки по всем кускам моделью шага stage: решения и id вызовов по номерам замечаний."""
        decided, calls = {}, {}

        def work(batch):
            ns = [n for _, part in batch for n in part]
            prompt = VERIFY_PROMPT.format(passport=pline, practice=practice + "\n" if practice else "", items=wrap("\n\n".join(t for t, _ in batch)),
                                          count=len(ns), numbers=", ".join(str(n) for n in ns))
            unit = unit_of("finding", f"замечания ({len(ns)})", [findings[n].block_id for n in ns])
            # бюджет ответа по числу замечаний: зациклившийся ответ не занимает модель на минуты (нижнюю границу задаёт TZCHECK_LLM_MAX_TOKENS)
            data = {}
            for _ in range(2):
                # ответ без единого годного решения (у части моделей JSON-режим изредка отдаёт мусор) запрашивается ещё раз
                data = ask(doc, prompt, min(4000, 500 + 300 * len(ns)), step, stage, title, unit) or {}
                if any(isinstance(d, dict) and str(d.get("decision")) in ("keep", "lower", "reject") for d in data.get("decisions") or []):
                    break
            return ns, data

        for ns, data in pmap(work, batches):
            for n in ns:
                calls[n] = data.get("_call", "")
            for d in data.get("decisions") or []:
                try:
                    n = int(d.get("n"))
                except (TypeError, ValueError, AttributeError):
                    continue
                if n in ns and isinstance(d, dict):
                    decided[n] = d
        return decided, calls

    decided, calls = run("verify", "вторая проверка", "Вторая проверка замечаний")
    # TZCHECK_VERIFY2_MODEL: те же замечания независимо решает вторая модель; итог по двум решениям (см. _two_checks)
    second = run("verify2", "вторая проверка, вторая модель", "Вторая проверка замечаний второй моделью") if os.environ.get("TZCHECK_VERIFY2_MODEL") else None
    names = {"keep": "подтверждено", "lower": "понижено", "reject": "снято"}
    out = []
    for n, f in enumerate(findings):
        d = decided.get(n)
        dec = str(d.get("decision")) if d else ""
        if second is not None:
            d2 = second[0].get(n)
            dec2 = str(d2.get("decision")) if d2 else ""
            f.trace.append({"step": "verify", "call": calls.get(n, ""), "item": d, "text": "первая модель: " + names.get(dec, dec or "решения нет")
                            + (f": {scr.restore(str(d.get('reason') or ''))}" if d and d.get("reason") else "")})
            f.trace.append({"step": "verify2", "call": second[1].get(n, ""), "item": d2, "text": "вторая модель: " + names.get(dec2, dec2 or "решения нет")
                            + (f": {scr.restore(str(d2.get('reason') or ''))}" if d2 and d2.get("reason") else "")})
            f.evidence["вторая проверка, две модели"] = f"{names.get(dec, 'решения нет')} / {names.get(dec2, 'решения нет')}"
            if d2 and d2.get("reason"):
                f.evidence["довод второй модели"] = scr.restore(str(d2.get("reason") or ""))
            dec, d = _two_checks(dec, dec2), d or d2
        elif d:
            f.trace.append({"step": "verify", "call": calls.get(n, ""), "item": d,
                            "text": names.get(dec, dec) + (f": {scr.restore(str(d.get('reason') or ''))}" if d.get("reason") else "")})
        if not d:
            f.evidence["вторая проверка"] = "не проведена"
            f.needs_review = True
            if second is None:
                f.trace.append({"step": "verify", "call": calls.get(n, ""), "item": None, "text": "решения нет, на проверку"})
            out.append(f)
            continue
        f.evidence["вторая проверка"] = {"keep": "подтверждено", "lower": "понижено, на проверку", "reject": "снято",
                                         "split": "подтверждено одной из двух моделей, на проверку"}.get(dec, dec)
        f.evidence["довод проверки"] = scr.restore(str(d.get("reason") or ""))
        confidence.set_model_score(f, d.get("confidence"), confidence.STAGE_VERIFY)
        if dec == "reject":
            doc.llm_rejected.append(f)
            continue
        if dec == "lower":
            f.severity = SEV[max(0, SEV.index(f.severity) - 1)]
        if dec != "keep":
            f.needs_review = True
        out.append(f)
    return out


def _two_checks(first: str, second: str) -> str:
    """Итог двух независимых вторых проверок: подтверждено, если подтвердили обе; снято, если сняли обе; подтвердила только одна —
    "split" (на ручную проверку без понижения важности); остальное понижено."""
    if first == second and first in ("keep", "reject"):
        return first
    return "split" if "keep" in (first, second) else "lower"


def _distinct(a: Finding, b: Finding) -> bool:
    """Два замечания одного ответа модели с разными доводами: модель сама развела их как разные дефекты одной строки."""
    ca = a.trace[0].get("call") if a.trace else ""
    cb = b.trace[0].get("call") if b.trace else ""
    return bool(ca) and ca == cb and _norm(a.explanation).lower() != _norm(b.explanation).lower()


def dedupe(findings: list[Finding]) -> list[Finding]:
    """Дубль: то же место и категория, одна цитата вложена в другую (поиск и справочники назвали одно и то же, либо модель
    повторила замечание дословно). Разные замечания одного ответа модели на одной цитате остаются оба."""
    out = []
    for f in findings:
        dup = next((x for x in out if x.block_id == f.block_id and x.category == f.category and not _distinct(x, f)
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
