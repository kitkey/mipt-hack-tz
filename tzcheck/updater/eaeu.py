import hashlib
import json
import re
from datetime import datetime, timedelta

import lxml.etree
import lxml.html

from .. import refdata
from .base import Fetched, Source, SourceError
from .net import FetchError, now_iso

PROTECT = "https://protect.gost.ru"
EEC = "https://eec.eaeunion.org"
EEC_GENERAL = EEC + "/comission/department/deptexreg/tr/TR_general.php"
EAEU_API = "https://docs.eaeunion.org/api/documents.php"
TR_RE = re.compile(r"ТР\s*(ТС|ЕАЭС)\s*(\d{3})/(\d{4})")
ACT_FILTER = re.compile(r"перечн\w* стандарт|ТР ТС|ТР ЕАЭС|технич\w+ регламент|межгосударственн\w+ стандарт", re.I)
DESIGNATION_RE = re.compile(r"(?<![А-ЯЁA-Z])((?:ГОСТ Р|ГОСТ IEC|ГОСТ ISO|ГОСТ EN|ГОСТ|СТ РК|СТБ|ПНСТ|СП|ISO|IEC|EN)(?![А-ЯЁA-Z])[\s\w./:-]{0,40}?\d[\d.:/-]*\d)", re.I)


def _text(el) -> str:
    return " ".join(el.text_content().split()) if el is not None else ""


def _hash(obj) -> str:
    return hashlib.sha256(json.dumps(obj, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def tr_code(s: str) -> str | None:
    m = TR_RE.search(s or "")
    return f"ТР {m.group(1)} {m.group(2)}/{m.group(3)}" if m else None


def parse_index(html: str) -> list[dict]:
    doc = lxml.html.fromstring(html)
    out, seen = [], set()
    for a in doc.xpath("//a[starts-with(@href,'/TrList/')]"):
        href = a.get("href")
        m = re.match(r"/TrList/(\d+)$", href)
        if not m or href in seen:
            continue
        seen.add(href)
        box = a.getparent()
        spans = box.xpath(".//span[contains(@class,'font-bold')]") if box is not None else []
        names = box.xpath("./p") if box is not None else []
        out.append({"list_id": int(m.group(1)), "tr": _text(spans[0]) if spans else "", "tr_name": _text(names[0]) if names else "",
                    "title": _text(a)})
    return out


def column_role(title: str) -> str:
    t = title.lower()
    if t.startswith("№"):
        return "n"
    if "ссылка" in t:
        return "link"
    if "наименование стандарта" in t:
        return "standard"
    if "структурный элемент" in t or "объект технического" in t:
        return "element"
    return "note"


def parse_list(html: str) -> dict:
    doc = lxml.html.fromstring(html)
    for bad in doc.xpath("//script|//style|//svg"):
        bad.drop_tree()
    text = " ".join(doc.text_content().split())
    gen = re.search(r"от\s+(\d{2}\.\d{2}\.\d{4})\s+Перечень сгенерирован", text)
    ed = re.search(r"\((в ред\.[^)]{5,300})\)", text)
    rows, element, header = [], "", None
    for tr in doc.xpath("//table//tr"):
        tds = tr.xpath("./td|./th")
        cells = [_text(td) for td in tds]
        if header is None and any("наименование стандарта" in c.lower() for c in cells):
            header = [column_role(c) for c in cells]
            continue
        if len(cells) < 3 or not re.fullmatch(r"\d+(?:\.\d+)*\.?", cells[0]) or cells[1:3] == ["2", "3"]:
            continue
        roles = header or ["n", "element", "standard", "note", "link"]
        shift = len(roles) - len(cells)
        if shift < 0:
            continue
        got = {"n": cells[0], "element": "", "standard": "", "note": [], "link": ""}
        for i, c in enumerate(cells[1:], 1):
            role = roles[i + shift]
            if role == "note":
                got["note"].append(c)
            elif role in got:
                got[role] = c
        if "element" in roles:
            element = got["element"] if shift == 0 and got["element"] else element
        else:
            element = ""
        standard, note, last = got["standard"], "; ".join(x for x in got["note"] if x), got["link"]
        link = tds[-1].xpath(".//a[contains(@href,'/details/')]")
        if link:
            designation = _text(link[0])
            status = last.replace(designation, "", 1).strip()
            href = link[0].get("href")
        else:
            m = DESIGNATION_RE.search(last) or DESIGNATION_RE.search(standard)
            designation = m.group(1).strip() if m else standard[:60]
            status, href = None, None
        rows.append({"n": got["n"], "element": element, "standard": standard[:300], "note": note[:300], "designation": designation,
                     "key": refdata.norm_designation(designation), "status_text": status or None,
                     "href": PROTECT + href if href else None})
    return {"generated": gen.group(1) if gen else None, "edition": ed.group(1) if ed else None, "rows": rows}


def parse_general(html: str) -> list[dict]:
    doc = lxml.html.fromstring(html)
    out, seen = [], set()
    for a in doc.xpath("//a[@href]"):
        t, href = _text(a), a.get("href")
        code = tr_code(t)
        if not code or code in seen:
            continue
        seen.add(code)
        url = href if href.startswith("http") else EEC + href
        out.append({"tr": code, "name": re.sub(r"\s*\(ТР.*$", "", t).strip(), "page_url": url})
    return out


def parse_tr_page(html: str) -> dict:
    doc = lxml.html.fromstring(html)
    lists, changes = [], []
    for det in doc.xpath("//details"):
        summary = _text(det.find("summary"))
        links = [{"title": _text(a), "url": a.get("href")} for a in det.xpath(".//a[@href]")]
        if "Перечни стандартов" in summary:
            lists += links
        elif "Изменения в технический регламент" in summary:
            changes += links
    return {"decisions": lists, "amendments": changes}


def parse_acts(xml: bytes) -> tuple[int, list[dict]]:
    root = lxml.etree.fromstring(xml)
    out = []
    for d in root.findall("document"):
        out.append({"id": d.get("id"), "title": (d.findtext("title") or "").strip(), "full_name": (d.findtext("full_name") or "").strip(),
                    "link": d.findtext("link"), "date_accepted": d.findtext("date_accepted"), "date_published": d.findtext("date_published"),
                    "files": [f.findtext("url") for f in d.findall("files/file") if f.findtext("url")]})
    return int(root.get("total") or 0), out


class EaeuSource(Source):
    name = "eaeu"
    title = "Перечни стандартов к техрегламентам ЕАЭС"
    filename = "eaeu.json.gz"
    min_records = 50
    field_names = {"edition": "редакция", "generated": "дата перечня", "rows_hash": "строки перечня", "rows_count": "строк",
                   "decisions_hash": "решения о перечнях", "amendments_hash": "изменения ТР"}

    def fields_for(self, rec):
        kind = rec.get("kind")
        if kind == "list":
            return ("edition", "generated", "rows_hash", "rows_count")
        if kind == "tr":
            return ("decisions_hash", "amendments_hash")
        return ("title", "full_name")

    def chain(self):
        return [("protect_trlist", "protect.gost.ru /TrList + ЕЭК + API правового портала ЕАЭС", self._via_protect),
                ("eec_portal", "ЕЭК и правовой портал ЕАЭС (без таблиц перечней)", self._via_eec)]

    def _lists(self) -> dict:
        idx = parse_index(self.http.get(f"{PROTECT}/TrList").text)
        if len(idx) < 50:
            raise SourceError(f"в индексе /TrList найдено {len(idx)} перечней, ожидалось около 94")
        out = {}
        for item in idx:
            url = f"{PROTECT}/TrList/{item['list_id']}"
            data = parse_list(self.http.get(url).text)
            rows = data["rows"]
            out[f"list:{item['list_id']}"] = {
                "kind": "list", "list_id": item["list_id"], "tr": tr_code(item["tr"]) or item["tr"], "tr_name": item["tr_name"],
                "title": item["title"][:400], "edition": data["edition"], "generated": data["generated"], "rows": rows,
                "rows_count": len(rows), "rows_hash": _hash([(r["key"], r["status_text"], r["element"]) for r in rows]),
                "url": url, "source": "protect.gost.ru, перечни к техрегламентам", "checked_at": now_iso()}
        return out

    def _eec(self) -> tuple[dict, list]:
        out, notes = {}, []
        for tr in parse_general(self.http.get(EEC_GENERAL).text):
            if "docs.eaeunion.org" in tr["page_url"]:
                info = {"decisions": [{"title": tr["name"], "url": tr["page_url"]}], "amendments": []}
            else:
                try:
                    info = parse_tr_page(self.http.get(tr["page_url"]).text)
                except FetchError as e:
                    notes.append(f"страница {tr['tr']} недоступна: {e}")
                    continue
            out[f"tr:{tr['tr']}"] = {"kind": "tr", "tr": tr["tr"], "name": tr["name"], "page_url": tr["page_url"],
                                     "decisions": info["decisions"], "amendments": info["amendments"],
                                     "decisions_hash": _hash(info["decisions"]), "amendments_hash": _hash(info["amendments"]),
                                     "source": "ЕЭК, страницы техрегламентов", "checked_at": now_iso()}
        return out, notes

    def _acts(self, prev: dict) -> tuple[dict, list]:
        meta = self.load_meta()
        since = (meta.get("acts_checked_to") or (datetime.now() - timedelta(days=120)).strftime("%Y-%m-%d"))
        since = (datetime.strptime(since, "%Y-%m-%d") - timedelta(days=3)).strftime("%Y-%m-%d")
        out, page, total = {}, 1, None
        while page <= 30:
            r = self.http.get(EAEU_API, params={"page_size": 100, "page_number": page, "date_published_from": since})
            total, docs = parse_acts(r.content)
            for d in docs:
                if ACT_FILTER.search(d["full_name"]) or ACT_FILTER.search(d["title"]):
                    out[f"act:{d['id']}"] = dict(d, kind="act", source="Правовой портал ЕАЭС, API", checked_at=now_iso())
            if not docs or page * 100 >= total:
                break
            page += 1
        return out, [f"API правового портала ЕАЭС с {since}: документов {total}, по стандартам и ТР {len(out)}"]

    def _common(self, prev: dict, lists: dict | None) -> Fetched:
        records = {k: v for k, v in prev.items() if v.get("kind") == "act"}
        if lists is None:
            records.update({k: v for k, v in prev.items() if v.get("kind") == "list"})
        else:
            records.update(lists)
        notes = []
        try:
            trs, n = self._eec()
            records.update(trs)
            notes += n
        except FetchError as e:
            records.update({k: v for k, v in prev.items() if v.get("kind") == "tr"})
            notes.append(f"страницы ЕЭК недоступны: {e}")
        try:
            acts, n = self._acts(prev)
            records.update(acts)
            notes += n
        except (FetchError, lxml.etree.XMLSyntaxError) as e:
            notes.append(f"API правового портала ЕАЭС недоступен: {e}")
        n_lists = sum(1 for r in records.values() if r.get("kind") == "list")
        n_rows = sum(r.get("rows_count", 0) for r in records.values() if r.get("kind") == "list")
        notes.insert(0, f"перечней {n_lists}, строк в них {n_rows}")
        meta = {"lists": n_lists, "rows": n_rows, "acts_checked_to": datetime.now().strftime("%Y-%m-%d")}
        return Fetched(records, f"{PROTECT}/TrList", now_iso()[:10], meta, notes)

    def _via_protect(self, prev: dict) -> Fetched:
        return self._common(prev, self._lists())

    def _via_eec(self, prev: dict) -> Fetched:
        if not any(v.get("kind") == "list" for v in prev.values()):
            raise SourceError("нет сохранённых таблиц перечней, а protect.gost.ru недоступен")
        res = self._common(prev, None)
        res.notes.append("таблицы перечней не обновлены: protect.gost.ru недоступен, оставлены прежние")
        return res

    def brief(self, rec):
        return {k: v for k, v in rec.items() if k not in ("rows", "decisions", "amendments")}

    def line_added(self, key, rec):
        kind = rec.get("kind")
        if kind == "list":
            return f"{rec.get('tr')}: перечень {rec.get('list_id')} ({rec.get('rows_count')} строк), {rec.get('edition') or 'редакция не указана'}"
        if kind == "tr":
            return f"{rec.get('tr')}: решений о перечнях {len(rec.get('decisions') or [])}"
        return f"новый акт ЕЭК: {rec.get('title')} от {rec.get('date_accepted')} «{rec.get('full_name', '')[:150]}»"

    def line_changed(self, key, fields, old, new):
        kind = new.get("kind")
        if kind == "list":
            parts = []
            if "edition" in fields:
                parts.append(f"редакция «{old.get('edition') or '—'}» → «{new.get('edition') or '—'}»")
            a = {r["key"]: r for r in old.get("rows", [])}
            b = {r["key"]: r for r in new.get("rows", [])}
            plus, minus = sorted(set(b) - set(a)), sorted(set(a) - set(b))
            if plus:
                parts.append("добавлены " + ", ".join(plus[:10]) + (f" и ещё {len(plus) - 10}" if len(plus) > 10 else ""))
            if minus:
                parts.append("исключены " + ", ".join(minus[:10]) + (f" и ещё {len(minus) - 10}" if len(minus) > 10 else ""))
            st = [f"{k}: «{a[k]['status_text'] or '—'}» → «{b[k]['status_text'] or '—'}»" for k in sorted(set(a) & set(b))
                  if a[k].get("status_text") != b[k].get("status_text")]
            parts += st[:15]
            if len(st) > 15:
                parts.append(f"ещё {len(st) - 15} смен статуса")
            return f"{new.get('tr')}, перечень {new.get('list_id')}: " + ("; ".join(parts) or "изменились строки")
        if kind == "tr":
            olds = {d["url"] for d in old.get("decisions", []) + old.get("amendments", [])}
            fresh = [d["title"] for d in new.get("decisions", []) + new.get("amendments", []) if d["url"] not in olds]
            return f"{new.get('tr')}: на странице ЕЭК новые решения: " + ("; ".join(fresh[:5]) or "изменился список ссылок")
        return super().line_changed(key, fields, old, new)
