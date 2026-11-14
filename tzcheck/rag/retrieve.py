"""Кандидаты пунктов норм для замечания.

Запрос собирается из цитаты ТЗ, объяснения, категории и предмета закупки. Поиск идёт двумя дорожками:
законы и акты (223-ФЗ, 102-ФЗ, ГК РФ, техрегламенты) и стандарты (ГОСТ, СП, СНиП). Без дорожек законы
почти не попадают в выдачу: в корпусе 13 тысяч пунктов стандартов против 2,5 тысяч частей законов, и запрос
про конкретный прибор лексически ближе к стандарту на прибор, чем к ст. 3 ч. 6.1 223-ФЗ (eval/rag/miss_analysis.py).
Для дорожки законов к запросу добавляется правовая тема категории словами закона.

Кандидаты не прикладываются к замечанию сами: их проверяет модель (tzcheck/rag/verify.py).
"""
import re

from . import (LAWS, _public, law_targets, mentioned_documents, explicit_unknown, part_hint, scope_row)

LAW_NAME = re.compile(r"ФЗ|ГК РФ|ТР ТС|ТР ЕАЭС|Постановлени|Кодекс|Приказ|Решение", re.I)
# Правовая тема категории словами закона: только для дорожки законов
TOPIC = {
    "Ограничение конкуренции": "описание предмета закупки товарные знаки фирменные наименования эквивалент "
                               "необоснованное ограничение количества участников закупки",
    "Некорректная характеристика": "описание предмета закупки функциональные технические качественные характеристики товара",
    "Неполнота или комплектность": "описание предмета закупки функциональные технические качественные характеристики "
                                   "количество комплектность товара",
    "Стандарты (ГОСТ, поверка)": "требования к товару технические регламенты документы по стандартизации",
    "Гарантийные условия": "гарантия качества гарантийный срок",
    "Предмет закупки или наименование": "описание предмета закупки",
    "Лишнее в ТЗ": "описание предмета закупки товарные знаки требования к товару",
    "Противоречие": "",
    "Неверный или общий код ОКПД 2": "",
    "Оформление по внутреннему стандарту": "",
}
METROLOGY = re.compile(r"поверк|средств\w*\s+измерени|\bСИ\b|утвержд\w*\s+тип|метролог|калибровк", re.I)
METROLOGY_TOPIC = "средства измерений утвержденного типа поверка обеспечение единства измерений"
SUBJECTS = [re.compile(p, re.I) for p in (
    r"(?:предмет|объект)\w*\s+(?:закупки|договора)\s*[:\-–—]\s*([^.;\n|]{5,200})",
    r"\b((?:поставк|выполнени|оказани)[аеиуя]\s+(?:работ|услуг|товар|оборудован|измерительн|метролог|лабораторн|медицинск|"
    r"мебел|сервер|по\s|клининг)[^.;\n|]{3,200})",
    r"\bзакупк[аи]\s+(оборудования[^.;\n|]{3,200})",
)]


def subject_of(title: str, text: str = "") -> str:
    """Предмет закупки из заголовка или начала ТЗ: «поставка …», «выполнение работ по …»; до 120 символов."""
    for src in (title or "", (text or "")[:6000]):
        for rx in SUBJECTS:
            m = rx.search(src)
            if m:
                s = re.sub(r"\s+", " ", m.group(1)).strip(" ,:()")
                if len(s) > 120:
                    s = s[:s.rfind(" ", 0, 120)]
                return s
    return ""


def is_law(document: str) -> bool:
    return document in LAWS or bool(LAW_NAME.search(document))


def lanes(rows) -> tuple[set, set]:
    docs = {r["document"] for r in rows}
    law = {d for d in docs if is_law(d)}
    return law, docs - law


def queries(quote: str, explanation: str, category: str = "", subject: str = "") -> dict:
    """Запрос для дорожки стандартов (предмет, цитата, объяснение) и для дорожки законов (то же с правовой темой)."""
    base = " ".join(x for x in (quote, explanation) if x).strip()
    std = " ".join(x for x in (subject, base) if x)
    topic = TOPIC.get(category, "")
    if METROLOGY.search(base) and category in ("Стандарты (ГОСТ, поверка)", "Некорректная характеристика", "Неполнота или комплектность", ""):
        topic = (topic + " " + METROLOGY_TOPIC).strip()
    law = " ".join(x for x in (topic, category, base) if x)
    return {"std": std, "law": law, "base": base}


def _distinct(hits, n):
    out, seen = [], set()
    for h in hits:
        key = (h["document"], h["number"])
        if key not in seen:
            seen.add(key)
            out.append(h)
    return out[:n]


def _lane(search, query, documents, limit):
    if documents is not None and not documents:
        return []
    try:
        return search.search(query, limit=limit, threshold=-1e9, documents=documents)
    except TypeError:
        return []


def _parts(search) -> dict:
    """(документ, номер, вид) -> фрагменты пункта по порядку: длинный пункт порезан на окна, модели он идёт целиком."""
    parts = getattr(search, "_part_rows", None)
    if parts is None:
        parts = {}
        for r in search.rows:
            parts.setdefault((r["document"], r["number"], r.get("kind", "clause")), []).append(r)
        search._part_rows = parts
    return parts


def whole_part(search, hit: dict) -> dict:
    """Кандидат на весь пункт: текст всех окон через перевод строки, ids окон в window_ids."""
    rows = _parts(search).get((hit["document"], hit["number"], hit.get("kind", "clause")), [])
    if len(rows) <= 1 or hit.get("kind", "clause") != "clause":
        return hit
    return dict(hit, id=rows[0]["id"], text="\n".join(r["text"] for r in rows), window_ids=[r["id"] for r in rows])


def candidates(search, quote: str, explanation: str = "", category: str = "", subject: str = "", norm_ids=(),
               about_document: bool = False, k: int = 6, per_lane: int = 4, use_lanes: bool = True, use_topic: bool = True,
               order: str = "interleave") -> list[dict]:
    """До k пунктов-кандидатов: статья из norm_ids, пункты названного стандарта, затем дорожки законов и стандартов.
    order: interleave (закон, стандарт, закон, ...) или score (по оценке гибрида: косинус или вероятность реранкера).
    У каждого кандидата поле via: откуда он взят."""
    rows = search.rows
    q = queries(quote, explanation, category, subject)
    text = q["base"]
    out, seen = [], set()

    def add(hits, via, limit=None):
        n = 0
        for h in hits:
            key = (h["document"], h["number"], h.get("kind", "clause"))
            if key in seen:
                continue
            if limit is not None and n >= limit:
                break
            seen.add(key)
            out.append(dict(whole_part(search, h), via=via))
            n += 1

    arts = law_targets(norm_ids)
    if arts:
        hints = part_hint(norm_ids)
        hits = search.search(text, limit=10, threshold=-1e9, articles=arts)
        exact = [h for h in hits if h["number"] in hints]
        if not exact:
            exact = [dict(_public(r), score=0.0) for r in rows if (r["document"], r.get("article")) in arts and r["number"] in hints]
        add((exact + [h for h in hits if h not in exact])[:3], "статья из базы норм")
    docs = mentioned_documents(text, rows)
    if docs:
        if about_document:
            scope = scope_row(rows, docs)
            if scope is not None:
                add([dict(_public(scope), score=0.0)], "область применения стандарта, названного в тексте")
        add(_lane(search, q["std"], docs, 3), "по обозначению стандарта в тексте")
    elif explicit_unknown(text, rows) and not arts:
        # назван стандарт, которого нет в корпусе: пункты других стандартов основанием не будут
        law, _ = lanes(rows)
        add(_lane(search, q["law"] if use_topic else q["std"], law, per_lane * 3), "поиск по законам", per_lane)
        return out[:k]
    if use_lanes:
        law, std = lanes(rows)
        lh = _distinct(_lane(search, q["law"] if use_topic else q["std"], law, per_lane * 3), per_lane)
        sh = _distinct(_lane(search, q["std"], std, per_lane * 3), per_lane)
        merged = []
        for i in range(max(len(lh), len(sh))):
            merged += [(h, "поиск по законам") for h in lh[i:i + 1]] + [(h, "поиск по стандартам") for h in sh[i:i + 1]]
        if order == "score" and getattr(search, "mode", "bm25") != "bm25":
            merged.sort(key=lambda x: -float(x[0].get("score") or 0))
        for h, via in merged:
            add([h], via)
    else:
        add(_lane(search, q["std"] if not use_topic else q["law"], None, per_lane * 6), "поиск по корпусу", per_lane * 2)
    return out[:k]
