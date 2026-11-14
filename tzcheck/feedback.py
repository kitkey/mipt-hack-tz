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
           "category": finding.get("category"), "source": finding.get("source", "").split("+")[0], "quote": finding.get("quote", "")[:300],
           "decision": decision, "comment": comment[:1000]}
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


def stats() -> dict:
    by_rule = defaultdict(lambda: {"принято": 0, "отклонено": 0, "ручная проверка": 0})
    for r in load():
        by_rule[f'{r["source"]} / {r["category"]}'][DECISIONS[r["decision"]]] += 1
    res = {}
    for k, v in by_rule.items():
        n = sum(v.values())
        res[k] = {**v, "всего": n, "доля принятых": round(v["принято"] / n, 2) if n else None}
    return dict(sorted(res.items(), key=lambda x: -x[1]["всего"]))


def apply(findings: list) -> list:
    """Решения закупщиков возвращаются в проверку: отклонённое дважды по той же цитате не показывается,
    правило с долей отклонений от 60 % на 5+ решениях уходит в ручную проверку с пониженной уверенностью."""
    rows = load()
    if not rows:
        return findings
    rejected_quotes = defaultdict(int)
    per_rule = defaultdict(lambda: [0, 0])
    for r in rows:
        key = (r["category"], _norm(r["quote"]))
        if r["decision"] == "reject":
            rejected_quotes[key] += 1
        elif r["decision"] == "accept":
            rejected_quotes[key] -= 1
        pr = per_rule[(r["source"], r["category"])]
        pr[0] += 1
        pr[1] += r["decision"] == "reject"
    out = []
    for f in findings:
        if rejected_quotes.get((f.category, _norm(f.quote)), 0) >= 2:
            continue
        n, rej = per_rule.get((f.source.split("+")[0], f.category), (0, 0))
        if n >= 5 and rej / n >= 0.6:
            f.needs_review = True
            f.confidence = round(max(0.2, f.confidence - 0.2), 2)
            f.evidence["обратная связь"] = f"закупщики отклонили {rej} из {n} таких замечаний"
        elif n >= 5:
            f.evidence["обратная связь"] = f"закупщики приняли {n - rej} из {n} таких замечаний"
        out.append(f)
    return out
