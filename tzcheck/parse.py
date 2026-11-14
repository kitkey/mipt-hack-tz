import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field, asdict

import docx
from docx.document import Document as DocxDocument
from docx.table import Table
from docx.text.paragraph import Paragraph

HEADING_RE = re.compile(r"^\s*(\d{1,2}(?:\.\d{1,2}){0,3})\.?\s+[А-ЯЁA-Z]")
OKPD_RE = re.compile(r"\b\d{2}\.\d{2}(?:\.\d{1,2}){0,2}(?:\.\d{3})?\b")


@dataclass
class Block:
    id: str
    kind: str
    text: str
    section: str = ""
    table: int | None = None
    row: int | None = None
    cells: list[str] = field(default_factory=list)
    page: int | None = None
    source: str = ""
    automatic_numbering: bool = False

    def location(self) -> str:
        where = [f"файл «{self.source}»"] if self.source else []
        if self.section:
            where.append(f"раздел «{self.section[:80]}»")
        if self.kind == "row":
            where.append(f"таблица {self.table + 1}, строка {self.row + 1}")
        elif self.kind != "heading":
            where.append(f"абзац {self.id.rsplit('_', 1)[-1].lstrip('ph')}")
        if self.page:
            where.append(f"стр. {self.page}")
        return ", ".join(where)


@dataclass
class ParsedDoc:
    name: str
    blocks: list[Block]
    fields: dict
    warnings: list[str]
    source_format: str

    def to_dict(self):
        return {"name": self.name, "source_format": self.source_format, "fields": self.fields,
                "warnings": self.warnings, "blocks": [asdict(b) for b in self.blocks]}

    def block(self, bid: str) -> Block | None:
        return next((b for b in self.blocks if b.id == bid), None)

    def text(self) -> str:
        return "\n".join(b.text for b in self.blocks)


class ParseError(Exception):
    pass


def _iter_body(doc: DocxDocument):
    for child in doc.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            yield Paragraph(child, doc)
        elif tag == "tbl":
            yield Table(child, doc)


def _row_cells(row) -> list[str]:
    out = []
    prev_tc = None
    for cell in row.cells:
        if cell._tc is prev_tc:
            continue
        prev_tc = cell._tc
        text = " ".join(cell.text.split())
        if out and text == out[-1]:
            continue
        out.append(text)
    return out


class _Numbering:
    def __init__(self):
        self.top = 0

    def accept(self, text: str) -> bool:
        m = HEADING_RE.match(text)
        if not m:
            return False
        parts = [int(x) for x in m.group(1).split(".")]
        if len(parts) == 1:
            if parts[0] == self.top + 1 or (self.top == 0 and parts[0] <= 2):
                self.top = parts[0]
                return True
            return False
        return parts[0] == self.top


def _is_heading(p: Paragraph, text: str, num: "_Numbering") -> bool:
    style = (p.style.name or "").lower() if p.style is not None else ""
    if style.startswith("heading") or style.startswith("заголовок") or style == "title":
        return True
    if len(text) > 160:
        return False
    if HEADING_RE.match(text):
        return len(text) < 110 and num.accept(text)
    runs = [r for r in p.runs if r.text.strip()]
    return bool(runs) and all(r.bold for r in runs) and len(text) < 120


def parse_docx(path: str) -> ParsedDoc:
    try:
        d = docx.Document(path)
    except Exception as e:
        raise ParseError("файл повреждён или это не docx (не открывается как документ Word)") from e
    blocks: list[Block] = []
    section = ""
    pi = ti = 0
    num = _Numbering()
    for item in _iter_body(d):
        if isinstance(item, Paragraph):
            text = " ".join(item.text.split())
            if not text:
                continue
            pi += 1
            if _is_heading(item, text, num):
                section = text.rstrip(":")
                blocks.append(Block(f"h{pi}", "heading", text, section))
            else:
                blocks.append(Block(f"p{pi}", "para", text, section))
            blocks[-1].automatic_numbering = bool(item._p.xpath("./w:pPr/w:numPr"))
        else:
            for ri, row in enumerate(item.rows):
                cells = _row_cells(row)
                text = " | ".join(c for c in cells if c)
                if not text:
                    continue
                blocks.append(Block(f"t{ti + 1}r{ri + 1}", "row", text, section, ti, ri, cells))
            ti += 1
    if not blocks:
        raise ParseError("в документе нет текста")
    return ParsedDoc(os.path.basename(path), blocks, extract_fields(blocks), [], "docx")


def _convert_with_soffice(path: str, fmt: str) -> str:
    exe = shutil.which("soffice") or shutil.which("libreoffice")
    if not exe:
        raise ParseError("для .doc нужен LibreOffice (soffice) в PATH; в Docker-образе он есть")
    out_dir = tempfile.mkdtemp(prefix="tzcheck_")
    try:
        subprocess.run([exe, "--headless", "--convert-to", fmt, "--outdir", out_dir, path],
                       check=True, capture_output=True, timeout=int(os.environ.get("TZCHECK_SOFFICE_TIMEOUT", "180")))
    except subprocess.TimeoutExpired:
        raise ParseError("LibreOffice не успел сконвертировать файл (таймаут); сохраните документ в .docx и загрузите снова")
    except subprocess.CalledProcessError:
        raise ParseError("LibreOffice не смог сконвертировать файл: файл повреждён или формат не поддерживается")
    base = os.path.splitext(os.path.basename(path))[0] + "." + fmt
    out = os.path.join(out_dir, base)
    if not os.path.exists(out):
        raise ParseError("LibreOffice не смог сконвертировать файл")
    return out


SHORT_WORDS = {"в", "к", "с", "и", "о", "у", "а", "я", "по", "на", "за", "до", "от", "из", "не", "ни", "но", "же", "ли", "во", "со", "ко",
               "об", "то", "мм", "см", "м", "кг", "г", "шт", "л", "т", "ч", "с", "мин", "вт", "гц", "дб"}
WORD_RE = re.compile(r"[а-яёa-z]+", re.I)
LIST_START_RE = re.compile(r"^(?:\d{1,2}(?:\.\d{1,2}){0,3}[.)]\s|[-–—•●▪◦]\s?|[а-яa-z]\)\s)")
_OKPD_WORDS: set | None = None


def _okpd_words() -> set:
    global _OKPD_WORDS
    if _OKPD_WORDS is None:
        try:
            from . import okpd
            _OKPD_WORDS = {w.lower() for n in okpd.names() for w in WORD_RE.findall(n)}
        except Exception:
            _OKPD_WORDS = set()
    return _OKPD_WORDS


def _vocab(pdf) -> set:
    vocab = set()
    for page in pdf:
        lines: dict = {}
        for w in page.get_text("words"):
            lines.setdefault((w[5], w[6]), []).append(w[4])
        for ws in lines.values():
            for w in ws[1:-1]:
                vocab.update(x.lower() for x in WORD_RE.findall(w))
    return vocab | _okpd_words()


_MORPH = None


def _known(word: str, vocab: set) -> bool:
    global _MORPH
    if word in vocab:
        return True
    if _MORPH is None:
        try:
            import pymorphy3
            _MORPH = pymorphy3.MorphAnalyzer()
        except Exception:
            _MORPH = False
    return bool(_MORPH) and _MORPH.word_is_known(word)


def _join_lines(lines: list[str], vocab: set = frozenset(), cell: bool = False, full: list[bool] | None = None) -> str:
    out = ""
    prev_full = False
    for i, line in enumerate(x.strip() for x in lines):
        was_full, prev_full = prev_full, bool(full and full[i])
        if not line:
            continue
        if not out:
            out = line
            continue
        tail = re.search(r"(?:^|[^а-яёa-z0-9])([а-яёa-z]+)(-?)$", out, re.I)
        head = re.match(r"([а-яёa-z]+)", line)
        if tail and head and head.group(1)[:1].islower():
            w, t = tail.group(1).lower(), head.group(1).lower()
            if tail.group(2):
                glued = _known(w + t, vocab) and not (_known(w, vocab) and _known(t, vocab))
                out = out[:-1] + line if glued else out + line
            elif not cell or not was_full:
                out += " " + line
            elif _known(w + t, vocab) or not _known(w, vocab):
                out += line
            else:
                out += " " + line
        elif out.endswith("-") and line[:1].isdigit():
            out += line
        elif cell and was_full and re.search(r"\d[\d.]*$", out) and re.match(r"^[\d.]+", line) and _code_split(out, line):
            out += line
        else:
            out += " " + line
    return " ".join(out.split())


CODE_FULL_RE = re.compile(r"\d{2}\.\d{2}\.\d{2}\.\d{3}|\d{2}\.\d{2}\.\d{2}")
CODE_PART_RE = re.compile(r"\d{2}(?:\.\d{1,3}){1,3}")


def _code_split(out: str, line: str) -> bool:
    last = re.search(r"[\d.]+$", out).group()
    first = re.match(r"^[\d.]+", line).group()
    if CODE_FULL_RE.fullmatch(last.rstrip(".")):
        return False
    return bool(CODE_PART_RE.fullmatch(last + first))


def _cell_text(page, rect, vocab: set) -> str:
    import pymupdf
    rect = pymupdf.Rect(rect)
    lines: dict = {}
    for w in page.get_text("words", clip=rect):
        if pymupdf.Rect(w[:4]).intersects(rect):
            lines.setdefault((w[5], w[6]), []).append(w)
    ordered = sorted(lines.values(), key=lambda ws: (round(min(x[1] for x in ws)), min(x[0] for x in ws)))
    if not ordered:
        return ""
    full = []
    for ws in ordered:
        x1 = max(x[2] for x in ws)
        chars = sum(len(x[4]) for x in ws) or 1
        charw = (x1 - min(x[0] for x in ws)) / chars
        full.append(rect.x1 - x1 < 1.5 * charw + 4)
    return _join_lines([" ".join(x[4] for x in sorted(ws, key=lambda x: x[0])) for ws in ordered], vocab, cell=True, full=full)


def _span_bold(span) -> bool:
    return bool(span["flags"] & 16) or "bold" in span["font"].lower()


def _page_lines(page, rects) -> list[tuple]:
    import pymupdf
    parts = []
    for b in page.get_text("dict")["blocks"]:
        if b.get("type") != 0:
            continue
        for ln in b["lines"]:
            spans = [sp for sp in ln["spans"] if sp["text"].strip()]
            if not spans:
                continue
            r = pymupdf.Rect(ln["bbox"])
            c = pymupdf.Point((r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2)
            if any(c in tr for tr in rects):
                continue
            parts.append((r.x0, r.y0, r.x1, r.y1, "".join(sp["text"] for sp in ln["spans"]).strip(), all(_span_bold(sp) for sp in spans)))
    parts.sort(key=lambda x: (x[1], x[0]))
    lines = []
    for x0, y0, x1, y1, text, bold in parts:
        if lines:
            L = lines[-1]
            overlap = min(L["y1"], y1) - max(L["y0"], y0)
            if overlap > 0.5 * min(L["y1"] - L["y0"], y1 - y0):
                L["parts"].append((x0, x1, text, bold))
                L["y0"], L["y1"] = min(L["y0"], y0), max(L["y1"], y1)
                continue
        lines.append({"y0": y0, "y1": y1, "parts": [(x0, x1, text, bold)]})
    out = []
    for L in lines:
        ps = sorted(L["parts"])
        text = ps[0][2]
        for prev, cur in zip(ps, ps[1:]):
            text += ("" if cur[0] - prev[1] < 1.0 else " ") + cur[2]
        out.append((" ".join(text.split()), ps[0][0], L["y0"], max(x[1] for x in ps), L["y1"], all(x[3] for x in ps)))
    return out


def _paragraphs(lines: list[tuple], vocab: set) -> list[tuple[str, bool, float]]:
    if not lines:
        return []
    left = min(x[1] for x in lines)
    right = max(x[3] for x in lines)
    width = max(right - left, 1)
    groups = [[lines[0]]]
    for prev, cur in zip(lines, lines[1:]):
        p_text, p_x0, p_y0, p_x1, p_y1, p_bold = prev
        c_text, c_x0, c_y0, c_x1, c_y1, c_bold = cur
        height = max(p_y1 - p_y0, 1)
        short = p_x1 < right - 0.15 * width
        indented = c_x0 > left + 8
        starts_new = c_text[:1].isupper() or c_text[:1].isdigit() or c_text[:1] in "«\"*"
        brk = (p_bold != c_bold
               or bool(LIST_START_RE.match(c_text))
               or c_y0 - p_y1 > 0.6 * height
               or p_text.endswith(";")
               or (short and starts_new)
               or (re.search(r"[.:!?]$", p_text) and indented and starts_new))
        if brk:
            groups.append([cur])
        else:
            groups[-1].append(cur)
    return [(_join_lines([x[0] for x in g], vocab), all(x[5] for x in g), g[0][2]) for g in groups]


def _row_cells_pdf(page, row, vocab: set) -> list[str]:
    return [_cell_text(page, c, vocab) if c else None for c in row.cells]


def _rows_fill_vmerge(page, table, vocab: set) -> list[list]:
    """Ячейка, объединённая по вертикали, повторяется в каждой строке, которую накрывает, как в разборе docx."""
    out, origin = [], {}
    carry: dict = {}
    for row in table.rows:
        texts = _row_cells_pdf(page, row, vocab)
        top = row.bbox[1]
        carry = {}
        for k, c in enumerate(row.cells):
            if c is not None:
                origin[k] = (c, texts[k])
            elif k in origin and origin[k][0][1] < top - 1 and origin[k][0][3] > top + 1:
                texts[k] = origin[k][1]
                if texts[k]:
                    carry[k] = texts[k]
        out.append(texts)
    _rows_fill_vmerge.carry = carry
    return out


def _apply_carry(rows: list, carry: dict) -> dict:
    """Объединённая ячейка, перешедшая через разрыв страницы: на новой странице её часть пустая."""
    carry = dict(carry)
    for cells, _ in rows:
        for k in list(carry):
            if k < len(cells) and not cells[k]:
                cells[k] = carry[k]
            else:
                carry.pop(k)
    return carry


def _compact(cells: list) -> list[str]:
    out = []
    for c in cells:
        if c is None:
            continue
        if out and c == out[-1] and c:
            continue
        out.append(c)
    while out and not out[-1]:
        out.pop()
    return out


def _continues(prev: list, cur: list) -> bool:
    if not prev or not cur or len(prev) != len(cur):
        return False
    if (cur[0] or "") == "":
        return any(cur)
    first = next((c for c in cur if c), "")
    last = next((c for c in reversed(prev) if c), "")
    return first[:1].islower() and not re.search(r"[.;:!?]$", last)


def _tables(page, **kw) -> list:
    try:
        return page.find_tables(use_layout=False, **kw).tables
    except TypeError:
        return page.find_tables(**kw).tables


def _find_tables(page) -> list:
    try:
        strict = _tables(page, strategy="lines_strict")
        if strict:
            return strict
        loose = _tables(page)
    except Exception:
        return []
    keep = []
    for t in loose:
        rows = [[c for c in r if c and c.strip()] for r in t.extract()]
        multi = sum(1 for r in rows if len(set(r)) >= 2)
        if rows and multi >= 0.5 * len(rows):
            keep.append(t)
    return keep


def parse_pdf(path: str) -> ParsedDoc:
    import pymupdf
    try:
        pdf = pymupdf.open(path)
    except Exception as e:
        raise ParseError("PDF повреждён и не открывается") from e
    if pdf.needs_pass:
        raise ParseError("PDF защищён паролем")
    vocab = _vocab(pdf)
    items: list = []
    warnings: list[str] = []
    tables: list = []
    open_table = None
    for pno, page in enumerate(pdf, 1):
        raw = page.get_text("text")
        if len(raw.strip()) < 30:
            layout = _ocr_page(page)
            if layout is None:
                warnings.append(f"стр. {pno}: нет текстового слоя, OCR недоступен (установите requirements-ocr.txt, TZCHECK_OCR=auto)")
                continue
            warnings.append(f"стр. {pno}: скан без текстового слоя, текст распознан OCR (PaddleOCR): цифры и коды сверить с оригиналом")
            lines, scanned = layout
            page_items = [(t["y"], "table", t) for t in scanned]
            page_items += [(y0, "text", (text, bold)) for text, bold, y0 in _paragraphs(lines, vocab)]
            for _, kind, obj in sorted(page_items, key=lambda x: x[0]):
                if kind == "text":
                    items.append(("text", obj[0], False, pno))
                else:
                    rows = [[_join_lines(c.split(chr(10)), vocab, cell=True, full=[True] * len(c.split(chr(10)))) for c in r]
                            for r in obj["rows"]]
                    t = {"cols": max(len(r) for r in rows), "rows": [(r, pno) for r in rows]}
                    tables.append(t)
                    items.append(("table", t, None, pno))
            open_table = None
            continue
        found = _find_tables(page)
        rects = [pymupdf.Rect(t.bbox) for t in found]
        page_items = [(t.bbox[1], "table", t) for t in found]
        for text, bold, y0 in _paragraphs(_page_lines(page, rects), vocab):
            page_items.append((y0, "text", (text, bold)))
        page_items.sort(key=lambda x: x[0])
        first = True
        for y, kind, obj in page_items:
            if kind == "text":
                text, bold = obj
                if re.fullmatch(r"(?:стр\.?\s*)?\d{1,3}(?:\s*из\s*\d{1,3})?", text, re.I):
                    continue
                prev = items[-1] if items else None
                if (first and prev and prev[0] == "text" and prev[3] == pno - 1 and prev[2] == bold
                        and not re.search(r"[.:;!?]$", prev[1]) and (text[:1].islower() or text[:1].isdigit())):
                    items[-1] = ("text", _join_lines([prev[1], text], vocab), bold, prev[3])
                else:
                    items.append(("text", text, bold, pno))
                open_table = None
                first = False
                continue
            first = False
            rows = [(c, pno) for c in _rows_fill_vmerge(page, obj, vocab)]
            page_carry = _rows_fill_vmerge.carry
            rows = [(c, p) for c, p in rows if any(c)]
            if not rows:
                continue
            if open_table is not None and y < page.rect.height * 0.25 and obj.col_count == open_table["cols"]:
                prev_cells = open_table["rows"][-1][0]
                if _continues(prev_cells, rows[0][0]):
                    merged = [_join_lines([a or "", b or ""], vocab, cell=True, full=[True, False]) if (a is not None or b is not None) else None
                              for a, b in zip(prev_cells, rows[0][0])]
                    open_table["rows"][-1] = (merged, open_table["rows"][-1][1])
                    rows = rows[1:]
                left = _apply_carry(rows, open_table.get("carry", {}))
                open_table["carry"] = {**left, **page_carry} if rows else open_table.get("carry", {})
                open_table["rows"] += rows
            else:
                open_table = {"cols": obj.col_count, "rows": rows, "carry": page_carry}
                tables.append(open_table)
                items.append(("table", open_table, None, pno))
    blocks: list[Block] = []
    section = ""
    num = _Numbering()
    pi = ti = 0
    for kind, obj, bold, pno in items:
        if kind == "text":
            pi += 1
            text = obj
            heading = len(text) < 160 and ((HEADING_RE.match(text) and len(text) < 110 and num.accept(text))
                                           or (bold and len(text) < 120 and not LIST_START_RE.match(text)))
            if heading:
                section = text.rstrip(":")
                blocks.append(Block(f"h{pi}", "heading", text, section, page=pno))
            else:
                blocks.append(Block(f"p{pi}", "para", text, section, page=pno))
            continue
        for ri, (cells, rpage) in enumerate(obj["rows"]):
            cells = _compact(cells)
            text = " | ".join(c for c in cells if c)
            if text:
                blocks.append(Block(f"t{ti + 1}r{ri + 1}", "row", text, section, ti, ri, cells, page=rpage))
        ti += 1
    if not blocks:
        from . import ocr
        hint = "" if ocr.enabled() else ": похоже на скан без текстового слоя, распознавание включается установкой PaddleOCR (requirements-ocr.txt, в Docker WITH_OCR=1)"
        raise ParseError("в PDF не найден текст" + hint)
    return ParsedDoc(os.path.basename(path), blocks, extract_fields(blocks), warnings, "pdf")


def _ocr_page(page) -> tuple | None:
    """Скан страницы -> (строки вне таблиц, таблицы) через OCR; None, если OCR выключен или упал."""
    from . import ocr
    if not ocr.enabled():
        return None
    try:
        return ocr.ocr_pdf_layout(page)
    except Exception:
        return None


def parse(path: str) -> ParsedDoc:
    if not os.path.exists(path):
        raise ParseError("файл не найден")
    if os.path.getsize(path) == 0:
        raise ParseError("файл пустой")
    ext = os.path.splitext(path)[1].lower()
    if ext == ".docx":
        return parse_docx(path)
    if ext in (".doc", ".rtf", ".odt"):
        doc = parse_docx(_convert_with_soffice(path, "docx"))
        doc.name, doc.source_format = os.path.basename(path), ext.lstrip(".")
        return doc
    if ext == ".pdf":
        return parse_pdf(path)
    from .ocr import IMAGE_EXT
    if ext in IMAGE_EXT:
        import pymupdf
        try:
            pdf_bytes = pymupdf.open(path).convert_to_pdf()
        except Exception as e:
            raise ParseError("картинка не открывается") from e
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            tmp_pdf = os.path.join(tmp, "scan.pdf")
            with open(tmp_pdf, "wb") as fh:
                fh.write(pdf_bytes)
            doc = parse_pdf(tmp_pdf)
        doc.name, doc.source_format = os.path.basename(path), ext.lstrip(".")
        return doc
    if ext == ".xlsx":
        try:
            import openpyxl
            book = openpyxl.load_workbook(path, read_only=True, data_only=True)
            blocks = []
            for ti, sheet in enumerate(book.worksheets):
                for ri, row in enumerate(sheet.iter_rows(values_only=True)):
                    cells = [str(v).strip() if v is not None else "" for v in row]
                    if any(cells):
                        blocks.append(Block(f"t{ti+1}r{ri+1}", "row", " | ".join(c for c in cells if c), sheet.title, ti, ri, cells))
            book.close()
        except Exception as e:
            raise ParseError("XLSX повреждён и не открывается") from e
        if not blocks:
            raise ParseError("в XLSX нет данных")
        return ParsedDoc(os.path.basename(path), blocks, extract_fields(blocks), [], "xlsx")
    if ext in (".txt", ".md"):
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = [" ".join(x.split()) for x in f]
        blocks, section = [], ""
        for i, line in enumerate(x for x in lines if x):
            if HEADING_RE.match(line) and len(line) < 120:
                section = line
                blocks.append(Block(f"h{i + 1}", "heading", line, section))
            else:
                blocks.append(Block(f"p{i + 1}", "para", line, section))
        return ParsedDoc(os.path.basename(path), blocks, extract_fields(blocks), [], ext.lstrip("."))
    raise ParseError(f"формат {ext or 'без расширения'} не поддерживается: нужен docx, doc, pdf или txt")


FIELD_PATTERNS = {
    "предмет закупки": re.compile(r"(?:предмет закупки|на (?:поставку|выполнение|оказание)[^.:]{3,160})", re.I),
    "срок": re.compile(r"срок[^.]{0,40}(?:поставки|выполнения|оказания)[^.]{0,160}", re.I),
    "место": re.compile(r"(?:место|адрес)[^.]{0,30}(?:поставки|выполнения|оказания)?[^.]{0,160}", re.I),
    "гарантия": re.compile(r"гаранти[^.]{0,200}", re.I),
}


def extract_fields(blocks: list[Block]) -> dict:
    fields: dict = {"okpd2": [], "заголовок": ""}
    for b in blocks[:6]:
        if len(b.text) > 15:
            fields["заголовок"] = (fields["заголовок"] + " " + b.text).strip()[:300]
    for b in blocks:
        near_code = re.search(r"окпд|код", b.text + " " + b.section, re.I)
        for code in OKPD_RE.findall(b.text):
            if code in fields["okpd2"]:
                continue
            if near_code or (b.kind == "row" and len(code) >= 8):
                fields["okpd2"].append(code)
        for key, rx in FIELD_PATTERNS.items():
            if key not in fields:
                m = rx.search(b.text)
                if m:
                    fields[key] = {"text": m.group(0)[:220], "block": b.id}
    for key in FIELD_PATTERNS:
        fields.setdefault(key, None)
    return fields


def parse_many(paths) -> ParsedDoc:
    """Parse a main document and its attachments, retaining stable per-file block IDs."""
    paths = [os.fspath(p) for p in paths]
    if not paths:
        raise ParseError("не переданы файлы")
    docs = [parse(p) for p in paths]
    blocks, warnings = [], []
    for index, doc in enumerate(docs):
        for block in doc.blocks:
            block.source = doc.name if len(docs) > 1 else ""
            if index:
                block.id = f"a{index}_{block.id}"
            blocks.append(block)
        warnings.extend(f"{doc.name}: {w}" for w in doc.warnings)
    return ParsedDoc(docs[0].name, blocks, extract_fields(blocks), warnings, docs[0].source_format)
