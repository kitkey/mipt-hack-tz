import json
import os
import tempfile
import time
from collections import defaultdict

PATH = os.environ.get("TZCHECK_FEEDBACK", os.path.join(os.environ.get("TZCHECK_STORE", os.path.join(tempfile.gettempdir(), "tzcheck_runs")), "feedback.jsonl"))
DECISIONS = {"accept": "принято", "reject": "отклонено", "review": "ручная проверка"}


def _norm(q: str) -> str:
    return " ".join(q.lower().split())[:120]


def record(run_id: str, file: str, finding: dict, decision: str, comment: str = "", customer: str = "") -> dict:
    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {list(DECISIONS)}")
    row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "run_id": run_id, "file": file, "finding_id": finding.get("id"),
           "category": finding.get("category"), "source": finding.get("source", "").split("+")[0], "rule_id": finding.get("rule_id") or "",
           "quote": finding.get("quote", "")[:300], "decision": decision, "comment": comment[:1000], "customer": customer or ""}
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


def latest(rows: list[dict] | None = None) -> list[dict]:
    """Последнее решение по каждому замечанию прогона (run_id, finding_id): повторный клик заменяет прежнее решение, а не добавляет голос."""
    last = {}
    for r in load() if rows is None else rows:
        last[(r.get("run_id"), r.get("finding_id") or _norm(r.get("quote", "")))] = r
    return list(last.values())


def _rule_key(source: str, category: str, rule_id: str = "") -> str:
    """Голосуют решения по тому же правилу: по rule_id, а у старых записей без него по паре «источник / категория».
    Старые записи без rule_id не голосуют против замечаний, у которых rule_id есть."""
    return rule_id or f"{source.split('+')[0]} / {category}"


def stats() -> dict:
    by_rule = defaultdict(lambda: {"принято": 0, "отклонено": 0, "ручная проверка": 0})
    for r in latest():
        by_rule[_rule_key(r["source"], r["category"], r.get("rule_id", ""))][DECISIONS[r["decision"]]] += 1
    res = {}
    for k, v in by_rule.items():
        n = v["принято"] + v["отклонено"]  # «ручная проверка» не решение: в долю не входит
        res[k] = {**v, "всего": n, "доля принятых": round(v["принято"] / n, 2) if n else None}
    return dict(sorted(res.items(), key=lambda x: -x[1]["всего"]))


def apply(findings: list, customer: str = "") -> list:
    """Решения закупщиков возвращаются в проверку того же заказчика (customer: id профиля из customers.json, "" для нераспознанного).
    По каждому замечанию прогона берётся последнее решение; голоса считаются по разным прогонам (run_id), а не по кликам.
    Цитата, отклонённая в двух прогонах (за вычетом прогонов, где её приняли), не показывается; правило (rule_id), отклонённое
    в 60 % прогонов из 5+, уходит в ручную проверку с пониженной уверенностью. «Ручная проверка» решением не считается.
    Замечания, подтверждённые справочником (confirmed_by: «ГОСТ заменён» по каталогу, кода нет в ОКПД 2), голосованием
    не снимаются и не понижаются: решения закупщиков записываются в основание."""
    rows = [r for r in latest() if r["decision"] in ("accept", "reject") and (r.get("customer") or "") == (customer or "")]
    if not rows:
        return findings
    quote_runs = defaultdict(lambda: {"reject": set(), "accept": set()})
    rule_runs = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    for r in rows:
        rk = _rule_key(r["source"], r["category"], r.get("rule_id", ""))
        quote_runs[(rk, _norm(r["quote"]))][r["decision"]].add(r["run_id"])
        v = rule_runs[rk][r["run_id"]]
        v[r["decision"] == "reject"] += 1
    per_rule = {}
    for rk, runs in rule_runs.items():
        # прогон голосует один раз: «против», если отклонений в нём больше, чем принятий
        per_rule[rk] = (len(runs), sum(1 for acc, rej in runs.values() if rej > acc))
    out = []
    for f in findings:
        rk = _rule_key(f.source, f.category, getattr(f, "rule_id", ""))
        q = quote_runs.get((rk, _norm(f.quote)))
        rejected = bool(q) and len(q["reject"]) - len(q["accept"]) >= 2
        n, rej = per_rule.get(rk, (0, 0))
        if getattr(f, "confirmed_by", ""):
            if rejected or (n >= 5 and rej / n >= 0.6):
                f.evidence["обратная связь"] = (f"закупщики отклоняли такие замечания (в {rej} из {n} проверок), но вывод подтверждён справочником "
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
            f.evidence["обратная связь"] = f"закупщики отклонили такие замечания в {rej} из {n} проверок"
            # причина ручной проверки, которую подтверждение второй проверки не снимает (pipeline.route)
            f._hold = getattr(f, "_hold", []) + [f.evidence["обратная связь"]]
        elif n >= 5:
            f.evidence["обратная связь"] = f"закупщики приняли такие замечания в {n - rej} из {n} проверок"
        out.append(f)
    return out
