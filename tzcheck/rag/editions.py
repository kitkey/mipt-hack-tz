"""Сверка требований ТЗ с новой редакцией заменённого стандарта.

Тексты: прежняя редакция из корпуса data/norm_corpus/ (или скачанная), новая из <data>/norm_texts/ (tzcheck/updater/gost_texts.py).
Обе разбираются на пункты и таблицы так же, как корпус (tzcheck.rag.chunks), приложения выделяются в отдельные пункты.
Пункты сопоставляются по тексту с поправкой на совпадающий номер; изменения выделяет код: числа с единицами,
обязательность («должен» / «может»), ссылки на стандарты, главный изменённый фрагмент.
Требования ТЗ по теме стандарта (слова из его заглавия) ищутся BM25 по пунктам обеих редакций; найденный пункт берётся
вместе с парой из другой редакции. Числа требования сверяются с прежними и новыми значениями пункта; если по числам решить
нельзя, вывод «сверить вручную», а при включённой модели решает модель (model_review, ТЗ маскируется до отправки).
"""
import difflib
import math
import os
import re
from collections import Counter
from functools import lru_cache
from pathlib import Path

from .. import refdata
from . import CORPUS, DESIGNATION, _doc_files, _read_doc, _windows, best_quote, chunks, norm_document, texts_dir, tokens

PREFACE_RE = re.compile(r"^(?:\d+\s+)?(?:РАЗРАБОТАН|ВНЕСЕН|ПРИНЯТ|ВВЕДЕН|ВЗАМЕН|ПОДГОТОВЛЕН|УТВЕРЖДЕН|Приказом|Сведения о стандарте|"
                        r"Настоящий стандарт (?:разработан|подготовлен|идентичен|модифицирован|может быть применен для соблюдения))", re.I)
SCOPE_RE = re.compile(r"област[ьи]\s+применения", re.I)
NOISE_RE = re.compile(r"ИС МЕГАНОРМ|Краткое наименование страны|Код страны по МК", re.I)
ANNEX_RE = re.compile(r"^Приложение\s+([А-ЯЁA-Z])(?![а-яёa-z])")
ANNEX_ITEM_RE = re.compile(r"^([А-ЯЁA-Z]\.\d+(?:\.\d+)*)\.?\s+\S")
META_RE = re.compile(r"нормативные ссылки|термины и определения|библиограф|ключевые слова|структура настоящего стандарта|"
                     r"сопоставлени\w* структуры", re.I)
MUST_RE = re.compile(r"(?<![а-яё])(?:долж(?:ен|на|но|ны)|обязан\w*|не допускается|запрещается|необходимо)(?![а-яё])", re.I)
MAY_RE = re.compile(r"(?<![а-яё])(?:может|могут|допускается|рекомендуется|по возможности|при необходимости)(?![а-яё])", re.I)
UNIT = (r"мм|см|дм|км|м/с(?:2|²)?|м²|м2|м|с|сек|мин|ч|сут|%|дБ\s*\(?[AА]\)?|дБ[AА]?|кВ|мВ|В|мА|А|кГц|Гц|кВт|Вт|кг|т|кН|Н|МПа|кПа|"
        r"Па|лк|°\s*[CС]|К|лет|года|год|мес(?:яц\w*)?\.?|раз")
NUMBER = r"\d{1,3}(?:\s\d{3})+(?![\d,.])|\d+(?:[.,]\d+)?"
NUM_RE = re.compile(r"(?<![\w.,])(" + NUMBER + r")(?:\s*(?:[-–…]|до)\s*(" + NUMBER + r"))?\s*(" + UNIT + r")(?![А-Яа-яЁёA-Za-z])")
RANGE_RE = re.compile(r"(?<![а-яё])от\s+(" + NUMBER + r")\s+до\s+(" + NUMBER + r")\s*(" + UNIT + r")(?![А-Яа-яЁёA-Za-z])", re.I)
CMP_RE = re.compile(r"(?<![а-яё])(?:не\s+(?:менее|более|ниже|выше|свыше)|до|от|±|\+-)\s*(" + NUMBER + r")(?!\s*(?:" + UNIT +
                    r")(?![А-Яа-яЁёA-Za-z]))(?![\w.,])", re.I)
REF_NUM = re.compile(r"(?<![\d,])\d+(?:\.\d+){1,5}(?![\d,])|приложени\w+\s+[А-ЯA-Z](?![а-я])|таблиц\w+\s+[А-ЯA-Z]?\.?\d+", re.I)
TOPIC_STOP = {"лифты", "лифтов", "общие", "общий", "технические", "требования", "требований", "устройства", "устройство",
              "дополнительное", "оборудование", "правила", "методы", "условия", "часть", "безопасности", "безопасность",
              "межгосударственный", "стандарт", "национальный", "основные", "положения", "термины", "определения",
              "эксплуатации", "транспортирования", "людей", "грузов", "приспособления", "дополнительные", "специальные",
              "проектирования", "установке", "устройству", "лифтами"}
STOP_STEMS = {"лифт", "лифтов", "устрой", "должны", "должен", "обеспе", "настоя", "станда", "которы", "такой", "этот", "либо",
              "систем", "требов", "соотве", "наличи", "случай", "время", "если", "только", "иметь", "может", "данный", "работа",
              "выполн", "раздел", "пункт", "предус", "примен", "являть", "весь", "один", "другой", "свой", "каждый", "разный",
              "указан", "следую", "менее", "более"}
MATCH_MIN = 2
DENSE_MIN = float(os.environ.get("TZCHECK_EDITIONS_DENSE_MIN", "0.55"))
SAME = 0.995
PAIR_MIN = 0.45
PAIR_TRUST = 0.6
MAX_ITEMS = 6


def _norm_text(s: str) -> str:
    s = s.lower().replace("ё", "е").replace("№", "n ")
    s = re.sub(r"[«»“”„\"']", '"', s)
    s = re.sub(r"[‑–—−]", "-", s)
    s = re.sub(r"^\s*(?:\d+(?:\.\d+)*|[а-яa-z]\.\d+(?:\.\d+)*)\.?\s+", "", s)
    return re.sub(r"\s+", " ", s).strip()


def _words(s: str) -> list[str]:
    return _norm_text(s).split()


def stems(s: str) -> list[str]:
    return [w if w.isdigit() else w[:6] for w in tokens(s)]


def _dir_signature() -> tuple:
    sig = []
    for folder in (Path(CORPUS), texts_dir()):
        try:
            for n in sorted(os.listdir(folder)):
                if n.endswith((".json", ".json.gz")) and n not in ("index.json", "manifest.json"):
                    st = os.stat(folder / n)
                    sig.append((str(folder), n, st.st_size, int(st.st_mtime)))
        except OSError:
            pass
    return tuple(sig)


@lru_cache(maxsize=8)
def _catalog(signature: tuple) -> dict:
    out = {}
    for folder in (Path(CORPUS), texts_dir()):
        for p in _doc_files(folder):
            try:
                d = _read_doc(p)
            except (OSError, ValueError):
                continue
            if isinstance(d, dict) and d.get("document") and "blocks" in d:
                out.setdefault(norm_document(d["document"]), str(p))
    return out


@lru_cache(maxsize=32)
def _load(path: str, mtime: float) -> dict:
    return _read_doc(Path(path))


def _path(designation: str) -> str | None:
    return _catalog(_dir_signature()).get(norm_document(refdata.norm_designation(designation)))


def find_document(designation: str) -> dict | None:
    path = _path(designation)
    return _load(path, os.path.getmtime(path)) if path else None


def text_record(designation: str) -> dict | None:
    return refdata._mirror("norm_texts.json").get(refdata.norm_designation(designation))


def _split_annexes(c: dict) -> list[dict]:
    """Приложения в разборе корпуса приклеены к последнему пункту: выделить их и их пункты (Д.1, Д.2.1) отдельно."""
    if c["kind"] != "clause":
        return [c]
    out, cur, annex = [], dict(c, text=""), None
    for line in c["text"].split("\n"):
        s = line.strip()
        m = ANNEX_RE.match(s)
        mi = ANNEX_ITEM_RE.match(s) if annex else None
        tail = re.match(r"УДК\s|Ключевые слова", s)
        if m or mi or tail:
            if cur["text"].strip():
                out.append(cur)
            if tail:
                cur = dict(c, title="УДК, ключевые слова", text=line)
            elif m:
                annex = m.group(1)
                cur = dict(c, number=f"прил. {annex}", title=s[:80], text=line)
            else:
                cur = dict(c, number=mi.group(1), title="", text=line)
            continue
        cur["text"] = cur["text"] + "\n" + line if cur["text"] else line
    if cur["text"].strip():
        out.append(cur)
    return out


def clauses(doc: dict) -> list[dict]:
    """Пункты и таблицы без предисловия: окна и куски одного пункта склеиваются, приложения выделяются отдельно."""
    rows = chunks(doc)
    start = 0
    for i, r in enumerate(rows):
        if r["number"] in ("1", "1.1") and (SCOPE_RE.search(r["text"][:200]) or SCOPE_RE.search(r.get("title", ""))
                                            or (r["number"] == "1.1" and re.search(r"настоящий стандарт", r["text"][:120], re.I))):
            start = i
            break
    merged = []
    for r in rows[start:]:
        if PREFACE_RE.match(r["text"]) or NOISE_RE.search(r["text"][:200]):
            continue
        if merged and merged[-1]["number"] == r["number"] and merged[-1]["kind"] == r["kind"] == "clause":
            merged[-1]["text"] += "\n" + r["text"]
            continue
        merged.append({"document": doc["document"], "number": r["number"], "title": r.get("title", ""), "kind": r["kind"],
                       "text": r["text"]})
    out = []
    for c in merged:
        for part in _split_annexes(c):
            part["meta"] = bool(META_RE.search(part.get("title", "") + " " + part["text"][:120])) or \
                bool(re.match(r"УДК|МКС|Ключевые слова", part["text"].strip()))
            part["id"] = f"{doc['document']}:{len(out)}"
            out.append(part)
    titles = {}
    for c in out:
        if c["kind"] == "clause" and c.get("title"):
            titles.setdefault(c["number"], c["title"])
    for c in out:
        num = c["number"]
        if num.startswith("прил."):
            parents = []
        elif re.match(r"[А-ЯЁA-Z]\.", num):
            parents = ["прил. " + num[0]] + [num[:k] for k in range(3, len(num)) if num[k] == "."]
        else:
            bits = num.split(".")
            parents = [".".join(bits[:k]) for k in range(1, len(bits))]
        c["path"] = " ".join(titles[p] for p in parents if p in titles)
    return out


def _ratio(a: str, b: str) -> float:
    wa, wb = _words(a), _words(b)
    if wa == wb:
        return 1.0
    return difflib.SequenceMatcher(None, wa, wb, autojunk=False).ratio()


def align(old: list[dict], new: list[dict]) -> dict:
    """Пары пунктов (старый -> новый) по тексту с поправкой на совпадающий номер; без пары: исключённые и новые пункты."""
    tok_new = [set(tokens(c["text"])) for c in new]
    cand = []
    for i, o in enumerate(old):
        to = set(tokens(o["text"]))
        for j, n in enumerate(new):
            if o["kind"] != n["kind"]:
                continue
            jac = len(to & tok_new[j]) / max(1, len(to | tok_new[j]))
            if jac < 0.2 and o["number"] != n["number"]:
                continue
            r = _ratio(o["text"], n["text"])
            if r >= PAIR_MIN or (o["number"] == n["number"] and r >= 0.3):
                cand.append((r + (0.15 if o["number"] == n["number"] else 0.0), r, i, j))
    cand.sort(reverse=True)
    used_o, used_n, pairs = set(), set(), {}
    for _, r, i, j in cand:
        if i in used_o or j in used_n:
            continue
        used_o.add(i)
        used_n.add(j)
        pairs[i] = (j, r)
    return {"pairs": pairs, "removed": [i for i in range(len(old)) if i not in used_o],
            "added": [j for j in range(len(new)) if j not in used_n]}


def _num(s: str) -> float:
    return float(re.sub(r"\s", "", s).replace(",", "."))


def _unit(u: str) -> str:
    u = re.sub(r"[\s()]", "", u).replace("С", "C").replace("А", "A").rstrip(".")
    u = {"сек": "с", "года": "год", "лет": "год", "м2": "м²", "дБ": "дБA"}.get(u, u)
    return "мес" if u.startswith("мес") else u


def values(text: str) -> set[tuple]:
    """(число, единица) из текста; номера пунктов, приложений, таблиц и обозначения стандартов не считаются."""
    t = REF_NUM.sub(" ", DESIGNATION.sub(" ", text))
    out = set()
    for m in RANGE_RE.finditer(t):
        out |= {(_num(m.group(1)), _unit(m.group(3))), (_num(m.group(2)), _unit(m.group(3)))}
    t2 = RANGE_RE.sub(" ", t)
    for m in NUM_RE.finditer(t2):
        out.add((_num(m.group(1)), _unit(m.group(3))))
        if m.group(2):
            out.add((_num(m.group(2)), _unit(m.group(3))))
    for m in CMP_RE.finditer(t2):
        out.add((_num(m.group(1)), ""))
    return out


def _fmt_val(v: tuple) -> str:
    x, u = v
    return (f"{x:g}".replace(".", ",") + f" {u}").strip()


def _refs(text: str) -> set[str]:
    return {re.sub(r"\s+", " ", m.group()) for m in DESIGNATION.finditer(text)}


def _span(words: list[str], a: int, b: int, ctx: int = 3, limit: int = 20) -> str:
    lo, hi = max(0, a - ctx), min(len(words), b + ctx)
    seg = words[lo:hi]
    post = "…" if hi < len(words) else ""
    if len(seg) > limit:
        seg, post = seg[:limit], "…"
    return ("…" if lo > 0 else "") + " ".join(seg) + post


def changes(old_text: str, new_text: str) -> dict:
    """Что изменилось в пункте: значения, обязательность, ссылки и главный изменённый фрагмент."""
    ov, nv = values(old_text), values(new_text)
    wo, wn = old_text.split(), new_text.split()
    key = lambda w: w.lower().replace("ё", "е").strip(".,;:«»\"()")  # noqa: E731
    ops = [op for op in difflib.SequenceMatcher(None, [key(w) for w in wo], [key(w) for w in wn], autojunk=False).get_opcodes()
           if op[0] != "equal"]
    ops.sort(key=lambda op: -max(op[2] - op[1], op[4] - op[3]))
    main = None
    if ops:
        _, a1, a2, b1, b2 = ops[0]
        main = {"was": _span(wo, a1, a2) if a2 > a1 else "", "now": _span(wn, b1, b2) if b2 > b1 else ""}
    return {"values_removed": sorted(ov - nv), "values_added": sorted(nv - ov), "values_old": sorted(ov), "values_new": sorted(nv),
            "must": (len(MUST_RE.findall(old_text)), len(MUST_RE.findall(new_text))),
            "may": (len(MAY_RE.findall(old_text)), len(MAY_RE.findall(new_text))),
            "refs_removed": sorted(_refs(old_text) - _refs(new_text)), "refs_added": sorted(_refs(new_text) - _refs(old_text)),
            "main": main, "spans": len(ops), "ratio": round(_ratio(old_text, new_text), 3)}


def describe(ch: dict) -> str:
    parts = []
    if ch["values_removed"] or ch["values_added"]:
        parts.append("значения: было " + (", ".join(map(_fmt_val, ch["values_removed"])) or "нет") +
                     ", стало " + (", ".join(map(_fmt_val, ch["values_added"])) or "нет"))
    (om, nm), (oy, ny) = ch["must"], ch["may"]
    if nm < om and ny > oy:
        parts.append("обязательное требование стало допускаемым («должен» → «может»)")
    elif nm > om and ny < oy:
        parts.append("допускаемое стало обязательным («может» → «должен»)")
    elif nm != om:
        parts.append(f"обязательных формулировок {'больше' if nm > om else 'меньше'} ({om} → {nm})")
    if ch["refs_removed"] or ch["refs_added"]:
        parts.append("ссылки: " + ", ".join(ch["refs_removed"] or ["—"]) + " → " + ", ".join(ch["refs_added"] or ["—"]))
    m = ch.get("main")
    if m and m["was"] and m["now"]:
        parts.append(f"было «{m['was']}», стало «{m['now']}»")
    elif m and m["now"]:
        parts.append(f"добавлено «{m['now']}»")
    elif m and m["was"]:
        parts.append(f"исключено «{m['was']}»")
    if ch["spans"] > 1:
        parts.append(f"изменённых мест в пункте {ch['spans']}")
    return "; ".join(parts)


FIELD_WEIGHT = {"s": 1.0, "p": 0.4, "t": 0.6}


def _features(text: str, title: str = "") -> list[str]:
    """Признаки окна: основа 6 букв (s), короткая основа 4 буквы для «этаж» / «этажный» (p), основа из заглавий пункта (t)."""
    out = []
    for w in stems(text):
        if w.isdigit() or w in STOP_STEMS:
            continue
        out.append("s" + w)
        if len(w) >= 5:
            out.append("p" + w[:4])
    out += ["t" + w for w in stems(title) if not w.isdigit() and w not in STOP_STEMS]
    return out


class _BM25:
    """BM25 по основам слов для окон пунктов двух редакций; заглавия пункта и его разделов весят больше текста."""

    def __init__(self, rows: list[dict]):
        self.rows = rows
        self.freq = [Counter(_features(r["text"], r.get("title", ""))) for r in rows]
        self.length = [sum(f.values()) for f in self.freq]
        self.avg = sum(self.length) / max(1, len(self.length))
        self.df = Counter()
        for f in self.freq:
            self.df.update(f.keys())
        self.n = len(rows)

    def idf(self, w: str) -> float:
        return math.log(1 + (self.n - self.df[w] + 0.5) / (self.df[w] + 0.5))

    def top(self, query: str, side: str, limit: int = 5) -> list[tuple[int, float]]:
        base = {w[1:] for w in _features(query) if w[0] == "s"}
        q = {"s" + w for w in base} | {"p" + w[:4] for w in base if len(w) >= 5} | {"t" + w for w in base}
        out = []
        for i, f in enumerate(self.freq):
            if self.rows[i]["side"] != side:
                continue
            norm = 1.2 * (0.25 + 0.75 * self.length[i] / max(1, self.avg))
            s = sum(FIELD_WEIGHT[w[0]] * self.idf(w) * f[w] * 2.2 / (f[w] + norm) for w in q if w in f)
            if s > 0:
                out.append((i, s))
        return sorted(out, key=lambda x: -x[1])[:limit]


class Comparison:
    """Две редакции одного стандарта: пункты, пары, изменения и поиск требований ТЗ по пунктам обеих редакций."""

    def __init__(self, old_doc: dict, new_doc: dict):
        self.old_doc, self.new_doc = old_doc, new_doc
        self.old, self.new = clauses(old_doc), clauses(new_doc)
        a = align(self.old, self.new)
        self.pairs, self.removed, self.added = a["pairs"], a["removed"], a["added"]
        self.back = {j: (i, r) for i, (j, r) in self.pairs.items()}
        self.changed = {i: changes(self.old[i]["text"], self.new[j]["text"]) for i, (j, r) in self.pairs.items() if r < SAME}
        rows = []
        for side, cl in (("o", self.old), ("n", self.new)):
            for k, c in enumerate(cl):
                if c["meta"]:
                    continue
                for piece in _windows(c["text"], 900):
                    rows.append({"side": side, "clause": k, "text": piece, "title": (c.get("path", "") + " " + c.get("title", "")).strip()})
        self.bm25 = _BM25(rows)

    def summary(self) -> dict:
        same = sum(1 for i, (j, r) in self.pairs.items() if r >= SAME)
        return {"old_clauses": len(self.old), "new_clauses": len(self.new), "unchanged": same, "changed": len(self.changed),
                "added": len(self.added), "removed": len(self.removed)}

    def most_changed(self, limit: int = 6) -> list[str]:
        items = sorted(((i, ch) for i, ch in self.changed.items() if not self.old[i]["meta"]), key=lambda x: x[1]["ratio"])
        return [self.new[self.pairs[i][0]]["number"] for i, _ in items[:limit]]

    def counterpart_new(self, number: str, quote: str = "") -> dict | None:
        """Пункт новой редакции для пункта прежней (по номеру и цитате из основания замечания)."""
        cands = [i for i, c in enumerate(self.old) if c["number"] == number and (not quote or quote[:60] in c["text"])]
        cands = cands or [i for i, c in enumerate(self.old) if c["number"] == number]
        if not cands:
            return None
        i = cands[0]
        if i not in self.pairs:
            return {"status": "removed", "old": self.old[i], "new": None, "changes": None}
        j, r = self.pairs[i]
        return {"status": "same" if r >= SAME else "changed", "old": self.old[i], "new": self.new[j], "changes": self.changed.get(i)}

    def dense_vectors(self, enc):
        """Векторы окон обеих редакций; кэш на диске по содержимому, чтобы не кодировать при каждой проверке."""
        if getattr(self, "_dense", None) is not None:
            return self._dense
        import hashlib
        import numpy as np
        from .models import doc_text, prefix
        model = getattr(enc, "_tz_model", "")
        texts = [prefix(model) + doc_text({"document": self.new_doc["document"], "number": "", "title": r["title"], "text": r["text"]})
                 for r in self.bm25.rows]
        key = hashlib.sha1(("\n".join([model] + texts)).encode("utf8")).hexdigest()[:24]
        path = Path(refdata.data_dir()) / "cache" / "editions" / f"{key}.npy"
        try:
            self._dense = np.load(path)
        except (OSError, ValueError):
            self._dense = enc.encode(texts, batch_size=8, normalize_embeddings=True, show_progress_bar=False).astype("float32")
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                np.save(path, self._dense)
            except OSError:
                pass
        return self._dense

    def _ranked(self, query: str, side: str) -> list[tuple[int, float, float]]:
        """(окно, оценка BM25, косинус) по одной редакции; с моделью эмбеддингов порядок по RRF двух списков."""
        lex = self.bm25.top(query, side, limit=40)
        enc = _encoder()
        if enc is None:
            return [(row, s, 0.0) for row, s in lex[:12]]
        from .models import prefix
        vecs = self.dense_vectors(enc)
        qv = enc.encode([prefix(getattr(enc, "_tz_model", ""), True) + query], normalize_embeddings=True, show_progress_bar=False)[0]
        cos = vecs @ qv
        side_rows = [i for i, r in enumerate(self.bm25.rows) if r["side"] == side]
        dense = sorted(side_rows, key=lambda i: -cos[i])[:40]
        points = {}
        for seq in ([row for row, _ in lex], dense):
            for rank, i in enumerate(seq, 1):
                points[i] = points.get(i, 0.0) + 1 / (60 + rank)
        lexd = dict(lex)
        order = sorted(points, key=lambda i: -points[i])[:12]
        return [(i, lexd.get(i, 0.0), float(cos[i])) for i in order]

    def _candidates(self, query: str, side: str, topic: set[str], own: str = "") -> list[tuple[int, float, list[str]]]:
        """Пункты редакции по запросу, прошедшие проверку общих слов, в порядке ранжирования. own: текст самого требования
        без контекста раздела, по нему считаются общие слова: совпадение только по заголовку раздела ТЗ пункт не подтверждает."""
        q = {w for w in stems(own or query) if w not in STOP_STEMS and not w.isdigit() and len(w) >= 4}
        cl = self.old if side == "o" else self.new
        dense = _encoder() is not None
        out, seen = [], set()
        for row, score, cos in self._ranked(query, side):
            k = self.bm25.rows[row]["clause"]
            if k in seen:
                continue
            seen.add(k)
            common = q & set(stems(cl[k]["text"] + " " + cl[k].get("title", "") + " " + cl[k].get("path", "")))
            lexical_ok = len(common) >= 3 or (len(common) >= MATCH_MIN and common & topic)
            dense_ok = (cos >= DENSE_MIN + 0.05 and common) or (cos >= DENSE_MIN and len(common) >= MATCH_MIN)
            if (dense and dense_ok) or (not dense and lexical_ok):
                out.append((k, (round(cos, 3) if dense else round(score, 2)), sorted(common)))
        return out

    def _best(self, query: str, side: str, topic: set[str], own: str = ""):
        c = self._candidates(query, side, topic, own)
        return c[0] if c else None

    def match(self, query: str, topic: set[str], own: str = "") -> dict | None:
        """Пункт по требованию ТЗ: сначала лучший пункт новой редакции и его пара в прежней; если пары нет, лучший пункт
        прежней редакции, когда он похож на найденный новый. Без находки в новой редакции: пункт прежней и его судьба."""
        cn, co = self._candidates(query, "n", topic, own), self._candidates(query, "o", topic, own)
        if not co and not cn:
            return None
        bn, bo = (cn or [None])[0], (co or [None])[0]
        # пара пунктов из сопоставления редакций, найденная по запросу с обеих сторон, надёжнее лучшего пункта одной стороны
        rank_n = {k: r for r, (k, _, _) in enumerate(cn)}
        both = [(r + rank_n[self.pairs[k][0]], r, k) for r, (k, _, _) in enumerate(co) if k in self.pairs and self.pairs[k][0] in rank_n]
        i = j = None
        if both:
            _, r, i = min(both)
            j = self.pairs[i][0]
            bo, bn = co[r], cn[rank_n[j]]
        elif bn:
            j = bn[0]
            if j in self.back:
                i = self.back[j][0]
            elif bo and _ratio(self.old[bo[0]]["text"], self.new[j]["text"]) >= 0.3:
                i = bo[0]
        else:
            i = bo[0]
            j = self.pairs[i][0] if i in self.pairs else None
        old = self.old[i] if i is not None else None
        new = self.new[j] if j is not None else None
        score = (bn or bo)[1]
        common = sorted(set((bo or (0, 0, []))[2]) | set((bn or (0, 0, []))[2]))
        how = "эмбеддинги и BM25 (RRF)" if _encoder() is not None else "BM25 по основам слов"
        if old and new:
            ch = self.changed.get(i) if self.pairs.get(i, (None,))[0] == j else changes(old["text"], new["text"])
            status = "same" if ch is None or ch["ratio"] >= SAME else "changed"
            return {"old": old, "new": new, "status": status, "changes": ch, "score": score, "common": common, "how": how}
        return {"old": old, "new": new, "status": "removed" if old else "added", "changes": None, "score": score, "common": common,
                "how": how}


_ENC: dict = {}


def _encoder():
    """Модель эмбеддингов для подбора пунктов. TZCHECK_EDITIONS_DENSE=auto (по умолчанию): та же модель, что у гибридного RAG,
    и только когда RAG включён (TZCHECK_RAG=1, режим не bm25): модель уже загружена для поиска, отдельной загрузки нет.
    1: загрузить модель и без RAG; 0: только BM25. В режиме правил без RAG подбор идёт BM25 за доли секунды."""
    v = os.environ.get("TZCHECK_EDITIONS_DENSE", "auto").lower()
    if v in ("0", "off", "false"):
        return None
    from . import _enabled, _searcher, default_mode
    try:
        mode, model = default_mode()
    except Exception:
        return None
    if model in _ENC:
        return _ENC[model]
    enc = None
    if v == "auto":
        if not _enabled() or mode == "bm25":
            return None
        try:
            enc = getattr(_searcher(mode, model), "encoder", None)
        except Exception:
            enc = None
    else:
        try:
            from .models import encoder
            enc = encoder(model)
        except Exception:
            enc = None
    if enc is not None:
        enc._tz_model = model
    _ENC[model] = enc
    return enc


@lru_cache(maxsize=16)
def _comparison(old_key: tuple, new_key: tuple) -> Comparison:
    return Comparison(_load(*old_key), _load(*new_key))


def comparison(old: str, new: str) -> Comparison | None:
    po, pn = _path(old), _path(new)
    if not po or not pn:
        return None
    return _comparison((po, os.path.getmtime(po)), (pn, os.path.getmtime(pn)))


def topic_stems(*titles: str) -> set[str]:
    out = set()
    for t in titles:
        for w in re.findall(r"[А-Яа-яЁё]{5,}", t or ""):
            w = w.lower().replace("ё", "е")
            if w not in TOPIC_STOP:
                out.add(w[:6])
    return out - STOP_STEMS


def requirement_blocks(doc, topic: set[str], skip: set[str]) -> list:
    """Блоки ТЗ по теме стандарта: строка таблицы или фраза со словом из заглавия, без самих ссылок на стандарты."""
    out, section = [], {}
    for b in doc.blocks:
        table = re.match(r"(t\d+)r\d+", b.id)
        cells = [c for c in (x.strip() for x in b.text.split("|")) if c]
        if table and len(cells) == 1:
            section[table.group(1)] = cells[0]
            continue
        if b.id in skip or b.id.startswith("h") or DESIGNATION.search(b.text) or len(b.text.strip()) < 15:
            continue
        context = b.text + " " + (section.get(table.group(1), "") if table else "")
        if {w.lower().replace("ё", "е")[:6] for w in re.findall(r"[А-Яа-яЁё]{5,}", context)} & topic:
            b_ctx = section.get(table.group(1), "") if table else ""
            out.append((b, b_ctx))
    return out


def _tz_quote(b) -> str:
    cells = [c.strip() for c in b.text.split("|")]
    cells = [c for c in cells if c and not re.fullmatch(r"\d{1,3}\.?", c)]
    return (" | ".join(cells) if cells else b.text)[:240]


def _verdict(tz_text: str, m: dict) -> tuple[str, str]:
    """(вывод, довод): соответствует / не соответствует / сверить вручную."""
    if m["status"] == "same":
        return "соответствует", "пункт в новой редакции не изменился"
    if m["status"] == "added":
        return "сверить вручную", "в прежней редакции такого пункта не было, требование ТЗ сверить с новым пунктом"
    if m["status"] == "removed":
        return "сверить вручную", "пункт исключён из новой редакции, требование ТЗ опирается на исключённый текст"
    ch = m["changes"] or {}
    tz_vals = values(tz_text)
    gone = sorted(v for v in tz_vals if v in set(ch.get("values_removed", [])))
    fresh = sorted(v for v in tz_vals if v in set(ch.get("values_new", [])) and v in set(ch.get("values_added", [])))
    if gone and not fresh:
        return "не соответствует", "в ТЗ " + ", ".join(map(_fmt_val, gone)) + " по прежней редакции, в новой " + \
            (", ".join(map(_fmt_val, ch.get("values_added", []))) or "этого значения нет")
    if fresh and not gone:
        return "соответствует", "значения ТЗ " + ", ".join(map(_fmt_val, fresh)) + " совпадают с новой редакцией"
    return "сверить вручную", "изменённых значений требование ТЗ не задаёт, формулировку сверить с новым пунктом"


def _label(c: dict | None, designation: str) -> str:
    if not c:
        return "—"
    if c["kind"] == "table":
        return f"{designation}, таблица к п. {c['number']}"
    num = c["number"] if c["number"].startswith("прил.") else "п. " + c["number"]
    return f"{designation}, {num}" + (f" «{c['title'][:80]}»" if c.get("title") and not c["number"].startswith("прил.") else "")


def check_requirements(doc, comp: Comparison, old: str, new: str, skip: set[str]) -> list[dict]:
    titles = [(refdata.gost_status(x) or {}).get("title") or "" for x in (old, new)]
    topic = topic_stems(*titles) or topic_stems(" ".join(b["text"] for b in comp.new_doc["blocks"][:12])[:400])
    items, best = [], {}
    for b, ctx in requirement_blocks(doc, topic, skip):
        q = _tz_quote(b)
        cells = q.split(" | ")
        query = ((cells[0] + " ") * 2 + " ".join(cells[1:]) if len(cells) > 1 else q) + " " + re.sub(r"^Раздел\s+\d+\.?\s*", "", ctx)
        m = comp.match(query, topic, q)
        if not m:
            continue
        verdict, why = _verdict(b.text, m)
        it = {"block_id": b.id, "location": b.location(), "tz_quote": q, "status": m["status"], "verdict": verdict, "why": why,
              "old_label": _label(m["old"], old), "new_label": _label(m["new"], new),
              "old_quote": best_quote(m["old"]["text"], q) if m["old"] else "", "new_quote": "",
              "change": describe(m["changes"]) if m.get("changes") and m["status"] == "changed" else "",
              "score": round(m["score"], 3), "common": m["common"], "how": m["how"], "context": ctx, "dup": False}
        if m["new"]:
            it["new_quote"] = best_quote(m["new"]["text"], q + " " + (((m.get("changes") or {}).get("main") or {}).get("now") or ""))
        key = (it["old_label"], it["new_label"])
        if key in best and best[key]["score"] >= it["score"]:
            it["dup"] = True
        elif key in best:
            best[key]["dup"] = True
            best[key] = it
        else:
            best[key] = it
        items.append(it)
    order = {"не соответствует": 0, "сверить вручную": 1, "соответствует": 2}
    items.sort(key=lambda x: (x["dup"], order[x["verdict"]], x["status"] == "same", -x["score"]))
    return items


def item_line(it: dict) -> str:
    head = {"changed": f"в новой редакции {it['new_label']} изменился", "same": f"{it['new_label']} не изменился",
            "added": f"в новой редакции появился {it['new_label']}", "removed": f"{it['old_label']} исключён из новой редакции"}[it["status"]]
    s = f"Требование ТЗ «{it['tz_quote']}» ({it['location']}): {head}"
    if it["status"] == "changed":
        s += f". Было ({it['old_label']}): «{it['old_quote']}». Стало: «{it['new_quote']}»"
        if it["change"]:
            s += f". Что изменилось: {it['change']}"
    elif it["status"] == "added":
        s += f": «{it['new_quote']}»"
    elif it["status"] == "removed":
        s += f": «{it['old_quote']}»"
    verdict = {"соответствует": "требование ТЗ соответствует новой редакции", "не соответствует": "требование ТЗ не соответствует новой редакции",
               "сверить вручную": "сверить вручную"}[it["verdict"]]
    return s + f". Вывод: {verdict}{' (по оценке модели)' if it.get('by_model') else ''}: {it['why']}."


def source_line(new: str, doc: dict | None, rec: dict | None) -> str:
    if doc:
        return (f"{new}: текст {doc.get('source', '')} (получен {doc.get('retrieved') or (rec or {}).get('retrieved') or '?'}, "
                f"статус {doc.get('status') or 'не проверен'}, дата введения {doc.get('edition_date') or 'не указана'})")
    if rec and not rec.get("ok"):
        return f"текст {new} в открытых источниках не найден ({'; '.join(rec.get('errors', [])[:3])}), сверить требования ТЗ вручную"
    return (f"текста {new} нет ни в корпусе, ни в сохранённых текстах (обновлятор gost_texts не запускался или не было сети), "
            "сверить требования ТЗ вручную")


def _news(replaced_by: str) -> list[str]:
    return [refdata.norm_designation(" ".join(m.group().split())) for m in DESIGNATION.finditer(replaced_by or "")] or \
        ([refdata.norm_designation(replaced_by)] if replaced_by else [])


def annotate(doc, findings) -> list:
    """Дополняет замечания о заменённых стандартах сверкой с новой редакцией. Возвращает отдельные замечания о требованиях
    ТЗ, которые по числам не соответствуют новой редакции."""
    from ..model import Finding
    extra = []
    skip = {f.block_id for f in findings if getattr(f, "_replaced", None)}
    for f in findings:
        st = getattr(f, "_replaced", None)
        news = _news((st or {}).get("replaced_by"))
        if not news:
            continue
        old, new = refdata.norm_designation(st["designation"]), news[0]
        new_doc = find_document(new)
        f.evidence["новая редакция"] = source_line(new, new_doc, text_record(new))
        if not new_doc:
            f.explanation += f" Текст {new} в открытых источниках не найден, требования ТЗ с новой редакцией сверить вручную."
            continue
        if not find_document(old):
            f.evidence["сравнение редакций"] = f"текста {old} нет в корпусе, изменения пунктов не выделены"
            f.explanation += f" Текст {new} получен ({new_doc.get('source', '')}), прежней редакции в корпусе нет: требования ТЗ сверить с ним вручную."
            continue
        comp = comparison(old, new)
        s = comp.summary()
        top = comp.most_changed(6)
        f.evidence["сравнение редакций"] = (f"{old} → {new}: пунктов и таблиц {s['old_clauses']} → {s['new_clauses']}, изменено {s['changed']}, "
                                            f"новых {s['added']}, исключено {s['removed']}, без изменений {s['unchanged']}"
                                            + (f"; сильнее всего изменились пп. {', '.join(top)}" if top else ""))
        titles = [(refdata.gost_status(x) or {}).get("title") or "" for x in (old, new)]
        f._edition = {"old": old, "new": new, "items": check_requirements(doc, comp, old, new, skip), "topic": topic_stems(*titles)}
    _dedupe_blocks([f for f in findings if getattr(f, "_edition", None)])
    for f in findings:
        if not getattr(f, "_edition", None):
            continue
        old = f._edition["old"]
        render(f)
        for it in f._edition["items"]:
            if it["verdict"] != "не соответствует" or it["dup"]:
                continue
            b = doc.block(it["block_id"])
            g = Finding(b.id, b.text[:200], "Стандарты (ГОСТ, поверка)",
                        f"Требование ТЗ «{it['tz_quote']}» соответствует {old}, а в {it['new_label']} значение изменено: {it['change']}. "
                        f"Вывод сверки: {it['why']}.",
                        f"Привести требование к {it['new_label']}: «{it['new_quote'][:200]}».", "medium", ["ROSSTANDART"], True,
                        "rule:gost-edition", 0.6, b.location())
            g.evidence.update({"пункт прежней редакции": it["old_label"], "цитата прежней редакции": it["old_quote"],
                               "пункт новой редакции": it["new_label"], "цитата новой редакции": it["new_quote"],
                               "новая редакция": f.evidence["новая редакция"]})
            extra.append(g)
    return extra


def _dedupe_blocks(found) -> None:
    """Одно требование ТЗ относится к одному стандарту. Сначала по разделу ТЗ: если заголовок раздела («Диспетчеризация»)
    совпадает со словами заглавия только одного из стандартов (без общих для них слов вроде «лифты»), требование остаётся
    у него, даже если пункт нашёлся только в другом стандарте. Иначе при подборе эмбеддингами остаётся стандарт с ближайшим пунктом (косинусы сравнимы); при BM25 оценки
    разных стандартов несравнимы, и требование остаётся у всех."""
    if len(found) < 2:
        return
    topics = {id(f): f._edition.get("topic") or set() for f in found}
    distinct = {id(f): topics[id(f)] - set().union(*(t for k, t in topics.items() if k != id(f))) for f in found}
    by_block = {}
    for f in found:
        for it in f._edition["items"]:
            if not it["dup"]:
                by_block.setdefault(it["block_id"], []).append((f, it))
    for group in by_block.values():
        ctx = set(stems(group[0][1].get("context", "")))
        owners = [(f, it) for f, it in group if distinct[id(f)] & ctx]
        if len(group) == 1:
            # раздел ТЗ про другой стандарт из этого же ТЗ («Диспетчеризация» при сверке 28911): требование не его
            f, it = group[0]
            it["dup"] = not owners and any(distinct[id(g)] & ctx for g in found if g is not f)
            continue
        if len(owners) == 1:
            for f, it in group:
                it["dup"] = it is not owners[0][1]
            continue
        if all(it.get("how", "").startswith("эмбеддинги") for _, it in group):
            best = max(group, key=lambda x: x[1]["score"])[1]
            for _, it in group:
                it["dup"] = it is not best


def _clip(text: str, limit: int) -> str:
    """Обрезка описания изменения по границе части («; »), а внутри части по слову."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    k = cut.rfind("; ")
    if k > limit // 3:
        return cut[:k]
    cut = cut[:cut.rfind(" ")].rstrip(" ,;«") + "…"
    return cut + "»" if cut.count("«") > cut.count("»") else cut


def render(f) -> None:
    """Текст замечания и основание из результатов сверки (повторно после решений модели)."""
    ed = getattr(f, "_edition", None)
    if not ed:
        return
    items = ed["items"]
    shown = [it for it in items if not it["dup"] and it["status"] != "same"][:MAX_ITEMS]
    same = [it for it in items if not it["dup"] and it["status"] == "same"]
    lines = [item_line(it) for it in shown]
    if same:
        lines.append("Пункты без изменений по требованиям ТЗ: " + "; ".join(f"«{it['tz_quote'][:80]}» → {it['new_label']}" for it in same[:6]))
    f.evidence["сверка требований ТЗ с новой редакцией"] = lines or [f"требований ТЗ по теме {ed['new']} не найдено"]
    base = f.explanation.split(" Сверка с текстом ")[0]
    fix = f.fix.split(" Пункты для сверки: ")[0]
    if shown:
        bad = sum(1 for it in shown if it["verdict"] == "не соответствует")
        ok = sum(1 for it in shown if it["verdict"] == "соответствует")
        manual = len(shown) - bad - ok
        first = shown[0]
        where = first["new_label"].split(", ", 1)[-1] if first["new_label"] != "—" else first["old_label"].split(", ", 1)[-1]
        head = {"changed": f"изменился {where}", "added": f"появился {where}", "removed": f"исключён {where}"}[first["status"]]
        tail = f" Сверка с текстом {ed['new']}: для требования ТЗ «{first['tz_quote'][:90]}» {head}"
        if first["change"]:
            tail += f" ({_clip(first['change'], 260)})"
        tail += (f". Требований ТЗ, которых касаются изменения: {len(shown)}" + (f", не соответствуют {bad}" if bad else "")
                 + (f", соответствуют {ok}" if ok else "") + (f", сверить вручную {manual}" if manual else "")
                 + ". Цитаты обоих пунктов в основании вывода.")
        f.explanation = base + tail
        f.fix = fix.rstrip(".") + ". Пункты для сверки: " + ", ".join(dict.fromkeys(
            (it["new_label"] if it["new_label"] != "—" else it["old_label"]).split(", ", 1)[-1] for it in shown)) + "."
    elif same:
        f.explanation = base + f" Сверка с текстом {ed['new']}: пункты, на которые опираются требования ТЗ ({len(same)}), не изменились."


def counterpart(document: str, number: str, quote: str = "") -> dict | None:
    """Для пункта заменённого стандарта из основания замечания: тот же пункт в новой редакции и что в нём изменилось."""
    st = refdata.gost_status(document) or {}
    news = _news(st.get("replaced_by")) if st.get("inactive") else []
    if not news:
        return None
    comp = comparison(document, news[0])
    if not comp:
        return {"new_document": news[0], "status": "no_text", "line": source_line(news[0], find_document(news[0]), text_record(news[0]))}
    cp = comp.counterpart_new(number, quote)
    if cp:
        cp["new_document"] = news[0]
        cp["label"] = _label(cp["new"], news[0])
        cp["quote"] = best_quote(cp["new"]["text"], quote or cp["old"]["text"][:300]) if cp["new"] else ""
        cp["change"] = describe(cp["changes"]) if cp.get("changes") else ""
    return cp


MODEL_PROMPT = """Ниже требования из ТЗ на закупку и пункты прежней и новой редакции стандарта, на который ссылается ТЗ.
Для каждого реши, соответствует ли требование ТЗ НОВОЙ редакции пункта. Отвечай только по приведённым текстам.
Верни JSON {{"items": [{{"n": номер, "verdict": "соответствует" | "не соответствует" | "неясно", "reason": "до 200 знаков"}}]}}.

{items}"""


def model_review(findings, scr) -> int:
    """Спорные пункты сверки (код не решил по числам) решает модель, если она включена; ТЗ маскируется до отправки."""
    from .. import llm
    todo = [(f, it) for f in findings for it in (getattr(f, "_edition", None) or {}).get("items", [])
            if it["verdict"] == "сверить вручную" and not it["dup"] and it["new_quote"]]
    if not todo or not llm.enabled():
        return 0
    todo = todo[:20]
    lines = [f"{n}. ТЗ: «{scr.scrub(it['tz_quote'])}»; прежняя редакция ({it['old_label']}): «{it['old_quote']}»; "
             f"новая редакция ({it['new_label']}): «{it['new_quote']}»" for n, (f, it) in enumerate(todo)]
    try:
        data = llm._call(MODEL_PROMPT.format(items="\n".join(lines)), 2000)
    except Exception:
        return 0
    done = 0
    for d in data.get("items", []) if isinstance(data, dict) else []:
        try:
            f, it = todo[int(d.get("n"))]
        except (TypeError, ValueError, IndexError):
            continue
        v = str(d.get("verdict", ""))
        if v in ("соответствует", "не соответствует"):
            it.update(verdict=v, why=scr.restore(str(d.get("reason", "")))[:200] or it["why"], by_model=True)
            done += 1
    for f in {id(f): f for f, _ in todo}.values():
        render(f)
    return done
