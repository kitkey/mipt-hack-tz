"""Поиск пунктов норм по локальному корпусу: BM25 на леммах, опционально эмбеддинги, RRF и реранкер.

Найденный пункт прикладывается к замечанию как основание с дословной цитатой из корпуса.
Это ссылка для закупщика, а не юридическое заключение.
"""
import gzip
import json
import math
import os
import re
from collections import Counter
from functools import lru_cache
from pathlib import Path

from tzcheck import refdata

ROOT = Path(__file__).resolve().parents[2]
CORPUS = Path(os.environ.get("TZCHECK_RAG_CORPUS", ROOT / "data" / "norm_corpus"))
INDEX = CORPUS / "index.json"
INDEX_VERSION = 2
WORD = re.compile(r"[а-яёa-z]+|\d+(?:[.\-/]\d+)*", re.I)
DESIGNATION = re.compile(
    r"\b(?:ГОСТ(?:\s+Р)?(?:\s+(?:ISO|ИСО|ЕН|IEC|МЭК|OIML))?|СП|СНиП|СанПиН|ТР\s+ТС|ТР\s+ЕАЭС)\s+"
    r"\d[\d.]*(?:\s*[-–—/]\s*\d{2,4})+(?!\d)", re.I)
LAWS = {"102-ФЗ": "ч.", "223-ФЗ": "ч.", "ГК РФ часть вторая": "п."}
ARTICLE = re.compile(r"^(Статья\s+\d+(?:\.\d+)*)(?:\s*-\s*\d+)?\.?\s*(.*)", re.I)
PART = re.compile(r"^(\d+(?:\.\d+)?)\.\s+\S")
HEADING = re.compile(r"^(\d{1,2}(?:\.\d+){0,6})\.?\s+([А-ЯЁA-Z«\"(][^\n]*)")
WINDOW = 1400
NORM_ID = re.compile(r"^(102-FZ|223-FZ|GK)-(\d+(?:\.\d+)?)(?:-(\d+(?:\.\d+)?))?")
LAW_OF_ID = {"102-FZ": "102-ФЗ", "223-FZ": "223-ФЗ", "GK": "ГК РФ часть вторая"}
ENRICH_CATEGORIES = {"Ограничение конкуренции", "Стандарты (ГОСТ, поверка)", "Гарантийные условия",
                     "Неполнота или комплектность", "Некорректная характеристика", "Предмет закупки или наименование"}


def norm_document(s: str) -> str:
    s = s.upper().replace("–", "-").replace("—", "-").replace("ИСО", "ISO").replace("Ё", "Е")
    s = re.sub(r"\s*-\s*", "-", s)
    s = re.sub(r"\s*/\s*", "/", s)
    return re.sub(r"\s+", " ", s).strip(" .")


@lru_cache(maxsize=1)
def morph():
    import pymorphy3
    return pymorphy3.MorphAnalyzer()


@lru_cache(maxsize=200000)
def lemma(w: str) -> str:
    return morph().parse(w)[0].normal_form if re.search("[а-яё]", w) and len(w) > 3 else w


def tokens(s: str) -> list[str]:
    return [lemma(x.lower().replace("ё", "е")) for x in WORD.findall(s) if len(x) > 2 or x.isdigit()]


def _windows(text: str, size: int = WINDOW) -> list[str]:
    """Длинный пункт режется по строкам на окна; каждое окно остаётся точной подстрокой текста."""
    if len(text) <= size * 1.3:
        return [text]
    out, cur = [], ""
    for line in text.split("\n"):
        while len(line) > size * 1.5:
            cut = line.rfind(". ", 0, size)
            cut = cut + 1 if cut > size // 3 else size
            if cur:
                out.append(cur)
                cur = ""
            out.append(line[:cut].strip())
            line = line[cut:].strip()
        if cur and len(cur) + len(line) + 1 > size:
            out.append(cur)
            cur = line
        else:
            cur = cur + "\n" + line if cur else line
    if cur:
        if out and len(cur) < size // 4:
            out[-1] = out[-1] + "\n" + cur
        else:
            out.append(cur)
    return [x for x in out if x.strip()]


def chunks(doc: dict) -> list[dict]:
    """Фрагменты документа: статья и её часть для законов, пункт с иерархией для стандартов, таблицы отдельно."""
    out: list[dict] = []
    law = LAWS.get(doc["document"])
    state = {"number": "", "title": "", "article": "", "parts": []}
    parents: dict[int, str] = {}

    def emit(kind, number, title, text, article, hierarchy):
        text = text.strip()
        if len(text) <= 30 or not number:
            return
        pieces = _windows(text) if kind == "clause" else [text]
        for k, piece in enumerate(pieces):
            suffix = f":w{k}" if len(pieces) > 1 else ""
            out.append(dict(id=f"{doc['id']}:{number}:{len(out)}{suffix}", document=doc["document"], number=number,
                            article=article, title=title, hierarchy=hierarchy, text=piece,
                            edition=doc.get("edition", ""), edition_date=doc.get("edition_date"),
                            status=doc.get("status", "не проверен"), source=doc.get("source", ""), kind=kind))

    def flush():
        if state["parts"] and state["number"]:
            emit("clause", state["number"], state["title"], "\n".join(state["parts"]), state["article"],
                 [parents[k] for k in sorted(parents) if k < state["number"].count(".") + 1] if not law else [state["article"]])
        state["parts"] = []

    for block in doc["blocks"]:
        txt = block["text"].strip()
        if not txt:
            continue
        if block["type"] == "table":
            flush()
            if state["number"] and len(txt) > 30:
                emit("table", state["number"], state["title"], txt, state["article"], list(parents.values()))
            continue
        if law:
            m = ARTICLE.match(txt)
            if m:
                flush()
                state.update(number=m.group(1).strip(), article=m.group(1).strip(), title=m.group(2).strip()[:160], parts=[txt])
                continue
            p = PART.match(txt)
            if p and state["article"]:
                flush()
                state.update(number=f"{state['article']}, {law} {p.group(1)}", parts=[txt])
                continue
        else:
            m = HEADING.match(txt)
            if m and int(m.group(1).split(".")[0]) <= 40:
                flush()
                number = m.group(1)
                depth = number.count(".")
                parents[depth] = number
                for k in list(parents):
                    if k > depth:
                        del parents[k]
                state.update(number=number, article=number.split(".")[0], title=m.group(2).strip()[:160], parts=[txt])
                continue
        if state["number"]:
            state["parts"].append(txt)
    flush()
    return out


def _doc_files(corpus: Path):
    """Документы корпуса: <id>.json или сжатые <id>.json.gz (в репозитории лежат сжатые)."""
    out = {}
    for p in sorted(Path(corpus).glob("*.json.gz")) + sorted(Path(corpus).glob("*.json")):
        key = p.name[:-3] if p.name.endswith(".gz") else p.name
        if key not in ("index.json", "manifest.json"):
            out[key] = p
    return [out[k] for k in sorted(out)]


def _read_doc(p: Path):
    data = gzip.decompress(p.read_bytes()) if p.name.endswith(".gz") else p.read_bytes()
    return json.loads(data.decode("utf8"))


def _iter_docs(corpus: Path):
    for p in _doc_files(corpus):
        try:
            d = _read_doc(p)
        except (OSError, ValueError):
            continue
        if isinstance(d, dict) and "blocks" in d and d.get("document"):
            yield p, d


def build(corpus=None, index=None) -> int:
    corpus = Path(corpus or CORPUS)
    index = Path(index or corpus / "index.json")
    rows, seen = [], set()
    for _, d in _iter_docs(corpus):
        key = norm_document(d["document"])
        if key in seen:
            continue
        seen.add(key)
        if d["document"].upper().startswith(("ГОСТ", "СП", "СНИП")):
            try:
                st = refdata.gost_status(d["document"])
            except Exception:
                st = None
            if st and st.get("status"):
                d["status"] = st["status"]
                if st.get("replaced_by"):
                    d["replaced_by"] = st["replaced_by"]
        for row in chunks(d):
            if d.get("replaced_by"):
                row["replaced_by"] = d["replaced_by"]
            rows.append(row)
    for row in rows:
        row["tokens"] = tokens(row["document"] + " " + row["number"] + " " + row["title"] + " " + row["text"])
    index.parent.mkdir(parents=True, exist_ok=True)
    index.write_text(json.dumps({"version": INDEX_VERSION, "rows": rows}, ensure_ascii=False), encoding="utf8")
    return len(rows)


def ensure_index() -> bool:
    """Индекс BM25 не хранится в репозитории: собирается шагом Docker-сборки или при первой проверке с TZCHECK_RAG=1."""
    if INDEX.exists():
        return True
    try:
        return build() > 0
    except Exception:
        return False


def mentioned_documents(text: str, rows) -> set[str]:
    """Документы корпуса, обозначения которых названы в тексте (с поправкой на пропущенную «Р»)."""
    names = {norm_document(r["document"]): r["document"] for r in rows}
    found = set()
    for m in DESIGNATION.finditer(text):
        key = norm_document(m.group())
        if key in names:
            found.add(names[key])
        elif key.startswith("ГОСТ ") and "ГОСТ Р " + key[5:] in names:
            found.add(names["ГОСТ Р " + key[5:]])
    return found


def explicit_unknown(text: str, rows) -> bool:
    """В тексте названо обозначение, которого нет в корпусе, и нет ни одного известного."""
    designations = [m.group() for m in DESIGNATION.finditer(text)]
    return bool(designations) and not mentioned_documents(text, rows)


class Search:
    """BM25 (k1=1.2, b=0.75) по леммам pymorphy3."""
    name = "bm25"

    def __init__(self, path=None):
        data = json.loads(Path(path or INDEX).read_text(encoding="utf8"))
        self.rows = data["rows"]
        self.freq = [Counter(r["tokens"]) for r in self.rows]
        self.length = [sum(x.values()) for x in self.freq]
        self.avg = sum(self.length) / max(1, len(self.length))
        self.df = Counter()
        for counts in self.freq:
            self.df.update(counts.keys())
        self.n = len(self.rows)
        self.postings: dict[str, list[int]] = {}
        for i, counts in enumerate(self.freq):
            for w in counts:
                self.postings.setdefault(w, []).append(i)

    def idf(self, w):
        return math.log(1 + (self.n - self.df[w] + 0.5) / (self.df[w] + 0.5))

    def scores(self, query: str, allowed=None) -> dict[int, float]:
        q = set(tokens(query))
        out: dict[int, float] = {}
        matched: Counter = Counter()
        for w in q:
            idf = self.idf(w)
            for i in self.postings.get(w, ()):
                if allowed is not None and i not in allowed:
                    continue
                f = self.freq[i][w]
                out[i] = out.get(i, 0.0) + idf * f * 2.2 / (f + 1.2 * (0.25 + 0.75 * self.length[i] / max(1, self.avg)))
                if len(w) >= 4 and not w.isdigit():
                    matched[i] += 1
        return {i: s for i, s in out.items() if matched[i] >= min(3, max(1, len([w for w in q if len(w) >= 4])))}

    def allowed(self, documents=None, articles=None):
        if documents is None and articles is None:
            return None
        return {i for i, r in enumerate(self.rows)
                if (documents is None or r["document"] in documents)
                and (articles is None or (r["document"], r.get("article", "")) in articles)}

    def search(self, query, limit=5, threshold=2.5, documents=None, articles=None):
        if documents is None and articles is None:
            docs = mentioned_documents(query, self.rows)
            if docs:
                documents = docs
            elif explicit_unknown(query, self.rows):
                return []
        allowed = self.allowed(documents, articles)
        sc = self.scores(query, allowed)
        best = sorted(sc.items(), key=lambda x: -x[1])[:limit]
        return [dict(_public(self.rows[i]), score=round(s, 3)) for i, s in best if s >= threshold]


def _public(row: dict) -> dict:
    return {k: v for k, v in row.items() if k != "tokens"}


def best_quote(text: str, query: str, limit: int = 450) -> str:
    """Самая близкая к запросу строка или пара предложений пункта; всегда точная подстрока text."""
    q = set(tokens(query))
    pieces = [p for p in re.split(r"(?<=[.;:])\s+|\n", text) if len(p.strip()) > 15]
    if not pieces:
        return text[:limit].strip()
    best, best_score = pieces[0], -1.0
    for i, p in enumerate(pieces):
        cand = p if len(p) >= 120 or i + 1 >= len(pieces) else None
        if cand is None:
            j = text.find(pieces[i + 1], text.find(p) + len(p))
            cand = text[text.find(p):j + len(pieces[i + 1])] if j >= 0 else p
        overlap = len(q & set(tokens(cand)))
        score = overlap / math.sqrt(1 + len(cand) / 200)
        if score > best_score:
            best, best_score = cand, score
    best = best.strip()
    if len(best) > limit:
        cut = best.rfind(" ", 0, limit)
        best = best[:cut if cut > limit // 2 else limit].rstrip(" ,;")
    return best


def corpus_has_quote(hit: dict, quote: str) -> bool:
    """Цитата сверяется с исходным документом корпуса, а не только с индексом."""
    source_id = hit["id"].split(":", 1)[0]
    direct = [p for p in (CORPUS / (source_id + ".json"), CORPUS / (source_id + ".json.gz")) if p.exists()]
    candidates = direct[:1] or _doc_files(CORPUS)
    for path in candidates:
        try:
            doc = _read_doc(path)
            if not isinstance(doc, dict) or doc.get("id") != source_id:
                continue
            if doc.get("source") != hit.get("source"):
                return False
            return any(c["id"] == hit["id"] and quote in c["text"] for c in chunks(doc))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return False


def _enabled() -> bool:
    return os.environ.get("TZCHECK_RAG", "").lower() in ("1", "true", "yes", "on")


@lru_cache(maxsize=4)
def _searcher(mode: str, model: str):
    if mode != "bm25":
        try:
            from .models import HybridSearch
            return HybridSearch(model, mode=mode)
        except Exception:
            pass
    return Search(INDEX)


def default_mode() -> tuple[str, str]:
    """Режим и модель по умолчанию выбраны по замеру eval/rag (см. eval/rag/REPORT.md)."""
    from .models import DEFAULT_MODE, DEFAULT_MODEL
    return (os.environ.get("TZCHECK_RAG_MODE", DEFAULT_MODE), os.environ.get("TZCHECK_RAG_EMBED_MODEL", DEFAULT_MODEL))


def law_targets(norm_ids) -> set[tuple[str, str]]:
    out = set()
    for n in norm_ids:
        m = NORM_ID.match(n)
        if m:
            out.add((LAW_OF_ID[m.group(1)], "Статья " + m.group(2)))
    return out


def part_hint(norm_ids) -> set[str]:
    hints = set()
    for n in norm_ids:
        m = NORM_ID.match(n)
        if m and m.group(3):
            law = LAW_OF_ID[m.group(1)]
            hints.add(f"Статья {m.group(2)}, {LAWS[law]} {m.group(3)}")
    return hints


NORM_ITEM = re.compile(r"^(?:102-FZ|223-FZ|GK)-\d+(?:\.\d+)?-\d+(?:\.\d+)?-(\d+)")
SCOPE_TITLE = re.compile(r"област[ьи] применения", re.I)


def item_quote(text: str, norm_ids, limit: int = 450) -> str:
    """Подпункт «N)» части закона, если он назван в norm_ids; точная подстрока text."""
    for n in norm_ids:
        m = NORM_ITEM.match(n)
        if not m:
            continue
        hit = re.search(rf"(?m)^\s*{m.group(1)}\)\s.+", text)
        if hit:
            q = hit.group(0).strip()
            if len(q) > limit:
                cut = q.rfind(" ", 0, limit)
                q = q[:cut if cut > limit // 2 else limit].rstrip(" ,;")
            return q
    return ""


def scope_row(rows, docs):
    """Пункт «Область применения» названного стандарта: для замечаний о статусе самого стандарта."""
    for r in rows:
        if (r["document"] in docs and (SCOPE_TITLE.search(r.get("title", "")) or SCOPE_TITLE.match(r["text"].lstrip("1. ")))
                and re.search(r"распространя|устанавлива|применя", r["text"][:400], re.I)):
            return r
    return None


def evidence_for(query: str, norm_ids=(), search=None, threshold=None, about_document=False) -> dict | None:
    """Пункт нормы для текста замечания: сначала по названным статье и стандарту, иначе поиском с порогом отказа."""
    mode, model = default_mode()
    search = search or _searcher(mode, model)
    rows = search.rows
    docs = mentioned_documents(query, rows)
    arts = law_targets(norm_ids)
    hints = part_hint(norm_ids)
    how = None
    if arts:
        hits = search.search(query, limit=10, threshold=-1e9, articles=arts)
        exact = [h for h in hits if h["number"] in hints]
        hits = exact + [h for h in hits if h not in exact]
        if not hits:
            hits = [dict(_public(r), score=0.0) for r in rows if (r["document"], r.get("article")) in arts and r["number"] in hints][:1]
        how = "по статье из базы норм"
    elif docs and about_document:
        scope = scope_row(rows, docs)
        if scope is None:
            return None
        hits = [dict(_public(scope), score=0.0)]
        how = "область применения стандарта, названного в тексте"
    elif docs:
        hits = search.search(query, limit=10, threshold=-1e9, documents=docs)
        how = "по обозначению стандарта в тексте"
    else:
        if explicit_unknown(query, rows):
            return None
        from .models import REFUSE
        th = threshold if threshold is not None else getattr(search, "refuse_threshold", REFUSE["bm25"])
        hits = search.search(query, limit=5, threshold=th)
        how = "поиском по корпусу (проверить применимость)"
    if not hits:
        return None
    h = hits[0]
    if arts:
        with_item = [x for x in hits if x["number"] in hints and item_quote(x["text"], norm_ids)]
        if not with_item:
            with_item = [dict(_public(r), score=0.0) for r in rows
                         if (r["document"], r.get("article")) in arts and r["number"] in hints and item_quote(r["text"], norm_ids)][:1]
        h = with_item[0] if with_item else h
    quote = (item_quote(h["text"], norm_ids) if arts else "") or (
        best_quote(h["text"], "настоящий стандарт распространяется устанавливает") if how.startswith("область") else best_quote(h["text"], query))
    if not quote or not corpus_has_quote(h, quote):
        return None
    status = h.get("status") or "не проверен"
    return {"document": h["document"], "number": h["number"], "title": h.get("title", ""), "quote": quote,
            "status": status, "replaced_by": h.get("replaced_by"), "edition": h.get("edition", ""),
            "source": h.get("source", ""), "kind": h.get("kind", "clause"), "how": how, "score": h.get("score")}


OPEN_CATEGORIES = ("Стандарты (ГОСТ, поверка)", "Ограничение конкуренции", "Гарантийные условия")


def open_search_allowed(search) -> bool:
    """Открытый поиск по корпусу (без статьи в norm_ids и без обозначения в тексте). TZCHECK_RAG_OPEN=auto: только
    в гибридном режиме, где на 99 правках точность приложенного пункта 12 из 13; на одном BM25 было 3 из 4."""
    v = os.environ.get("TZCHECK_RAG_OPEN", "auto").lower()
    if v in ("1", "on", "true"):
        return True
    if v in ("0", "off", "false"):
        return False
    return getattr(search, "mode", "bm25") in ("hybrid", "rerank")


def enrich(findings) -> None:
    """К замечаниям про конкуренцию, стандарты, поверку и гарантию прикладывается пункт нормы с цитатой."""
    if not _enabled() or not ensure_index():
        return
    try:
        mode, model = default_mode()
        search = _searcher(mode, model)
    except (OSError, ValueError, KeyError, TypeError, ImportError):
        return
    allow_open = open_search_allowed(search)
    for f in findings:
        if f.category not in ENRICH_CATEGORIES and not f.norm_ids:
            continue
        text = f.quote + " " + f.explanation
        open_search = not law_targets(f.norm_ids) and not mentioned_documents(text, search.rows)
        if open_search and (not allow_open or f.category not in OPEN_CATEGORIES):
            continue
        try:
            ev = evidence_for(text, f.norm_ids, search, about_document=f.source == "rule:gost")
        except Exception:
            ev = None
        if not ev:
            continue
        label = f"{ev['document']}, {ev['number'] if ev['number'].startswith('Статья') else 'п. ' + ev['number']}"
        if ev["title"]:
            label += f" «{ev['title'][:120]}»"
        f.evidence["пункт нормы"] = label
        f.evidence["цитата нормы"] = ev["quote"]
        f.evidence["источник нормы"] = f"{ev['source']} (редакция: {ev['edition'] or 'не указана'}; статус: {ev['status']}; подбор: {ev['how']})"
        if ev["status"].lower().startswith(("заменён", "заменен", "отменён", "отменен", "утратил")):
            f.evidence["стандарт заменён"] = ev.get("replaced_by") or "да, см. сведения Росстандарта"
            f.needs_review = True
        if ev["how"].startswith("поиском"):
            f.needs_review = True
