import json
import os
import tempfile
import time
from collections import defaultdict

PATH = os.environ.get("TZCHECK_FEEDBACK", os.path.join(os.environ.get("TZCHECK_STORE", os.path.join(tempfile.gettempdir(), "tzcheck_runs")), "feedback.jsonl"))
DECISIONS = {"accept": "принято", "reject": "отклонено", "review": "ручная проверка"}


def _norm(q: str) -> str:
    return " ".join(q.lower().split())[:120]


def record(run_id: str, file: str, finding: dict, decision: str, comment: str = "") -> dict:
    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {list(DECISIONS)}")
    row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "run_id": run_id, "file": file, "finding_id": finding.get("id"),
           "category": finding.get("category"), "source": finding.get("source", "").split("+")[0], "rule_id": finding.get("rule_id") or "",
           "quote": finding.get("quote", "")[:300], "decision": decision, "comment": comment[:1000]}
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    with open(PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def load() -> list[dict]:
    if not os.path.exists(PATH):
        return []
    out = []
    with open(PATH, encoding="utf-8") as f:
        for line in f:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _rule_key(source: str, category: str, rule_id: str = "") -> str:
    """Голосуют решения по тому же правилу: по rule_id, а у старых записей без него по паре «источник / категория».
    Старые записи без rule_id не голосуют против замечаний, у которых rule_id есть."""
    return rule_id or f"{source.split('+')[0]} / {category}"


def stats() -> dict:
    by_rule = defaultdict(lambda: {"принято": 0, "отклонено": 0, "ручная проверка": 0})
    for r in load():
        by_rule[_rule_key(r["source"], r["category"], r.get("rule_id", ""))][DECISIONS[r["decision"]]] += 1
    res = {}
    for k, v in by_rule.items():
        n = sum(v.values())
        res[k] = {**v, "всего": n, "доля принятых": round(v["принято"] / n, 2) if n else None}
    return dict(sorted(res.items(), key=lambda x: -x[1]["всего"]))


def apply(findings: list) -> list:
    """Решения закупщиков возвращаются в проверку: отклонённое дважды по той же цитате не показывается,
    правило (rule_id) с долей отклонений от 60 % на 5+ решениях уходит в ручную проверку с пониженной уверенностью.
    Замечания, подтверждённые справочником (confirmed_by: «ГОСТ заменён» по каталогу, кода нет в ОКПД 2), голосованием
    не снимаются и не понижаются: решения закупщиков записываются в основание."""
    rows = load()
    if not rows:
        return findings
    rejected_quotes = defaultdict(int)
    per_rule = defaultdict(lambda: [0, 0])
    for r in rows:
        key = (_rule_key(r["source"], r["category"], r.get("rule_id", "")), _norm(r["quote"]))
        if r["decision"] == "reject":
            rejected_quotes[key] += 1
        elif r["decision"] == "accept":
            rejected_quotes[key] -= 1
        pr = per_rule[_rule_key(r["source"], r["category"], r.get("rule_id", ""))]
        pr[0] += 1
        pr[1] += r["decision"] == "reject"
    out = []
    for f in findings:
        rejected = rejected_quotes.get((_rule_key(f.source, f.category, getattr(f, "rule_id", "")), _norm(f.quote)), 0) >= 2
        n, rej = per_rule.get(_rule_key(f.source, f.category, getattr(f, "rule_id", "")), (0, 0))
        if getattr(f, "confirmed_by", ""):
            if rejected or (n >= 5 and rej / n >= 0.6):
                f.evidence["обратная связь"] = (f"закупщики отклоняли такие замечания ({rej} из {n}), но вывод подтверждён справочником "
                                                f"({f.confirmed_by}), поэтому замечание оставлено")
            out.append(f)
            continue
        if rejected:
            continue
        if n >= 5 and rej / n >= 0.6:
            f.needs_review = True
            f.confidence = round(max(0.2, f.confidence - 0.2), 2)
            if getattr(f, "confidence_pct", None) is not None:
                # в отчёте показывается оценка в процентах: понижение должно быть видно и там
                f.confidence_pct = max(0, int(f.confidence_pct) - 20)
                f.confidence_basis = (f.confidence_basis or "оценка") + "; −20 по отклонениям закупщиков"
            f.evidence["обратная связь"] = f"закупщики отклонили {rej} из {n} таких замечаний"
        elif n >= 5:
            f.evidence["обратная связь"] = f"закупщики приняли {n - rej} из {n} таких замечаний"
        out.append(f)
    return out
