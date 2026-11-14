"""«Сопоставить требования»: текущее ТЗ и другой документ (например, прошлогоднее ТЗ на тот же предмет).

Это не сравнение версий одного документа (/compare): требования двух разных документов (позиции и характеристики из
строк таблиц, пункты разделов) сводятся попарно по сходству текста детерминированно, без модели. На выходе изменённые
требования (было / стало), новые и убранные. По текущему документу идёт полная проверка; в сводке замечания,
которые повторяются в обоих документах, и замечания на изменённых и новых требованиях."""
import re
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher

HEADER_WORDS = re.compile(r"^\s*(?:№|n|п/п|номер\w*|окпд\s*2?|код\w*|наименовани\w*|ед\.?\s*изм\w*|единиц\w*\s+измерени\w*|кол-?во|количеств\w*|характеристик\w*|"
                          r"значени\w*|требовани\w*|показател\w*|параметр\w*|примечани\w*|цена|стоимость|сумма)\b", re.I)
UNIT_RE = re.compile(r"^\s*(?:шт|штук\w*|компл\w*|ед\.?|упак\w*|кг|м|м2|м3|л|пар\w*|усл\.?\s*ед\.?)\.?\s*$", re.I)
NUM_ONLY = re.compile(r"^\s*[\d.,]+\s*\.?\s*$")
WORD = re.compile(r"[а-яёa-z]+|\d+(?:[.,]\d+)*", re.I)
THRESHOLD = 0.55


@dataclass
class Req:
    side: str
    block_id: str
    kind: str
    location: str
    section: str
    label: str
    value: str
    text: str
    norm: str = ""
    stems: frozenset = field(default_factory=frozenset)

    def public(self) -> dict:
        d = asdict(self)
        d.pop("norm")
        d.pop("stems")
        return d


def _norm(s: str) -> str:
    return " ".join(s.lower().replace("ё", "е").split())


def _stems(s: str) -> frozenset:
    from .rag import lemma
    return frozenset(lemma(w) if not w[0].isdigit() else w for w in (x.lower().replace("ё", "е") for x in WORD.findall(s)) if len(w) > 2 or w[0].isdigit())


def _label_sim(a: str, b: str) -> float:
    """Сходство подписей позиции: посимвольно или вложение слов одной подписи в другую («Тесламетр» и «Тесламетр (измеритель …)»)."""
    ratio = SequenceMatcher(None, _norm(a), _norm(b)).ratio()
    sa, sb = _stems(a), _stems(b)
    if not sa or not sb:
        return ratio
    contain = len(sa & sb) / min(len(sa), len(sb))
    return max(ratio, 0.9 * contain if contain == 1 else 0.7 * contain)


def _header(cells: list[str]) -> bool:
    filled = [c for c in cells if c.strip()]
    return len(filled) >= 2 and sum(1 for c in filled if HEADER_WORDS.match(c)) >= max(2, len(filled) // 2) \
        and not any(re.search(r"\d", c) for c in filled if not HEADER_WORDS.match(c))


def extract(doc, side: str) -> list[Req]:
    """Требования документа: строка таблицы = позиция (есть единица или количество) или характеристика; абзац = пункт раздела."""
    out = []
    for b in doc.blocks:
        text = " ".join(b.text.split())
        if b.kind == "heading" or len(text) < 4 or not re.search(r"[а-яёa-z]", text, re.I):
            continue
        if b.kind == "row":
            cells = [" ".join(c.split()) for c in b.cells]
            if _header(cells):
                continue
            label_cells = [c for c in cells if c and not NUM_ONLY.match(c) and not UNIT_RE.match(c) and re.search(r"[а-яёa-z]{3}", c, re.I)]
            value_cells = [c for c in cells if c and c not in label_cells]
            label = label_cells[0] if label_cells else text[:120]
            value = " | ".join(label_cells[1:] + value_cells)
            kind = "позиция" if any(UNIT_RE.match(c) for c in cells) else "характеристика"
            # номер позиции в первой ячейке не часть требования: перенумерация строк изменением не считается
            content = [c for c in cells if c]
            if len(content) > 1 and NUM_ONLY.match(content[0]):
                content = content[1:]
            key = " | ".join(content)
        else:
            label, value, kind, key = b.section or "", text, "пункт раздела", text
        out.append(Req(side, b.id, kind, b.location(), b.section or "", label, value, text, _norm(key), _stems(key)))
    return out


def _score(a: Req, b: Req) -> float:
    if (a.kind == "пункт раздела") != (b.kind == "пункт раздела"):
        return 0.0
    inter = len(a.stems & b.stems)
    if not inter:
        return 0.0
    jac = inter / len(a.stems | b.stems)
    if jac < 0.2:
        return 0.0
    sm = SequenceMatcher(None, a.norm[:400], b.norm[:400], autojunk=False)
    if sm.real_quick_ratio() < 0.3 or sm.quick_ratio() < 0.4:
        return 0.0
    ratio = sm.ratio()
    if a.kind == "пункт раздела":
        sec = SequenceMatcher(None, _norm(a.section), _norm(b.section)).ratio() if a.section or b.section else 1.0
        return 0.45 * ratio + 0.4 * jac + 0.15 * sec
    label = _label_sim(a.label, b.label)
    return 0.45 * label + 0.3 * ratio + 0.25 * jac


def _diff(old: str, new: str) -> list[dict]:
    a, b = re.findall(r"\s+|[^\s]+", old), re.findall(r"\s+|[^\s]+", new)
    out = []
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, a, b, autojunk=len(a) + len(b) > 1500).get_opcodes():
        if tag != "equal":
            out.append({"op": {"replace": "заменено", "delete": "удалено", "insert": "добавлено"}[tag],
                        "было": "".join(a[i1:i2]).strip(), "стало": "".join(b[j1:j2]).strip()})
    return [x for x in out if x["было"] or x["стало"]]


def pair(old: list[Req], new: list[Req]) -> dict:
    """Сведение требований: сначала совпавшие дословно, затем жадно по убыванию сходства с порогом THRESHOLD."""
    by_norm = defaultdict(list)
    for r in old:
        by_norm[r.norm].append(r)
    used_old, used_new, unchanged, changed = set(), set(), [], []
    for r in new:
        same = by_norm.get(r.norm)
        if same:
            o = same.pop(0)
            used_old.add(id(o))
            used_new.add(id(r))
            unchanged.append((o, r))
    rest_old = [r for r in old if id(r) not in used_old]
    rest_new = [r for r in new if id(r) not in used_new]
    index = defaultdict(list)
    for i, r in enumerate(rest_old):
        for s in r.stems:
            index[s].append(i)
    cands = []
    for j, r in enumerate(rest_new):
        seen = set()
        for s in r.stems:
            for i in index.get(s, ()):
                if i in seen:
                    continue
                seen.add(i)
                sc = _score(rest_old[i], r)
                if sc >= THRESHOLD:
                    cands.append((sc, i, j))
    cands.sort(key=lambda x: (-x[0], x[1], x[2]))
    taken_o, taken_n = set(), set()
    for sc, i, j in cands:
        if i in taken_o or j in taken_n:
            continue
        taken_o.add(i)
        taken_n.add(j)
        o, n = rest_old[i], rest_new[j]
        changed.append({"similarity": round(sc, 3), "kind": n.kind, "old": o.public(), "new": n.public(), "diff": _diff(o.text, n.text)})
    changed.sort(key=lambda x: _order(new, x["new"]["block_id"]))
    added = [r.public() for j, r in enumerate(rest_new) if j not in taken_n]
    removed = [r.public() for i, r in enumerate(rest_old) if i not in taken_o]
    pairs = {n.block_id: o.block_id for o, n in unchanged}
    pairs.update({c["new"]["block_id"]: c["old"]["block_id"] for c in changed})
    return {"counts": {"old": len(old), "new": len(new), "unchanged": len(unchanged), "changed": len(changed),
                       "added": len(added), "removed": len(removed)},
            "changed": changed, "added": added, "removed": removed, "_pairs": pairs}


def _order(reqs: list[Req], bid: str) -> int:
    return next((i for i, r in enumerate(reqs) if r.block_id == bid), 10 ** 6)


def _qkey(f: dict) -> tuple:
    return f["category"], " ".join(f["quote"].lower().split())[:80]


def _short(f: dict) -> dict:
    keys = ("id", "group", "category", "severity", "status", "section", "quote", "location", "block_id", "explanation", "fix", "replacement")
    return {k: f.get(k) for k in keys}


def summarize(old_res: dict, new_res: dict, req: dict) -> dict:
    """Замечания текущего документа против прошлого: повторяются, на изменённых требованиях, на новых."""
    pairs = req["_pairs"]
    changed_new = {c["new"]["block_id"] for c in req["changed"]}
    added_new = {a["block_id"] for a in req["added"]}
    old_by_block = defaultdict(list)
    old_by_key = {}
    for g in old_res.get("findings", []):
        for bid in [g["block_id"]] + list(g.get("also_in") or []):
            old_by_block[bid].append(g)
        if g["quote"].strip():
            old_by_key.setdefault(_qkey(g), g)
    repeated, on_changed, on_added, other = [], [], [], 0
    matched_old = set()
    for f in new_res.get("findings", []):
        blocks = [f["block_id"]] + list(f.get("also_in") or [])
        twin = old_by_key.get(_qkey(f)) if f["quote"].strip() else None
        if twin is None:
            twin = next((g for b in blocks if b in pairs for g in old_by_block.get(pairs[b], []) if g["category"] == f["category"]), None)
        if twin is not None:
            matched_old.add(twin["id"])
            repeated.append(dict(_short(f), old_finding_id=twin["id"], old_location=twin["location"]))
        elif any(b in changed_new for b in blocks):
            on_changed.append(_short(f))
        elif any(b in added_new for b in blocks):
            on_added.append(_short(f))
        else:
            other += 1
    fixed = [_short(g) for g in old_res.get("findings", []) if g["id"] not in matched_old]
    c = req["counts"]
    text = (f"Требований в прошлом документе {c['old']}, в текущем {c['new']}: без изменений {c['unchanged']}, изменено {c['changed']}, "
            f"новых {c['added']}, убрано {c['removed']}. Замечаний по текущему документу {len(new_res.get('findings', []))}: "
            f"ошибка повторяется с прошлого документа {len(repeated)}, на изменённых требованиях {len(on_changed)}, на новых {len(on_added)}, "
            f"на прежних требованиях без пары в прошлом документе {other}. В прошлом документе было и в текущем не найдено: {len(fixed)}.")
    return {"repeated": repeated, "on_changed": on_changed, "on_added": on_added, "other_count": other,
            "not_repeated_from_old": fixed, "summary": text}


def compare_requirements(old_doc, new_doc) -> dict:
    req = pair(extract(old_doc, "old"), extract(new_doc, "new"))
    return req


def public(req: dict) -> dict:
    return {k: v for k, v in req.items() if not k.startswith("_")}
