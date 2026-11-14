"""«Внести предлагаемые изменения»: копия исходного DOCX, где точные замены из замечаний (поле replacement) внесены
правками в режиме рецензирования (w:del / w:ins, автор «Проверка ТЗ»), а к каждой правке приложен комментарий с номером
замечания. Замечания без точной замены в этот файл не попадают: они остаются в файле с комментариями."""
import copy
import os
import re
import time
from difflib import SequenceMatcher

import docx
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

AUTHOR = "Проверка ТЗ"
INITIALS = "ТЗ"
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"


def _date() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _run_editable(r) -> bool:
    """Прогон, который можно резать: внутри только свойства и текст."""
    return all(c.tag in (qn("w:rPr"), qn("w:t"), qn("w:lastRenderedPageBreak")) for c in r)


def _run_text(r) -> str:
    return "".join(t.text or "" for t in r.iter(qn("w:t")))


def _segments(p):
    """Дети абзаца с их текстом в координатах исходного текста: удалённое (w:del) считается, вставленное (w:ins) нет."""
    out, pos = [], 0
    for el in p:
        if el.tag == qn("w:r"):
            txt = _run_text(el) if _run_editable(el) else "".join(
                (t.text or "") if t.tag == qn("w:t") else " " for t in el if t.tag in (qn("w:t"), qn("w:tab"), qn("w:br"), qn("w:cr")))
            kind = "run" if _run_editable(el) else "opaque"
        elif el.tag == qn("w:del"):
            txt, kind = "".join(t.text or "" for t in el.iter(qn("w:delText"))), "opaque"
        elif el.tag == qn("w:ins"):
            txt, kind = "", "opaque"
        else:
            txt, kind = "".join(t.text or "" for t in el.iter(qn("w:t"))), "opaque"
        out.append((el, pos, pos + len(txt), kind, txt))
        pos += len(txt)
    return out


def _find(full: str, quote: str) -> tuple[int, int] | None:
    """Цитата в тексте абзаца без учёта различий в пробелах и регистре; позиции в исходном тексте."""
    idx, norm = [], []
    prev_space = False
    for i, ch in enumerate(full):
        if ch.isspace():
            if prev_space or not norm:
                continue
            norm.append(" ")
            idx.append(i)
            prev_space = True
        else:
            norm.append(ch.lower())
            idx.append(i)
            prev_space = False
    text = "".join(norm)
    q = " ".join(quote.split()).lower()
    if not q:
        return None
    k = text.find(q)
    if k < 0:
        return None
    return idx[k], idx[k + len(q) - 1] + 1


def _set_text(r, text: str):
    for t in list(r.iter(qn("w:t"))):
        t.getparent().remove(t)
    t = OxmlElement("w:t")
    t.text = text
    t.set(XML_SPACE, "preserve")
    r.append(t)


def _split(p, pos: int):
    """Разрезать прогон так, чтобы pos пришёлся на границу прогонов."""
    for el, a, b, kind, txt in _segments(p):
        if kind == "run" and a < pos < b:
            second = copy.deepcopy(el)
            for x in second.findall(qn("w:lastRenderedPageBreak")):
                second.remove(x)
            _set_text(el, txt[:pos - a])
            _set_text(second, txt[pos - a:])
            el.addnext(second)
            return


def _rpr_near(p, pos: int):
    segs = [s for s in _segments(p) if s[3] == "run"]
    for el, a, b, _, _ in segs:
        if a <= pos < b or (pos == b and el is segs[-1][0]):
            rpr = el.find(qn("w:rPr"))
            return copy.deepcopy(rpr) if rpr is not None else None
    if segs:
        rpr = segs[-1][0].find(qn("w:rPr"))
        return copy.deepcopy(rpr) if rpr is not None else None
    return None


class _Ids:
    def __init__(self, d):
        ids = [int(v) for el in d.element.body.iter() for k, v in el.attrib.items() if k == qn("w:id") and v.lstrip("-").isdigit()]
        self.n = max(ids + [0]) + 1000

    def next(self) -> str:
        self.n += 1
        return str(self.n)


def _ins_el(text: str, rpr, ids: _Ids, date: str):
    ins = OxmlElement("w:ins")
    ins.set(qn("w:id"), ids.next())
    ins.set(qn("w:author"), AUTHOR)
    ins.set(qn("w:date"), date)
    r = OxmlElement("w:r")
    if rpr is not None:
        r.append(rpr)
    t = OxmlElement("w:t")
    t.text = text
    t.set(XML_SPACE, "preserve")
    r.append(t)
    ins.append(r)
    return ins


def _wrap_del(p, a: int, b: int, ids: _Ids, date: str):
    """Прогоны в [a, b) переносятся в w:del, текст становится w:delText. Возвращает элемент w:del."""
    _split(p, a)
    _split(p, b)
    runs = [el for el, x, y, kind, _ in _segments(p) if kind == "run" and x >= a and y <= b and y > x]
    if not runs:
        return None
    dl = OxmlElement("w:del")
    dl.set(qn("w:id"), ids.next())
    dl.set(qn("w:author"), AUTHOR)
    dl.set(qn("w:date"), date)
    runs[0].addprevious(dl)
    for r in runs:
        for t in r.findall(qn("w:t")):
            dt = OxmlElement("w:delText")
            dt.text = t.text
            dt.set(XML_SPACE, "preserve")
            t.addprevious(dt)
            r.remove(t)
        dl.append(r)
    return dl


def _ops(old: str, new: str):
    """Пословные правки: (начало, конец в old, новый текст); равные куски не трогаются."""
    a, b = re.findall(r"\s+|[^\s]+", old), re.findall(r"\s+|[^\s]+", new)
    pos = [0]
    for tok in a:
        pos.append(pos[-1] + len(tok))
    out = []
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag != "equal":
            out.append((pos[i1], pos[i2], "".join(b[j1:j2])))
    return out


def apply_to_paragraph(p, quote: str, replacement: str, ids: _Ids, date: str) -> list | None:
    """Внести одну замену в абзац; None, если цитату не найти или она задевает нередактируемый текст."""
    segs = _segments(p)
    full = "".join(s[4] for s in segs)
    span = _find(full, quote)
    if span is None:
        return None
    s, e = span
    if any(kind != "run" and y > x and x < e and y > s for _, x, y, kind, _ in segs):
        return None
    marks = []
    for a, b, text in reversed(_ops(full[s:e], replacement)):
        a, b = a + s, b + s
        rpr = _rpr_near(p, a)
        if b > a:
            dl = _wrap_del(p, a, b, ids, date)
            if dl is None:
                continue
            marks.append(dl)
            if text:
                ins = _ins_el(text, rpr, ids, date)
                dl.addnext(ins)
                marks.append(ins)
        elif text:
            _split(p, a)
            ins = _ins_el(text, rpr, ids, date)
            after = next((el for el, x, y, kind, _ in _segments(p) if kind == "run" and x == a and y > x), None)
            if after is not None:
                after.addprevious(ins)
            else:
                before = next((el for el, x, y, kind, _ in reversed(_segments(p)) if y == a), None)
                if before is None:
                    continue
                before.addnext(ins)
            marks.append(ins)
    return marks or None


def _comment(d, p, marks: list, text: str):
    """Комментарий на группу правок: границы диапазона вокруг первой и последней правки абзаца."""
    order = {id(el): i for i, el in enumerate(p)}
    marks = sorted(marks, key=lambda el: order.get(id(el), 0))
    c = d.comments.add_comment(text=text, author=AUTHOR, initials=INITIALS)
    start = OxmlElement("w:commentRangeStart")
    start.set(qn("w:id"), str(c.comment_id))
    marks[0].addprevious(start)
    end = OxmlElement("w:commentRangeEnd")
    end.set(qn("w:id"), str(c.comment_id))
    marks[-1].addnext(end)
    ref_run = OxmlElement("w:r")
    ref = OxmlElement("w:commentReference")
    ref.set(qn("w:id"), str(c.comment_id))
    ref_run.append(ref)
    end.addnext(ref_run)


def _targets(d):
    paras, rows = {}, {}
    pi, ti = 0, 0
    for child in d.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            p = docx.text.paragraph.Paragraph(child, d)
            if " ".join(p.text.split()):
                pi += 1
                paras[pi] = p
        elif tag == "tbl":
            t = docx.table.Table(child, d)
            for ri, row in enumerate(t.rows):
                rows[(ti + 1, ri + 1)] = row
            ti += 1
    return paras, rows


def _candidates(bid: str, paras: dict, rows: dict) -> list:
    if bid[:1] in "hp" and bid[1:].isdigit():
        p = paras.get(int(bid[1:]))
        return [p] if p is not None else []
    if bid.startswith("t") and "r" in bid:
        a, b = bid[1:].split("r", 1)
        if a.isdigit() and b.isdigit():
            row = rows.get((int(a), int(b)))
            if row is not None:
                seen, out = set(), []
                for cell in row.cells:
                    if id(cell._tc) in seen:
                        continue
                    seen.add(id(cell._tc))
                    out.extend(cell.paragraphs)
                return out
    return []


def to_docx_revisions(result: dict, src_path: str, out_path: str) -> dict:
    """Записать out_path; вернуть {"applied": [id], "skipped": {id: причина}}. Файл пишется, только если внесена хотя бы одна правка."""
    report = {"applied": [], "skipped": {}}
    if not src_path.lower().endswith(".docx") or result.get("status") not in ("ok", "incomplete"):
        return report
    paths = result.get("_paths", [src_path])
    try:
        file_index = [os.path.abspath(p) for p in paths].index(os.path.abspath(src_path))
    except ValueError:
        return report
    prefix = f"a{file_index}_" if file_index else ""
    d = docx.Document(src_path)
    paras, rows = _targets(d)
    ids, date = _Ids(d), _date()
    for f in result.get("findings", []):
        if not f.get("replacement") or f.get("section", "main") != "main":
            continue
        bids = [f["block_id"]] + list(f.get("also_in") or [])
        for n, bid in enumerate(bids):
            if prefix:
                if not bid.startswith(prefix):
                    continue
                bid = bid[len(prefix):]
            elif bid.startswith("a"):
                continue
            done = False
            for p in _candidates(bid, paras, rows):
                marks = apply_to_paragraph(p._p, f["quote"], f["replacement"], ids, date)
                if marks:
                    text = f'[{f["id"]}] {f["category"]}. {f["explanation"]} Предлагаемая правка: «{f["replacement"]}».'
                    _comment(d, p._p, marks, text if n == 0 else f'[{f["id"]}] то же исправление. ' + text)
                    done = True
                    break
            if done:
                report["applied"].append(f["id"] if n == 0 else f'{f["id"]}:{bid}')
            elif n == 0:
                report["skipped"][f["id"]] = "цитата не найдена в абзаце DOCX или задевает поле, ссылку или уже внесённую правку"
    if report["applied"]:
        d.save(out_path)
    return report


def write(result: dict, src_path: str, out_dir: str) -> dict:
    """<base>.revisions.docx для основного файла и attachment_<i>.revisions.docx для приложений DOCX."""
    out = {}
    base = os.path.splitext(os.path.basename(src_path))[0]
    path = os.path.join(out_dir, base + ".revisions.docx")
    rep = to_docx_revisions(result, src_path, path)
    if rep["applied"]:
        out["main"] = dict(rep, path=path)
    elif rep["skipped"]:
        out["main"] = dict(rep, path=None)
    for i, att in enumerate(result.get("_paths", [])[1:], 1):
        if att.lower().endswith(".docx"):
            p = os.path.join(out_dir, f"attachment_{i}.revisions.docx")
            r = to_docx_revisions(result, att, p)
            if r["applied"]:
                out[f"attachment_{i}"] = dict(r, path=p)
    return out
