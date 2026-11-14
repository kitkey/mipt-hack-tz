"""Выбор групп проверок (Логика, Стандарты, Риски): какие замечания остаются, вердикт неполной проверки."""
from collections import Counter

from .norms import CHECK_GROUPS, GROUP_LOGIC, GROUP_STANDARDS, categories_for, group_of, parse_checks

__all__ = ["parse_checks", "categories_for", "is_full", "group_names", "only_groups", "group_stats",
           "partial_note", "verdict_code", "GROUP_LOGIC", "GROUP_STANDARDS", "OK_STATUSES", "succeeded", "steps_note"]


def is_full(groups) -> bool:
    return len(parse_checks(groups)) == len(CHECK_GROUPS)


def group_names(groups) -> list[str]:
    return [CHECK_GROUPS[k] for k in parse_checks(groups)]


def only_groups(findings: list, groups) -> list:
    """Замечания выбранных групп: правило со смешанными категориями работает, а лишние замечания отбрасываются."""
    if is_full(groups):
        return findings
    names = set(group_names(groups))
    return [f for f in findings if group_of(f.category) in names]


def group_stats(main: list, groups) -> dict:
    names = group_names(groups)
    out = {}
    for g in CHECK_GROUPS.values():
        fs = [f for f in main if group_of(f.category) == g]
        out[g] = {"checked": g in names, "count": len(fs), "by_severity": dict(Counter(f.severity for f in fs)),
                  "by_status": dict(Counter(f.status for f in fs)), "by_category": dict(Counter(f.category for f in fs))}
    return out


def partial_note(groups) -> str:
    if is_full(groups):
        return ""
    skipped = [f"«{g}»" for g in CHECK_GROUPS.values() if g not in group_names(groups)]
    what = "не проверялась группа " if len(skipped) == 1 else "не проверялись группы "
    return f"Проверка неполная, {what}{' и '.join(skipped)}. Вывод о готовности ТЗ к публикации не делается."


def verdict_code(main: list, groups, incomplete_steps=None) -> str:
    """Код вердикта для интерфейса: blocked, review_high, fix, review, clean; при неполной проверке clean становится partial,
    а если шаг конвейера упал или не выполнен, clean и partial становятся incomplete."""
    if any(f.severity == "high" and f.status == "ошибка" for f in main):
        code = "blocked"
    elif any(f.severity == "high" for f in main):
        code = "review_high"
    elif any(f.status == "ошибка" for f in main):
        code = "fix"
    elif main:
        code = "review"
    else:
        code = "clean"
    if code == "clean" and incomplete_steps:
        return "incomplete"
    return "partial" if code == "clean" and not is_full(groups) else code


OK_STATUSES = ("ok", "incomplete")


def succeeded(res: dict) -> bool:
    """Проверка дошла до результата: ok (все шаги выполнены) или incomplete (часть шагов упала или не выполнена)."""
    return (res or {}).get("status") in OK_STATUSES


def steps_note(bad: list[dict]) -> str:
    """Начало заключения, если шаг конвейера упал или не выполнен: «Проверка неполная: <шаги>»."""
    if not bad:
        return ""
    parts = [f"«{x['title']}» ({x['state']}" + (f": {x['reason'][:140]}" if x.get("reason") else "") + ")" for x in bad]
    return "Проверка неполная: " + "; ".join(parts) + ". Вывод о готовности ТЗ к публикации не делается, проверку нужно повторить."
