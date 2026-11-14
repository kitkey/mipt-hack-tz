import gzip
import json
import os
import re
from functools import lru_cache

DATA = os.environ.get("TZCHECK_OKPD2") or os.path.join(os.environ.get("TZCHECK_DATA_DIR") or os.path.join(os.path.dirname(__file__), "data"), "okpd2.json.gz")
META = os.path.join(os.path.dirname(DATA), "okpd2_meta.json")
WORD_RE = re.compile(r"[а-яё]{4,}", re.I)
ADJ_END = ("ый", "ий", "ой", "ая", "яя", "ое", "ее", "ые", "ие", "ых", "их", "ого", "его", "ому", "ыми", "ими")
STOP = {"прочие", "прочий", "прочая", "прочее", "включенные", "включенный", "другие", "группировки", "группировку", "изделия",
        "продукция", "оборудование", "общего", "назначения", "работы", "услуги", "кроме", "также", "части", "принадлежности",
        "комплект", "набор", "поставка", "товар", "цифровой", "цифровая", "цифровые", "электронный", "электронная", "портативный",
        "лабораторный", "лабораторные", "настольный", "стационарный", "ручной", "ручного", "менее", "более"}


def _load():
    try:
        mtime = os.path.getmtime(DATA)
    except OSError:
        mtime = 0.0
    return _load_at(mtime)


@lru_cache(maxsize=1)
def _load_at(mtime: float):
    items = json.load(gzip.open(DATA, "rt", encoding="utf-8"))
    by_code = {x["c"]: x for x in items}
    leaves = [x for x in items if re.fullmatch(r"\d\d\.\d\d\.\d\d\.\d{3}", x["c"])]
    return by_code, leaves


def version() -> str:
    try:
        m = json.load(open(META, encoding="utf-8"))
        if m.get("version_label"):
            return m["version_label"]
        return f"{m.get('classifier', 'ОК 034-2014')}, редакция Росстата от {m.get('source_date', '?')}"
    except (OSError, ValueError):
        return "ОК 034-2014"


def name(code: str) -> str | None:
    x = _load()[0].get(code)
    return x["n"] if x else None


def names() -> list[str]:
    return [x["n"] for x in _load()[0].values()]


def _words(text: str) -> list[str]:
    return [w.lower() for w in WORD_RE.findall(text) if w.lower() not in STOP]


ENDINGS = {"", "а", "я", "о", "е", "ы", "и", "у", "ю", "ь", "й", "ом", "ем", "ой", "ей", "ов", "ев", "ам", "ям", "ах", "ях", "ами", "ями",
           "ий", "ый", "ая", "яя", "ое", "ее", "ые", "ие", "ых", "их", "ым", "им", "ого", "его", "ому", "ему", "ыми", "ими", "ую", "юю"}
GENERIC_HEADS = {"измеритель", "измерители", "прибор", "приборы", "устройство", "аппарат", "система", "инструмент", "работы", "замене",
                 "замена", "монтажу", "монтаж", "поставка", "выполнение", "оказание", "средство", "средства"}


def _same(a: str, b: str) -> bool:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n >= 4 and a[n:] in ENDINGS and b[n:] in ENDINGS


def _weights(item: str) -> dict[str, float]:
    iw = _words(item)
    nouns = [w for w in iw if not w.endswith(ADJ_END)]
    key = next((w for w in nouns if w not in GENERIC_HEADS), nouns[0] if nouns else None)
    out = {}
    for w in iw:
        if w == key:
            out[w] = 3.0
        elif w in GENERIC_HEADS:
            out[w] = 1.0
        elif w.endswith(ADJ_END):
            out[w] = 0.5
        else:
            out[w] = 2.0
    return out


BROAD = {"измерители", "приборы", "аппараты", "аппаратура", "приспособления", "материалы", "реагенты", "устройства", "оборудование", "машины", "инструменты", "инструмент", "изделия", "средства"}


def _stem(w: str) -> str:
    for k in (3, 2, 1):
        if len(w) - k >= 4 and w[-k:] in ENDINGS:
            return w[:-k]
    return w


def _hit(w: str, cw: list[str], key: bool) -> bool:
    if any(_same(w, c) for c in cw):
        return True
    st = _stem(w)
    return key and len(st) >= 5 and any(st in c for c in cw)


@lru_cache(maxsize=None)
def _code_words(code_name: str) -> list[str]:
    return _words(code_name)


def score(item: str, code_name: str) -> float:
    cw = _code_words(code_name)
    return sum(wt for w, wt in _weights(item).items() if _hit(w, cw, wt == 3.0))


def _covers_head(item: str, code_name: str) -> bool:
    cw = _code_words(code_name)
    wts = _weights(item)
    return all(_hit(w, cw, wts[w] == 3.0) for w in _words(item)[:3] if wts[w] >= 2.0)


def suggest(item: str, code: str | None = None, limit: int = 3) -> list[tuple[str, str, float, float]]:
    by_code, leaves = _load()
    cls = code[:8] if code else ""
    grp = code[:5] if code else ""
    res = []
    for x in leaves:
        base = score(item, x["n"])
        if base < 2 or not _covers_head(item, x["n"]):
            continue
        sc = base + (1.5 if cls and x["c"].startswith(cls) else 0.5 if grp and x["c"].startswith(grp) else 0.0)
        if re.search(r"прочи|не включенн", x["n"], re.I):
            sc -= 1.0
        if sc >= 3:
            res.append((x["c"], x["n"], sc, base))
    res.sort(key=lambda r: -r[2])
    return res[:limit]


def assess(item: str, code: str) -> dict:
    try:
        mtime = os.path.getmtime(DATA)
    except OSError:
        mtime = 0.0
    return _assess(item, code, mtime)


@lru_cache(maxsize=4096)
def _assess(item: str, code: str, mtime: float) -> dict:
    """Вердикт по паре «наименование позиции — код»: missing / generic / mismatch / ok, с подсказкой точного кода."""
    nm = name(code)
    if nm is None:
        return {"verdict": "missing", "suggest": suggest(item, code)}
    own = score(item, nm)
    best = suggest(item, code)
    generic = bool(re.search(r"прочи|не включенн", nm, re.I))
    better = [b for b in best if b[0] != code and b[2] > own + 1.5]
    if generic:
        near = [b for b in better if b[0][:5] == code[:5] or b[3] >= 5]
        if near:
            return {"verdict": "generic", "name": nm, "suggest": near}
        return {"verdict": "ok", "name": nm, "suggest": []}
    broad = (nm.replace(",", " ").split() or [""])[0].lower() in BROAD
    if broad:
        better = [b for b in better if b[0][:5] == code[:5] and b[0][:8] != code[:8]]
    if own < 2 and better and better[0][3] >= 3:
        return {"verdict": "mismatch", "name": nm, "suggest": better}
    return {"verdict": "ok", "name": nm, "suggest": []}
