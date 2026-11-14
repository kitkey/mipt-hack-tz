"""Тексты действующих редакций стандартов, на которые заменены стандарты из ТЗ и из корпуса норм.

Для каждого обозначения источники пробуются по порядку:
1. meganorm.ru: страница документа находится по спискам раздела «Актуализированная база» (ГОСТ, ГОСТ Р, СП), HTML;
2. то же через браузер (Playwright), если сайт не отдаёт страницу обычному запросу;
3. docs.cntd.ru: поиск и страница документа, берётся только полный текст (у части стандартов открыто одно начало);
4. просмотр protect.gost.ru: картинки страниц через API просмотрщика и OCR (PaddleOCR, если установлен).
Текст разбирается на блоки так же, как корпус (tzcheck/rag/collect_inventory.py), и проверяется: обозначение в начале
документа, объём, число нумерованных пунктов. Документ пишется в <data>/norm_texts/<id>.json.gz в формате
data/norm_corpus/, список источников и результатов в <data>/norm_texts.json, после записи пересобирается индекс RAG.

Запуск: python -m tzcheck.updater --only gost_texts; одно обозначение: python -m tzcheck.updater.gost_texts "ГОСТ 34441-2024"
"""
import argparse
import gzip
import hashlib
import json
import os
import re
import sys
from urllib.parse import quote, urljoin

import lxml.html

from .. import refdata
from .base import Context, Fetched, Source, SourceError, write_atomic
from .browser import Browser
from .net import Client, FetchError, now_iso

MEGANORM = "https://meganorm.ru/mega_doc/norm/"
MEGANORM_LISTS = {"ГОСТ": "gost_gosudarstvennyj-standart", "ГОСТ Р": "gost-r_gosudarstvennyj-standart", "СП": "sp_svod-pravil"}
MEGANORM_MAX_PAGES = int(os.environ.get("TZCHECK_MEGANORM_PAGES", "120"))
LIST_TTL = 7 * 86400
CNTD = "https://docs.cntd.ru"
PROTECT = "https://protect.gost.ru"
MIN_CHARS = 2500
MIN_CLAUSES = 6
REFRESH_DAYS = 30
DESIGNATION = re.compile(r"(?:ГОСТ(?:\s+Р)?(?:\s+(?:ISO|ИСО|IEC|МЭК|EN|ЕН))?|СП|ПНСТ)\s+\d[\d.]*(?:\s*[-–]\s*\d{2,4})+", re.I)
HEADING = re.compile(r"^(\d{1,2}(?:\.\d+){0,6})\.?\s+[А-ЯЁA-Z«\"(]")
INTRO_RE = re.compile(r"Дата\s+введения\s*[-–—]?\s*(\d{1,2}\s+[а-яё]+\s+\d{4}|\d{4}-\d{2}-\d{2}|\d{2}\.\d{2}\.\d{4})", re.I)
MONTHS = {"января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6, "июля": 7, "августа": 8, "сентября": 9,
          "октября": 10, "ноября": 11, "декабря": 12}
TRANSLIT = str.maketrans({"Г": "g", "О": "o", "С": "s", "Т": "t", "Р": "r", "П": "p", "Н": "n", "И": "i", "М": "m", "Э": "e",
                          "К": "k", "Е": "e"})


def texts_dir() -> str:
    return os.path.join(refdata.data_dir(), "norm_texts")


def doc_id(designation: str) -> str:
    key = refdata.norm_designation(designation).translate(TRANSLIT).lower()
    return "ed-" + re.sub(r"[^a-z0-9]+", "-", key).strip("-")


def doc_path(designation: str) -> str:
    return os.path.join(texts_dir(), doc_id(designation) + ".json.gz")


def same(a: str, b: str) -> bool:
    from ..rag import norm_document
    return norm_document(refdata.norm_designation(a)) == norm_document(refdata.norm_designation(b))


def split_designations(s: str | None) -> list[str]:
    return [" ".join(m.group().split()) for m in DESIGNATION.finditer(s or "")]


def designation_from_slug(name: str) -> str | None:
    """Имя страницы meganorm -> обозначение: gost_33984_1-2023_... -> ГОСТ 33984.1-2023, gost_r_53782-2010_... -> ГОСТ Р 53782-2010."""
    name = name.rsplit("/", 1)[-1]
    m = re.match(r"^(gost(?:_r)?)_(?:(iso|iec|en|mek)_)?(\d+(?:_\d+)*)-(\d{2,4})(?=_|\.|$)", name, re.I)
    if m:
        head = "ГОСТ Р" if m.group(1).lower() == "gost_r" else "ГОСТ"
        infix = {"iso": "ISO", "iec": "IEC", "en": "EN", "mek": "IEC"}.get((m.group(2) or "").lower())
        return " ".join(x for x in (head, infix, f"{m.group(3).replace('_', '.')}-{m.group(4)}") if x)
    m = re.match(r"^sp_(\d+(?:_\d+)*)_(\d{4})_", name, re.I)
    if m:
        return f"СП {m.group(1).replace('_', '.')}.{m.group(2)}"
    return None


def list_for(designation: str) -> str:
    key = refdata.norm_designation(designation)
    if key.startswith("СП "):
        return MEGANORM_LISTS["СП"]
    if key.startswith(("ГОСТ Р", "ПНСТ")):
        return MEGANORM_LISTS["ГОСТ Р"]
    return MEGANORM_LISTS["ГОСТ"]


def parse_listing(html: bytes | str, base: str) -> tuple[dict, set]:
    """Страница списка meganorm -> ({обозначение: адрес документа}, номера других страниц списка)."""
    tree = lxml.html.fromstring(html)
    docs, pages = {}, set()
    for href in tree.xpath("//a/@href"):
        m = re.search(r"_(\d+)\.html$", href)
        if m and re.search(r"(?:gosudarstvennyj-standart|svod-pravil)_\d+\.html$", href):
            pages.add(int(m.group(1)))
            continue
        d = designation_from_slug(href)
        if d and href.endswith(".html"):
            docs.setdefault(refdata.norm_designation(d), urljoin(base, href))
    return docs, pages


def parse_cntd(html: bytes | str) -> list[dict]:
    tree = lxml.html.fromstring(html)
    for x in tree.xpath("//script|//style"):
        x.drop_tree()
    body = tree.xpath('//div[contains(@class,"document-text_block")]') or tree.xpath('//div[contains(@class,"document-text")]')
    if not body:
        return []
    out = []
    for e in body[0].xpath(".//p[not(ancestor::table)] | .//table[not(ancestor::table)]"):
        if e.tag == "table":
            value = "\n".join(" | ".join(" ".join(c.text_content().split()) for c in row.xpath("./th|./td")) for row in e.xpath(".//tr"))
        else:
            value = " ".join(e.text_content().split())
        if value.strip(" |\n"):
            out.append({"type": "table" if e.tag == "table" else "text", "text": value})
    return out


def edition_date(blocks: list[dict]) -> str | None:
    head = " ".join(b["text"] for b in blocks[:60])
    m = INTRO_RE.search(head)
    if not m:
        return None
    s = m.group(1)
    if re.match(r"\d{4}-\d{2}-\d{2}$", s):
        return f"{s[8:10]}.{s[5:7]}.{s[:4]}"
    if re.match(r"\d{2}\.\d{2}\.\d{4}$", s):
        return s
    d, mon, y = s.split()
    return f"{int(d):02d}.{MONTHS.get(mon.lower(), 0):02d}.{y}" if mon.lower() in MONTHS else None


def check_text(designation: str, blocks: list[dict]):
    """Документ годится в корпус: то самое обозначение в начале, полный объём, нумерованные пункты."""
    from ..rag.collect_inventory import matches
    text = "\n".join(b["text"] for b in blocks)
    if not matches(refdata.norm_designation(designation), text[:12000]):
        raise SourceError("обозначение не подтверждено в начале документа")
    clauses = sum(1 for b in blocks if b["type"] == "text" and HEADING.match(b["text"]))
    if len(text) < MIN_CHARS or clauses < MIN_CLAUSES:
        raise SourceError(f"в открытом доступе только часть документа ({len(text)} знаков, нумерованных пунктов {clauses})")
    return len(text), clauses


class Fetcher:
    """Поиск и скачивание текста одного обозначения по цепочке источников."""

    def __init__(self, http: Client, browser: bool = True, say=print):
        self.http = http
        self.browser = browser and not http.offline
        self.say = say
        self._lists: dict = {}

    # meganorm
    def _listing(self, name: str) -> dict:
        if name in self._lists:
            return self._lists[name]
        hit = refdata.cache_get("norm_texts", "meganorm:" + name)
        state = hit["value"] if hit and hit.get("value") and refdata._age(hit.get("checked_at")) < LIST_TTL else {"docs": {}, "pages": []}
        self._lists[name] = state
        return state

    def meganorm_url(self, designation: str) -> str | None:
        key = refdata.norm_designation(designation)
        name = list_for(key)
        state = self._listing(name)
        if key in state["docs"]:
            return state["docs"][key]
        done = set(state["pages"])
        queue = [0] if 0 not in done else []
        queue += sorted(set(state.get("known", [])) - done)
        found = None
        while queue and len(done) < MEGANORM_MAX_PAGES:
            n = queue.pop(0)
            if n in done:
                continue
            url = f"{MEGANORM}{name}/{name}_{n}.html"
            try:
                r = self.http.get(url, ok=(200,))
            except FetchError as e:
                if "HTTP 404" in str(e):
                    done.add(n)
                    continue
                raise
            docs, pages = parse_listing(r.content, url)
            done.add(n)
            state["docs"].update(docs)
            known = set(state.get("known", [])) | pages
            state["known"] = sorted(known)
            queue = sorted(set(queue) | (known - done))
            if key in docs:
                found = docs[key]
                break
        state["pages"] = sorted(done)
        refdata.cache_put("norm_texts", "meganorm:" + name, state)
        return found

    def via_meganorm(self, designation: str, browser: bool = False) -> tuple[list[dict], str]:
        from ..rag.collect_inventory import parse_html
        url = self.meganorm_url(designation)
        if not url:
            raise SourceError("в списках meganorm.ru документа нет")
        if browser:
            with Browser(wait_ms=2500) as br:
                html = br.html(url)
        else:
            html = self.http.get(url).content
        return parse_html(html), url

    # docs.cntd.ru
    def via_cntd(self, designation: str) -> tuple[list[dict], str]:
        key = refdata.norm_designation(designation)
        r = self.http.get(f"{CNTD}/search", params={"q": key})
        tree = lxml.html.fromstring(r.content)
        url = None
        for a in tree.xpath('//a[contains(@href,"/document/")]'):
            title = " ".join(a.text_content().split())
            if any(same(d, key) for d in split_designations(title)):
                url = urljoin(CNTD, a.get("href"))
                break
        if not url:
            raise SourceError("поиск docs.cntd.ru не нашёл документ")
        return parse_cntd(self.http.get(url).content), url

    # protect.gost.ru
    def via_protect(self, designation: str) -> tuple[list[dict], str]:
        from .gost import parse_protect_rows
        key = refdata.norm_designation(designation)
        sp = key.startswith("СП ")
        r = self.http.get(f"{PROTECT}/{'sp' if sp else 'gost'}", params={"year": 0, "month": 0, "search": key.split(" ")[-1]})
        row = next((x for x in parse_protect_rows(r.text) if refdata.norm_designation(x["designation"]) == key), None)
        if not row:
            raise SourceError("на protect.gost.ru обозначение не найдено")
        details = PROTECT + row["href"]
        m = re.search(r'<doc-viewer\s+catalog="([^"]+)"\s+mdmid="([^"]+)"', self.http.get(details).text)
        if not m:
            raise SourceError("на карточке protect.gost.ru нет просмотра документа")
        info = self.http.get(f"{PROTECT}/api/docview/{quote(m.group(1))}/{quote(m.group(2))}/pages", save=False).json()
        pages = info.get("pages") or []
        if not pages or not info.get("token"):
            raise SourceError("просмотр protect.gost.ru не отдаёт страниц документа")
        from .. import ocr
        if not ocr.available():
            raise SourceError(f"просмотр protect.gost.ru отдаёт {len(pages)} страниц картинками, а OCR (PaddleOCR) не установлен")
        import numpy as np
        from PIL import Image
        import io
        blocks = []
        for n in range(1, len(pages) + 1):
            img = self.http.get(f"{PROTECT}/api/docview/img/{quote(info['token'])}/{n}", save=False).content
            arr = np.asarray(Image.open(io.BytesIO(img)).convert("RGB"))[:, :, ::-1].copy()
            blocks += [{"type": "text", "text": line} for line in ocr.ocr_array(arr) if line.strip()]
        return blocks, details

    def chain(self):
        out = [("meganorm", "meganorm.ru, страница документа (HTML)", lambda d: self.via_meganorm(d))]
        if self.browser:
            out.append(("meganorm_browser", "meganorm.ru через браузер (Playwright)", lambda d: self.via_meganorm(d, True)))
        out += [("cntd", "docs.cntd.ru, полный текст", self.via_cntd),
                ("protect", "protect.gost.ru, просмотр страниц и OCR", self.via_protect)]
        return out

    def fetch(self, designation: str, role: str = "new", replaces: str | None = None) -> dict:
        """Скачать, проверить и сохранить текст. Возвращает запись для norm_texts.json (ok=False с ошибками, если не вышло)."""
        key = refdata.norm_designation(designation)
        errors = []
        for via, title, fn in self.chain():
            try:
                blocks, url = fn(key)
                chars, clauses = check_text(key, blocks)
            except (FetchError, SourceError, ValueError, OSError) as e:
                errors.append(f"{title}: {e}")
                self.say(f"[gost_texts]   {key}: {title}: {e}")
                continue
            except Exception as e:
                errors.append(f"{title}: {type(e).__name__}: {str(e)[:200]}")
                self.say(f"[gost_texts]   {key}: {title}: {type(e).__name__}: {e}")
                continue
            st = refdata.gost_status(key) or {}
            doc = {"id": doc_id(key), "document": key, "edition": key.rsplit("-", 1)[-1] if "-" in key else "",
                   "edition_date": edition_date(blocks) or st.get("date_intro"), "status": st.get("status") or "не проверен",
                   "status_checked": st.get("checked_at"), "replaces": replaces, "role": role, "title": st.get("title"),
                   "source": url, "source_title": title, "via": via, "retrieved": now_iso()[:10], "blocks": blocks,
                   "extraction": "ocr" if via == "protect" else "html"}
            data = gzip.compress(json.dumps(doc, ensure_ascii=False).encode("utf-8"), mtime=0)
            write_atomic(doc_path(key), data)
            self.say(f"[gost_texts]   {key}: {title}, {chars} знаков, пунктов {clauses}")
            return {"designation": key, "ok": True, "role": role, "replaces": replaces, "id": doc["id"],
                    "file": os.path.basename(doc_path(key)), "source": url, "via": via, "via_title": title, "characters": chars,
                    "clauses": clauses, "blocks": len(blocks), "status": doc["status"], "edition_date": doc["edition_date"],
                    "retrieved": doc["retrieved"], "sha256": hashlib.sha256(data).hexdigest(), "errors": errors}
        return {"designation": key, "ok": False, "role": role, "replaces": replaces, "errors": errors, "retrieved": now_iso()[:10],
                "note": "текст редакции в открытых источниках не найден"}


def corpus_designations() -> set[str]:
    from .. import rag
    out = set()
    for _, d in rag._iter_docs(rag.CORPUS):
        out.add(refdata.norm_designation(d["document"]))
    return out


def have_text(designation: str) -> bool:
    from .. import rag
    key = rag.norm_document(refdata.norm_designation(designation))
    if os.path.exists(doc_path(designation)):
        return True
    return any(rag.norm_document(d) == key for d in corpus_designations())


def replacement_pairs(designations) -> list[tuple[str, str]]:
    """(заменённый, заменяющий) по каталогу статусов для набора обозначений."""
    mirror = refdata._mirror("gost.json.gz")
    pairs = []
    for k in sorted({refdata.norm_designation(x) for x in designations if x}):
        rec = mirror.get(k) or (refdata.cache_get("gost", k) or {}).get("value") or {}
        if rec.get("status") in refdata.INACTIVE and rec.get("replaced_by"):
            for new in split_designations(rec["replaced_by"]) or [rec["replaced_by"]]:
                pairs.append((k, refdata.norm_designation(new)))
    return pairs


class GostTextsSource(Source):
    name = "gost_texts"
    title = "Тексты действующих редакций стандартов (для сверки ТЗ с новой редакцией)"
    filename = "norm_texts.json"
    compare_fields = ("ok", "source", "sha256", "status", "edition_date")
    field_names = {"ok": "текст получен", "source": "источник", "sha256": "контрольная сумма", "status": "статус",
                   "edition_date": "дата введения"}
    drop_guard = 0.0

    def chain(self):
        return [("texts", "meganorm.ru → браузер → docs.cntd.ru → protect.gost.ru", self._collect)]

    def targets(self, prev: dict) -> list[tuple[str, str]]:
        from .gost import target_groups, WATCH
        try:
            groups = set().union(*target_groups(refdata._mirror("gost.json.gz")).values())
        except Exception:
            groups = {refdata.norm_designation(x) for x in WATCH} | {refdata.norm_designation(k) for k in refdata.cache_keys("gost")}
        names = groups | corpus_designations() | {r["replaces"] for r in prev.values() if r.get("replaces")}
        names |= {k for k, r in prev.items() if r.get("role") == "request"}
        return replacement_pairs(names)

    def _collect(self, prev: dict) -> Fetched:
        fetcher = Fetcher(self.http, self.ctx.browser, self.ctx.say)
        records = {k: dict(v) for k, v in prev.items()}
        pairs = self.targets(prev)
        jobs = []
        for old, new in pairs:
            jobs.append((new, "new", old))
            if not have_text(old):
                jobs.append((old, "old", None))
        got, failed, kept = 0, 0, 0
        for designation, role, replaces in dict.fromkeys(jobs):
            rec = records.get(designation)
            fresh = rec and rec.get("ok") and os.path.exists(doc_path(designation)) and not self.ctx.full and \
                refdata._age((rec.get("retrieved") or "") + "T00:00:00+00:00") < REFRESH_DAYS * 86400
            if fresh:
                kept += 1
                continue
            self.ctx.say(f"[gost_texts] {designation} ({'новая редакция взамен ' + replaces if replaces else 'текст для сверки'})")
            res = fetcher.fetch(designation, role, replaces)
            if not res["ok"] and rec and rec.get("ok") and os.path.exists(doc_path(designation)):
                rec["last_errors"] = res["errors"]
                kept += 1
                continue
            records[designation] = res
            got += res["ok"]
            failed += not res["ok"]
        if got:
            self.ctx.say(f"[gost_texts] пересборка индекса RAG: {reindex(self.ctx.say)}")
        notes = [f"пар «заменён → заменяющий»: {len(pairs)}; скачано текстов {got}, не найдено {failed}, оставлено прежних {kept}"]
        notes += [f"{k}: {'; '.join(r.get('errors', [])[:3])}" for k, r in records.items() if not r.get("ok")][:15]
        return Fetched(records, MEGANORM, now_iso()[:10], {"pairs": len(pairs)}, notes)

    def line_added(self, key, rec):
        if rec.get("ok"):
            return f"{key}: текст получен ({rec.get('via_title')}, {rec.get('characters')} знаков, пунктов {rec.get('clauses')})"
        return f"{key}: текст не найден ({'; '.join(rec.get('errors', [])[:2])})"


def reindex(say=print) -> str:
    """Индекс BM25 по корпусу и сохранённым текстам; векторы дополняются, если модель эмбеддингов доступна."""
    from .. import rag
    n = rag.build()
    msg = f"фрагментов {n}"
    try:
        from ..rag import models
        if os.environ.get("TZCHECK_RAG_VECTORS_UPDATE", "auto") != "0" and models.vectors_exist():
            added = models.refresh(models.DEFAULT_MODEL)
            msg += f", векторы дополнены: {added} новых фрагментов"
    except Exception as e:
        msg += f"; векторы не пересобраны ({type(e).__name__}: {str(e)[:120]}), гибрид до пересборки работает как BM25"
    return msg


def fetch_one(designation: str, offline: bool = False, browser: bool = True, replaces: str | None = None,
              say=print) -> dict:
    """Скачать текст одного обозначения по запросу и записать его в norm_texts.json."""
    src = GostTextsSource(Context(offline=offline, browser=browser))
    records = src.load()
    key = refdata.norm_designation(designation)
    if replaces is None:
        replaces = next((old for old, new in replacement_pairs(set(records) | corpus_designations()) if new == key), None)
    res = Fetcher(src.http, browser, say).fetch(key, "request" if replaces is None else "new", replaces)
    records[key] = res
    write_atomic(src.path, src.serialize(records))
    if res["ok"]:
        say(f"индекс: {reindex(say)}")
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m tzcheck.updater.gost_texts", description="Скачать текст редакции стандарта")
    ap.add_argument("designation", nargs="+", help="обозначения, например «ГОСТ 34441-2024»")
    ap.add_argument("--offline", action="store_true", help="разобрать сохранённые ответы без сети")
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args(argv)
    bad = 0
    for d in a.designation:
        res = fetch_one(d, a.offline, not a.no_browser)
        print(json.dumps({k: v for k, v in res.items() if k != "sha256"}, ensure_ascii=False))
        bad += not res["ok"]
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
