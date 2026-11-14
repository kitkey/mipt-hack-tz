"""Выбор групп проверок (Логика, Стандарты, Риски): какие правила работают, какие замечания остаются, вердикт неполной проверки."""
import inspect
from collections import Counter
from functools import lru_cache

from .norms import CATEGORIES, CATEGORY_GROUP, CHECK_GROUPS, GROUP_LOGIC, GROUP_STANDARDS, categories_for, group_of, parse_checks

__all__ = ["parse_checks", "categories_for", "is_full", "group_names", "rules_for", "only_groups", "group_stats",
           "partial_note", "verdict_code", "GROUP_LOGIC", "GROUP_STANDARDS"]


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


def verdict_code(main: list, groups) -> str:
    """Код вердикта для интерфейса: blocked, review_high, fix, review, clean; при неполной проверке clean становится partial."""
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
    return "partial" if code == "clean" and not is_full(groups) else code
