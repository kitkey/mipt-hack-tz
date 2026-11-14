import gzip
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from collections import Counter
from datetime import datetime

import lxml.html

from .base import Fetched, Source, SourceError
from .net import FetchError, now_iso

ROSSTAT = "https://rosstat.gov.ru"
ROSSTAT_PAGE = "https://rosstat.gov.ru/classification"
ROSSTAT_ARCHIVE = "https://rosstat.gov.ru/storage/mediabank/OKPD2.rar"
CLASSIF_XLSX = "https://classifikators.ru/assets/downloads/okpd/okpd.xlsx"
PROTECT_CARD = "https://protect.gost.ru/classificators/details/d62a528e-5761-40c2-a67b-2966a9d0911c"
MIRROR_AFTER_DAYS = int(os.environ.get("TZCHECK_OKPD2_MIRROR_AFTER_DAYS", "90"))
CODE_RE = re.compile(r"^\d{2}(?:\.\d(?:\d(?:\.\d(?:\d(?:\.\d{3})?)?)?)?)?$")
SECTIONS = [("A", 1, 3), ("B", 5, 9), ("C", 10, 33), ("D", 35, 35), ("E", 36, 39), ("F", 41, 43), ("G", 45, 47), ("H", 49, 53),
            ("I", 55, 56), ("J", 58, 63), ("K", 64, 66), ("L", 68, 68), ("M", 69, 75), ("N", 77, 82), ("O", 84, 84), ("P", 85, 85),
            ("Q", 86, 88), ("R", 90, 93), ("S", 94, 96), ("T", 97, 98), ("U", 99, 99)]
MONTHS = {"января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6, "июля": 7, "августа": 8, "сентября": 9,
          "октября": 10, "ноября": 11, "декабря": 12}


def section(code: str) -> str | None:
    n = int(code[:2])
    return next((s for s, a, b in SECTIONS if a <= n <= b), None)


def code_key(code: str):
    return tuple(int(x) for x in code.split("."))


def clean_name(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").replace(" ", " ")).strip()


def clean_code(s: str) -> str:
    return re.sub(r"\s+", "", (s or "").replace(" ", "")).rstrip(".")


def build(pairs) -> dict:
    names = {}
    for c, n in pairs:
        c, n = clean_code(c), clean_name(n)
        if CODE_RE.match(c) and n and c not in names:
            names[c] = n
    records = {}
    for c in sorted(names, key=code_key):
        parent = None
        for cut in (8, 7, 5, 4, 2):
            if cut < len(c) and c[:cut] in names:
                parent = c[:cut]
                break
        records[c] = {"c": c, "n": names[c], "l": 0, "v": len(c.replace(".", "")), "p": parent or section(c)}
    kids = Counter(r["p"] for r in records.values())
    for r in records.values():
        r["l"] = 0 if kids[r["c"]] else 1
    return records


def docx_pairs(data: bytes) -> tuple[list, str | None]:
    from docx import Document
    doc = Document(io.BytesIO(data))
    pairs = []
    for t in doc.tables:
        for row in t.rows:
            texts = []
            for cell in row.cells:
                x = clean_name(cell.text)
                if not texts or texts[-1] != x:
                    texts.append(x)
            idx = next((i for i, x in enumerate(texts) if CODE_RE.match(clean_code(x))), None)
            if idx is None:
                continue
            name = next((x for x in texts[idx + 1:] if x and not CODE_RE.match(clean_code(x))), "")
            pairs.append((texts[idx], name))
    if not pairs:
        for p in doc.paragraphs:
            m = re.match(r"^\s*(\d{2}(?:\.[\d.]+)?)\s+(.+)$", p.text)
            if m:
                pairs.append((m.group(1), m.group(2)))
    modified = doc.core_properties.modified
    return pairs, modified.strftime("%Y-%m-%d") if modified else None


def extract(archive: bytes, folder: str) -> list[str]:
    path = os.path.join(folder, "okpd2_source.bin")
    with open(path, "wb") as f:
        f.write(archive)
    out = os.path.join(folder, "x")
    os.makedirs(out, exist_ok=True)
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            z.extractall(out)
    else:
        done = False
        for cmd in (["bsdtar", "-xf", path, "-C", out], ["7z", "x", "-y", f"-o{out}", path], ["7za", "x", "-y", f"-o{out}", path],
                    ["unrar", "x", "-o+", path, out + os.sep], ["unar", "-f", "-o", out, path]):
            if shutil.which(cmd[0]):
                r = subprocess.run(cmd, capture_output=True, timeout=300)
                if r.returncode == 0:
                    done = True
                    break
        if not done:
            try:
                import rarfile
                rarfile.RarFile(path).extractall(out)
                done = True
            except Exception:
                pass
        if not done:
            raise SourceError("не удалось распаковать RAR: нужен bsdtar (libarchive-tools), 7z или unrar")
    files = []
    for root, _, names in os.walk(out):
        files += [os.path.join(root, n) for n in names if n.lower().endswith(".docx")]
    return sorted(files)


def official_change(http) -> dict:
    try:
        html = http.get(PROTECT_CARD).text
    except FetchError as e:
        return {"error": str(e)}
    text = " ".join(lxml.html.fromstring(html).text_content().split())
    best = None
    for m in re.finditer(r"Изменение\s+(\d+)\s*/\s*(\d{4})(?:\s+ОКПД\s*2)?\s*(\d{2}\.\d{2}\.\d{4})?", text):
        k = (int(m.group(2)), int(m.group(1)))
        if best is None or k > best[0]:
            best = (k, m.group(3))
    if not best:
        return {"error": "на карточке ОК 034-2014 не найдено ни одного «Изменение N/YYYY»"}
    (year, num), date = best
    return {"number": f"{num}/{year}", "date": date, "url": PROTECT_CARD}


def _div_text(node, cls: str) -> str:
    found = node.xpath(f".//div[contains(@class,'{cls}')]")
    return " ".join(found[0].text_content().split()) if found else ""


LOOKALIKE = str.maketrans("abcehkmoptxy", "ввсенкмортху")


def typo_norm(s) -> str:
    s = str(s or "").lower().replace("ё", "е").translate(LOOKALIKE)
    s = re.sub(r"\[\*(\w+)\]", r"\1", s)
    s = re.sub(r"[‐-―−-]", "-", s)
    s = re.sub(r"[«»“”„\"']", '"', s)
    return re.sub(r"\s+", "", s)


def change_tuple(s: str | None):
    m = re.search(r"(\d+)\s*/\s*(\d{4})", s or "")
    return (int(m.group(2)), int(m.group(1))) if m else None


class Okpd2Source(Source):
    name = "okpd2"
    title = "ОКПД 2 (ОК 034-2014)"
    filename = "okpd2.json.gz"
    compare_fields = ("n", "l")
    field_names = {"n": "наименование", "l": "конечный код"}
    min_records = 20000
    drop_guard = 0.9

    def from_file(self, data):
        return {x["c"]: x for x in data} if isinstance(data, list) else {}

    def to_file(self, records):
        return [records[c] for c in sorted(records, key=code_key)]

    def serialize(self, records):
        raw = json.dumps(self.to_file(records), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return gzip.compress(raw, mtime=0)

    def keep_meta_keys(self):
        return {"classifier", "classifier_name", "classifier_full_name"}

    def chain(self):
        return [("rosstat", "Росстат, архив ОКПД 2 (docx в RAR)", self._via_rosstat),
                ("classifikators", "classifikators.ru, okpd.xlsx (неофициальная копия)", self._via_classifikators)]

    def _meta(self, base: dict) -> dict:
        off = official_change(self.http)
        meta = {"classifier": "ОК 034-2014", "classifier_name": "ОКПД2",
                "classifier_full_name": "Общероссийский классификатор продукции по видам экономической деятельности",
                "record_format": "{c,n,l,v,p}", "latest_official_change": off}
        meta.update(base)
        own, last = change_tuple(meta.get("change_no")), change_tuple(off.get("number"))
        if own and last and last > own:
            meta["lag_warning"] = (f"в источнике изменения по {meta['change_no']}, а официально вышло {off['number']}"
                                   + (f" от {off['date']}" if off.get("date") else "") + ": коды из новых изменений могут отсутствовать")
        return meta

    def _done(self, records, url, version, meta, notes, unchanged=False) -> Fetched:
        meta = self._meta(meta)
        off = meta["latest_official_change"]
        if off.get("number"):
            notes.append(f"последнее официальное изменение ОКПД 2 на protect.gost.ru: {off['number']}" + (f" от {off['date']}" if off.get("date") else ""))
        if meta.get("lag_warning"):
            notes.append(meta["lag_warning"])
        return Fetched(records, url, version, meta, notes, unchanged)

    def _via_rosstat(self, prev: dict) -> Fetched:
        page = self.http.get(ROSSTAT_PAGE)
        doc = lxml.html.fromstring(page.content)
        link, title, info = None, "", ""
        for a in doc.xpath("//a[@href]"):
            href = a.get("href")
            if re.search(r"OKPD2[^/]*\.(?:rar|zip)$", href, re.I):
                link = href if href.startswith("http") else ROSSTAT + href
                item = a.xpath("ancestor::div[contains(@class,'document-list__item')][1]")
                if item:
                    title, info = _div_text(item[0], "item-title"), _div_text(item[0], "item-info")
                break
        link = link or ROSSTAT_ARCHIVE
        rng = re.search(r"по\s+(\d+\s*/\s*\d{4})", title)
        r = self.http.get(link, conditional=True)
        data = r.content
        if not (data[:4] == b"Rar!" or data[:2] == b"PK"):
            raise SourceError(f"по ссылке {link} пришёл не архив ({len(data)} байт, начало {data[:15]!r})")
        prev_meta = self.load_meta()
        sha = r.sha256
        meta = {"source_type": "rosstat_docx_archive", "source_page": ROSSTAT_PAGE, "source_title": title, "source_info": info,
                "source_sha256": sha, "source_size_bytes": len(data), "change_no": rng.group(1).replace(" ", "") if rng else None,
                "last_modified": r.headers.get("last-modified"), "etag": r.headers.get("etag")}
        if prev and prev_meta.get("source_sha256") == sha:
            meta.update({k: prev_meta.get(k) for k in ("source_date", "version", "version_label", "provenance", "source_files")})
            return self._done(prev, link, prev_meta.get("version"), meta, ["архив Росстата не изменился (тот же sha256)"], True)
        with tempfile.TemporaryDirectory() as tmp:
            files = extract(data, tmp)
            if not files:
                raise SourceError("в архиве нет файлов docx")
            pairs, dates = [], []
            for path in files:
                with open(path, "rb") as f:
                    p, d = docx_pairs(f.read())
                pairs += p
                if d:
                    dates.append(d)
            names = [os.path.basename(x) for x in files]
        records = build(pairs)
        date = max(dates) if dates else datetime.now().strftime("%Y-%m-%d")
        meta.update({"source_files": names, "source_date": date, "version": date, "provenance": f"Росстат · архив {os.path.basename(link)} от {date}",
                     "version_label": f"ОК 034-2014, редакция Росстата от {date}" + (f", изменения по {meta['change_no']}" if meta["change_no"] else "")})
        return self._done(records, link, date, meta, [f"файлов docx: {len(names)}, строк с кодами: {len(pairs)}"])

    def _via_classifikators(self, prev: dict) -> Fetched:
        from openpyxl import load_workbook
        prev_meta = self.load_meta()
        if prev and prev_meta.get("source_type") == "rosstat_docx_archive":
            try:
                age = (datetime.now() - datetime.strptime(prev_meta.get("source_date") or "", "%Y-%m-%d")).days
            except ValueError:
                age = MIRROR_AFTER_DAYS
            if age < MIRROR_AFTER_DAYS:
                raise SourceError(f"текущий файл взят у Росстата (редакция от {prev_meta['source_date']}), неофициальная копия "
                                  f"заменит его, только когда ему будет больше {MIRROR_AFTER_DAYS} дней")
        r = self.http.get(CLASSIF_XLSX, conditional=True)
        sha = r.sha256
        if prev and prev_meta.get("source_sha256") == sha:
            meta = {k: prev_meta.get(k) for k in ("source_type", "source_page", "source_sha256", "change_no", "source_date", "version",
                                                  "version_label", "provenance", "declared_count")}
            return self._done(prev, CLASSIF_XLSX, prev_meta.get("version"), meta, ["файл classifikators.ru не изменился"], True)
        wb = load_workbook(io.BytesIO(r.content), read_only=True, data_only=True)
        ws = wb.worksheets[0]
        head, pairs, code_col, name_col = [], [], None, None
        for row in ws.iter_rows(values_only=True):
            cells = ["" if v is None else str(v) for v in row]
            if code_col is None:
                low = [c.strip().lower() for c in cells]
                if "код" in low and ("название" in low or "наименование" in low):
                    code_col = low.index("код")
                    name_col = low.index("название") if "название" in low else low.index("наименование")
                else:
                    head.append(" ".join(c for c in cells if c.strip()))
                continue
            if len(cells) > max(code_col, name_col):
                pairs.append((cells[code_col], cells[name_col]))
        wb.close()
        if code_col is None:
            raise SourceError("в okpd.xlsx не найдена шапка с колонками «Код» и «Название»")
        header = " ".join(head)
        ch = re.search(r"изменением\s*№\s*(\d+\s*/\s*\d{4})", header)
        intro = re.search(r"Введено\s+(\d{2})\.(\d{2})\.(\d{4})", header)
        total = re.search(r"Всего записей\s+(\d+)", header)
        records = build(pairs)
        date = f"{intro[3]}-{intro[2]}-{intro[1]}" if intro else datetime.now().strftime("%Y-%m-%d")
        change = ch.group(1).replace(" ", "") if ch else None
        meta = {"source_type": "classifikators_xlsx", "source_page": "https://classifikators.ru/okpd", "source_sha256": sha,
                "source_header": header[:400], "change_no": change, "source_date": date, "version": date,
                "declared_count": int(total.group(1)) if total else None,
                "provenance": f"classifikators.ru · okpd.xlsx, изменение {change or '?'}, введено {date}",
                "version_label": f"ОК 034-2014 с изменением {change or '?'} (неофициальная копия classifikators.ru от {date})"}
        notes = [f"строк с кодами: {len(pairs)}, записей после разбора: {len(records)}",
                 "classifikators.ru — справочная копия, не официальный источник"]
        return self._done(records, CLASSIF_XLSX, date, meta, notes)

    def same(self, field, a, b):
        if field == "n":
            return typo_norm(a) == typo_norm(b)
        return a == b

    def line_added(self, key, rec):
        return f"новый код {key} «{rec['n'][:110]}»"

    def line_removed(self, key, rec):
        return f"код {key} «{rec['n'][:110]}» исключён"

    def line_changed(self, key, fields, old, new):
        if "n" in fields:
            return f"код {key}: наименование «{old['n'][:80]}» → «{new['n'][:80]}»"
        return f"код {key}: " + ("стал конечным" if new.get("l") else "получил подкоды")
