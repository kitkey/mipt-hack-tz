"""Точная замена цитаты для правок в режиме рецензирования (поле replacement замечания).

Замена ставится только там, где правило знает её детерминированно: заменённый ГОСТ на действующий, исправленное
обозначение ГОСТ / ГОСТ Р, срок прописью, единая формулировка начала срока, «мкр» на «мкм», документация на русском,
начало гарантии от документа о приёмке, «(или эквивалент)» после марки. Остальное (добавить характеристику,
перечислить стандарты, замечания модели) остаётся комментарием."""
import re

from .model import SECTION_MAIN

DESIGNATION_RE = re.compile(r"(?:ГОСТ(?:\s+Р)?(?:\s+(?:ISO|ИСО|IEC|МЭК|Р\s+ИСО|Р\s+МЭК))?|СП|СНиП)\s*\d[\d.]*\s*[-–]\s*\d{2,4}")
LANG_PHRASE_RE = re.compile(r"на\s+(?:русском\s+и/или\s+английском|английском|русском\s+или\s+английском)", re.I)
WARRANTY_FROM_RE = re.compile(r"(?:с\s+даты|с\s+момента)\s+при[её]мки\s+товара(?!\s*(?:по|согласно)\s+акт)", re.I)
WARRANTY_OTHER_DOC_RE = re.compile(r",?\s*или\s+иного\s+документа,?\s+подтверждающего\s+при[её]мку(?:\s+товара)?", re.I)
WARRANTY_START_TEXT = "с даты подписания Заказчиком документа о приёмке Товара"
EQUIV = " (или эквивалент)"


def _gost(f) -> str:
    q = f.quote.strip()
    if not DESIGNATION_RE.fullmatch(q):
        return ""
    repl = (f.evidence or {}).get("заменяющий стандарт")
    if repl and f.fix.startswith("Заменить ссылку на") and DESIGNATION_RE.fullmatch(str(repl).strip()):
        return str(repl).strip()
    m = re.fullmatch(r"(.+?)\.", f.fix.strip())
    if m and DESIGNATION_RE.fullmatch(m.group(1).strip()):
        return m.group(1).strip()
    return ""


def _days_in_words(f) -> str:
    return f.fix if re.fullmatch(r"\d+ \([а-яё ]+\) \S.*", f.fix) else ""


def _start_wording(f) -> str:
    m = re.match(r"Заменить «(.+?)» на «(.+?)»", f.fix)
    return m.group(2) if m and m.group(1).strip().lower() == f.quote.strip().lower() else ""


def _unit_typo(f) -> str:
    if not f.fix.startswith("Заменить «мкр» на «мкм»"):
        return ""
    return re.sub(r"(?<![А-Яа-яЁё])мкр(?![А-Яа-яЁё])", "мкм", f.quote)


def _english_docs(f) -> str:
    new = LANG_PHRASE_RE.sub("на русском", f.quote, count=1)
    return re.sub(r"на русском\s+языках", "на русском языке", new)


def _warranty_start(f) -> str:
    new = WARRANTY_FROM_RE.sub(WARRANTY_START_TEXT, f.quote, count=1)
    if new != f.quote:
        new = WARRANTY_OTHER_DOC_RE.sub("", new)
    return new


def _add_equivalent(f) -> str:
    m = re.search(r"\(([^()]+)\) без слов «или эквивалент»", f.explanation)
    if not m or "эквивалент" in f.quote.lower():
        return ""
    names = [n.strip() for n in m.group(1).split(",") if n.strip()]
    spans = []
    for n in names:
        for x in re.finditer(re.escape(n), f.quote):
            spans.append(x.end())
    if not spans:
        return ""
    new = f.quote
    for end in sorted(set(spans), reverse=True):
        # вставка после обозначения модели: если следом идёт номер или буквы, обозначение продолжается
        while end < len(new) and re.match(r"[\w./-]", new[end]):
            end += 1
        new = new[:end] + EQUIV + new[end:]
    return new


BY_RULE = {
    "days.in_words": _days_in_words,
    "days.start_wording": _start_wording,
    "pos.unit_meaning": _unit_typo,
    "wording.english_docs": _english_docs,
    "wording.warranty_start": _warranty_start,
    "brand.no_equiv_high": _add_equivalent,
}


def suggest(f) -> str:
    if f.section != SECTION_MAIN or not f.quote.strip():
        return ""
    if f.source.startswith("rule:gost") and not f.source.startswith("rule:gost_profile"):
        fn = _gost
    else:
        fn = BY_RULE.get(f.rule_id)
    if fn is None:
        return ""
    try:
        new = fn(f)
    except Exception:
        return ""
    return new if new and new.strip() and new != f.quote else ""


def apply(findings: list) -> list:
    for f in findings:
        if not getattr(f, "replacement", ""):
            f.replacement = suggest(f)
    return findings
