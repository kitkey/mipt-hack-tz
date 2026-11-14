"""Выбор групп проверок (Логика, Стандарты, Риски): какие правила работают, какие замечания остаются, вердикт неполной проверки."""
import inspect
from collections import Counter
from functools import lru_cache

from .norms import CATEGORIES, CATEGORY_GROUP, CHECK_GROUPS, GROUP_LOGIC, GROUP_STANDARDS, categories_for, group_of, parse_checks

__all__ = ["parse_checks", "categories_for", "is_full", "group_names", "rules_for", "only_groups", "group_stats",
           "partial_note", "verdict_code", "GROUP_LOGIC", "GROUP_STANDARDS", "OK_STATUSES", "succeeded", "steps_note",
           "unprofiled_positions", "unprofiled_note"]


def is_full(groups) -> bool:
    return len(parse_checks(groups)) == len(CHECK_GROUPS)


def group_names(groups) -> list[str]:
    return [CHECK_GROUPS[k] for k in parse_checks(groups)]


@lru_cache(maxsize=None)
def rule_groups(fn) -> frozenset | None:
    """Группы, категории которых названы в коде функции правила; None, если категорий в коде нет (правило работает всегда)."""
    try:
        src = inspect.getsource(fn)
    except (OSError, TypeError):
        return None
    cats = {c for c in CATEGORIES if f'"{c}"' in src}
    return frozenset(CATEGORY_GROUP[c] for c in cats) or None


def rules_for(groups):
    """Правила, которые нужны выбранным группам; None = все (полная проверка, run_rules без изменений)."""
    if is_full(groups):
        return None
    from . import rules
    names = set(group_names(groups))
    return [fn for fn in rules.ALL_RULES if rule_groups(fn) is None or rule_groups(fn) & names]


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
    done = ", ".join(f"«{g}»" for g in group_names(groups))
    skipped = ", ".join(f"«{g}»" for g in CHECK_GROUPS.values() if g not in group_names(groups))
    return (f"Проверка неполная: проверены только группы {done}, группы {skipped} не проверялись. "
            "Вывод о готовности ТЗ к публикации по такой проверке не делается, перед публикацией нужна полная проверка.")


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
    return ("Проверка неполная: " + "; ".join(parts) + ". Вывод о готовности ТЗ к публикации по такой проверке не делается, "
            "проверку нужно повторить.")


def unprofiled_positions(doc, limit: int = 8) -> tuple[list[str], int]:
    """Товарные позиции (строки таблиц с единицей измерения), для которых нет профиля характеристик в правилах:
    их свойства правилами не сверялись. Возвращает до limit названий и общее число."""
    from .req_compare import extract
    from .rules import _profile_for
    names = []
    for r in extract(doc, "new"):
        if r.kind != "позиция":
            continue
        name = r.label.strip().rstrip(" ,;:")
        if len(name.split()) < 2 and len(name) < 10:  # одиночное слово («полная», «новая») не название товара
            continue
        if _profile_for(name[:60]) is None and name not in names:
            names.append(name)
    return [n[:80] for n in names[:limit]], len(names)


def unprofiled_note(doc) -> str:
    names, total = unprofiled_positions(doc)
    if not total:
        return ""
    more = f" и ещё {total - len(names)}" if total > len(names) else ""
    return (f" Не проверялись по профилям характеристик позиции без профиля ({total}): " + "; ".join(names) + more
            + ". Их свойства (характеристики, комплектность, единицы) сверить вручную.")
