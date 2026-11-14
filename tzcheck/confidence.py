"""Уверенность замечания: число 0-100 даёт сама модель полем "confidence" в JSON-ответе.

Правила без модели цифру не получают: у них источник «правило». Если модель поле не вернула
или вернула мусор, ставится честная пометка «нет оценки», а не выдуманное число.
"""
import os
import re

NO_SCORE = "нет оценки"
RULE = "правило"
STAGE_FIND = "модель при поиске"
STAGE_VERIFY = "модель при второй проверке"
STAGE_RECHECK = "модель при повторной проверке"


def parse_pct(value) -> int | None:
    """Целое 0-100 из ответа модели; 0.85 считается долей и переводится в 85; вне диапазона и не число дают None."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        m = re.fullmatch(r"\s*(\d+(?:[.,]\d+)?)\s*%?\s*", value)
        if not m:
            return None
        value = float(m.group(1).replace(",", "."))
    if not isinstance(value, (int, float)) or value != value:
        return None
    if 0 < value < 1:
        value = value * 100
    if not 0 <= value <= 100:
        return None
    return int(round(value))


def set_model_score(f, value, stage: str):
    """Записать оценку модели в замечание; прежнюю оценку того же замечания сохранить в основании."""
    pct = parse_pct(value)
    prev = getattr(f, "confidence_pct", None)
    if prev is not None and getattr(f, "confidence_basis", ""):
        f.evidence[f"уверенность ({f.confidence_basis})"] = f"{prev} %"
    if pct is None:
        if prev is None:
            f.confidence_basis = f"{stage}: {NO_SCORE}"
        return f
    f.confidence_pct = pct
    f.confidence_basis = stage
    return f


def apply(findings: list) -> list:
    """Подписать каждое замечание: оценка модели, «правило» или «нет оценки»."""
    for f in findings:
        if f.confidence_pct is not None:
            continue
        if not f.confidence_basis:
            f.confidence_basis = RULE if f.source.startswith(("rule", "reconcile")) and "llm" not in f.source else NO_SCORE
    return findings


def label(f: dict) -> str:
    """Текст для отчёта: «85 % (модель при второй проверке)», «правило», «нет оценки»."""
    pct = f.get("confidence_pct")
    basis = f.get("confidence_basis") or NO_SCORE
    return f"{pct} % ({basis})" if pct is not None else basis


def low_threshold() -> int:
    return parse_pct(os.environ.get("TZCHECK_CONF_LOW", "60")) or 60


def show_threshold() -> int:
    return parse_pct(os.environ.get("TZCHECK_CONF_MIN", "0")) or 0


def is_low(f, threshold: int | None = None) -> bool:
    """Нужна повторная проверка: оценка модели ниже порога или у замечания модели оценки нет."""
    t = low_threshold() if threshold is None else threshold
    if f.confidence_pct is not None:
        return f.confidence_pct < t
    return "llm" in f.source and not f.evidence.get("повторная проверка")
