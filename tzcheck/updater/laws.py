import json
import re
from datetime import datetime

import lxml.html

from .base import Fetched, Source, SourceError
from .browser import Browser
from .net import FetchError, now_iso

ACTUAL = "http://actual.pravo.gov.ru"
ACTUAL_API = "http://actual.pravo.gov.ru:8000/api/ebpi"
PUBLICATION = "http://publication.pravo.gov.ru"
FZ_TYPE = "82a8bf1c-3bc7-47ed-827f-7affd43a7f27"
CONSULTANT = "https://www.consultant.ru/document/"
ACTS = [
    {"act": "223-ФЗ", "number": "223-ФЗ", "date": "20110718", "name": "О закупках товаров, работ, услуг отдельными видами юридических лиц", "cons": "cons_doc_LAW_116964"},
    {"act": "44-ФЗ", "number": "44-ФЗ", "date": "20130405", "name": "О контрактной системе в сфере закупок", "cons": "cons_doc_LAW_144624"},
    {"act": "102-ФЗ", "number": "102-ФЗ", "date": "20080626", "name": "Об обеспечении единства измерений", "cons": "cons_doc_LAW_77904"},
    {"act": "184-ФЗ", "number": "184-ФЗ", "date": "20021227", "name": "О техническом регулировании", "cons": "cons_doc_LAW_40241"},
    {"act": "135-ФЗ", "number": "135-ФЗ", "date": "20060726", "name": "О защите конкуренции", "cons": "cons_doc_LAW_61763"},
    {"act": "162-ФЗ", "number": "162-ФЗ", "date": "20150629", "name": "О стандартизации в Российской Федерации", "cons": "cons_doc_LAW_181810"},
    {"act": "ГК ч.1", "number": "51-ФЗ", "date": "19941130", "name": "Гражданский кодекс Российской Федерации (часть первая)", "cons": "cons_doc_LAW_5142"},
    {"act": "ГК ч.2", "number": "14-ФЗ", "date": "19960126", "name": "Гражданский кодекс Российской Федерации (часть вторая)", "cons": "cons_doc_LAW_9027"},
]
PUB_FILTER = re.compile(r"закупк|единств\w* измерений|техническом регулировании|стандартизац|защите конкуренции|Гражданск\w+ кодекс", re.I)


def ymd(s: str | None) -> str | None:
    if not s or len(s) < 8:
        return None
    return f"{s[6:8]}.{s[4:6]}.{s[0:4]}"


def search_query(act: dict) -> str:
    return json.dumps([{"AttrId": 5, "AttrMode": 0, "DateFrom": act["date"], "DateTo": act["date"]},
                       {"AttrId": 6, "AttrMode": 8, "Words": [act["number"]]},
                       {"AttrId": 999, "AttrMode": 1, "Words": [50, "type", "20220701", 0, 1]}], ensure_ascii=False)


def build_record(act: dict, doc: dict, reds: list) -> dict:
    today = datetime.now().strftime("%Y%m%d")
    current = next((r for r in reds if r.get("actual")), None)
    if current is None:
        past = [r for r in reds if (r.get("reddate") or "") <= today]
        current = max(past, key=lambda r: r.get("reddate") or "", default=None)
    future = sorted((r for r in reds if (r.get("reddate") or "") > today), key=lambda r: r.get("reddate") or "")
    return {
        "act": act["act"], "name": act["name"], "number": act["number"], "signed": ymd(act["date"]),
        "docstate": doc.get("docstate"), "hash": doc.get("dochash"), "docid": doc.get("docid"),
        "redactions": len(reds),
        "current": {"redid": current.get("redid"), "date": ymd(current.get("reddate")), "caption": (current.get("redcaption") or "").strip()} if current else None,
        "future": [{"redid": r.get("redid"), "date": ymd(r.get("reddate")), "caption": (r.get("redcaption") or "").strip()} for r in future],
        "source": "Официальный интернет-портал правовой информации, actual.pravo.gov.ru",
        "source_url": f"{ACTUAL}/", "api_url": f"{ACTUAL_API}/redactions/?bpa=ebpi&t=" + json.dumps({"hash": doc.get("dochash"), "ttl": 4}),
        "checked_at": now_iso(),
    }


def consultant_tail(html: str) -> dict:
    doc = lxml.html.fromstring(html)
    title = (doc.findtext(".//title") or "").strip()
    text = " ".join(doc.text_content().split())
    m = re.search(r"Список изменяющих документов\s*\((.{10,6000}?)\)\s", text)
    body = m.group(1) if m else ""
    amend = re.findall(r"от\s+(\d{2}\.\d{2}\.\d{4})\s+N\s+(\d+(?:-ФЗ)?)", body)
    last = amend[-1] if amend else None
    return {"title": title[:200], "amendments": len(amend), "last_amendment": f"от {last[0]} N {last[1]}" if last else None}


class LawsSource(Source):
    name = "laws"
    title = "Редакции законов (223-ФЗ, 44-ФЗ, 102-ФЗ, 184-ФЗ, ГК РФ и др.)"
    filename = "laws.json"
    compare_fields = ("current", "future", "docstate", "consultant_last")
    field_names = {"current": "действующая редакция", "future": "будущие редакции", "docstate": "состояние", "consultant_last": "последнее изменение по КонсультантПлюс"}

    def health(self, got, prev):
        acts = [r for r in got.records.values() if r.get("kind", "act") == "act"]
        if len(acts) < len(ACTS) // 2:
            return f"найдено редакций только по {len(acts)} актам из {len(ACTS)}"
        return None

    def serialize(self, records):
        return json.dumps(self.to_file(records), ensure_ascii=False, indent=1).encode("utf-8")

    def chain(self):
        return [("actual_api", "actual.pravo.gov.ru, API :8000 (HTTP)", self._via_api),
                ("actual_browser", "actual.pravo.gov.ru через браузер (Playwright, перехват XHR)", self._via_browser),
                ("consultant", "КонсультантПлюс, «Список изменяющих документов» (сверка)", self._via_consultant)]

    def _api_get(self, path: str, params: dict) -> dict:
        r = self.http.get(f"{ACTUAL_API}/{path}", params=params)
        return r.json()

    def _finish(self, prev: dict, acts: dict, url: str, notes: list) -> Fetched:
        records = dict(prev)
        records.update(acts)
        cons_notes = self._consultant_check(records)
        notes = notes + cons_notes + self._publications(records)
        versions = [r["current"]["date"] for r in acts.values() if r.get("current")]
        return Fetched(records, url, max(versions, key=lambda d: d[6:] + d[3:5] + d[:2]) if versions else None, {}, notes)

    def _via_api(self, prev: dict) -> Fetched:
        acts, notes = {}, []
        for act in ACTS:
            try:
                found = self._api_get("attrsearch/", {"bpa": "ebpi", "q": search_query(act)})
                docs = found.get("docs") or []
                if not docs:
                    notes.append(f"{act['act']}: не найден в actual.pravo.gov.ru")
                    continue
                doc = docs[0]
                reds = self._api_get("redactions/", {"bpa": "ebpi", "t": json.dumps({"hash": doc["dochash"], "ttl": 4})}).get("redactions") or []
                acts[act["act"]] = build_record(act, doc, reds)
            except (FetchError, ValueError, KeyError) as e:
                notes.append(f"{act['act']}: {e}")
        if not acts:
            raise SourceError("API actual.pravo.gov.ru не вернул ни одного акта: " + "; ".join(notes[:3]))
        return self._finish(prev, acts, f"{ACTUAL}/", notes)

    def _via_browser(self, prev: dict) -> Fetched:
        if not self.ctx.browser or self.http.offline:
            raise SourceError("браузерный фолбэк выключен")
        acts, notes = {}, []
        with Browser(wait_ms=4000) as br:
            for act in ACTS:
                page = br.open(f"{ACTUAL}/")
                captured = {}

                def on_response(resp, captured=captured):
                    u = resp.url
                    if "/api/ebpi/" in u:
                        for k in ("attrsearch", "redactions", "card"):
                            if f"/{k}" in u:
                                try:
                                    captured[k] = resp.json()
                                except Exception:
                                    pass

                page.on("response", on_response)
                try:
                    page.fill("input[name=number]", act["number"])
                    page.keyboard.press("Enter")
                    page.wait_for_timeout(4000)
                    docs = (captured.get("attrsearch") or {}).get("docs") or []
                    doc = next((d for d in docs if d.get("docpass0date") == act["date"]), docs[0] if docs else None)
                    if not doc:
                        notes.append(f"{act['act']}: поиск в браузере ничего не вернул")
                        continue
                    data = br.fetch_json(page, f"{ACTUAL_API}/redactions/?bpa=ebpi&t=" + json.dumps({"hash": doc["dochash"], "ttl": 4}))
                    acts[act["act"]] = build_record(act, doc, data.get("redactions") or [])
                    acts[act["act"]]["via"] = "browser"
                except FetchError as e:
                    notes.append(f"{act['act']}: {e}")
                except Exception as e:
                    notes.append(f"{act['act']}: {type(e).__name__}: {str(e)[:150]}")
                finally:
                    page.close()
        if not acts:
            raise SourceError("в браузере не удалось получить редакции: " + "; ".join(notes[:3]))
        return self._finish(prev, acts, f"{ACTUAL}/", notes)

    def _via_consultant(self, prev: dict) -> Fetched:
        records = dict(prev)
        notes = self._consultant_check(records)
        if not any(r.get("consultant_checked_at") for r in records.values()):
            raise SourceError("КонсультантПлюс тоже недоступен")
        notes.append("редакции по actual.pravo.gov.ru не обновлены, изменения видны только по КонсультантПлюс")
        return Fetched(records, CONSULTANT, None, {}, notes)

    def _consultant_check(self, records: dict) -> list:
        notes = []
        for act in ACTS:
            try:
                info = consultant_tail(self.http.get(CONSULTANT + act["cons"] + "/").text)
            except FetchError as e:
                notes.append(f"КонсультантПлюс, {act['act']}: {e}")
                continue
            rec = records.setdefault(act["act"], {"act": act["act"], "name": act["name"], "number": act["number"], "signed": ymd(act["date"])})
            rec["consultant_url"] = CONSULTANT + act["cons"] + "/"
            rec["consultant_last"] = info["last_amendment"]
            rec["consultant_checked_at"] = now_iso()
        return notes

    def _publications(self, records: dict) -> list:
        try:
            r = self.http.get(f"{PUBLICATION}/api/Documents", params={"DocumentTypes": FZ_TYPE, "PageSize": 30, "Index": 1})
            items = r.json().get("items") or []
        except (FetchError, ValueError) as e:
            return [f"publication.pravo.gov.ru недоступен: {e}"]
        fresh = []
        for it in items:
            name = (it.get("name") or "").strip('"')
            if not PUB_FILTER.search(name):
                continue
            key = f"pub:{it.get('eoNumber')}"
            records[key] = {"kind": "publication", "number": it.get("number"), "date": (it.get("documentDate") or "")[:10],
                            "name": name, "published": it.get("publishDateShort"), "eo_number": it.get("eoNumber"),
                            "url": f"{PUBLICATION}/document/{it.get('eoNumber')}", "checked_at": now_iso()}
            fresh.append(it.get("number"))
        return [f"publication.pravo.gov.ru: последних федеральных законов {len(items)}, по теме закупок и стандартизации {len(fresh)}"]

    def fields_for(self, rec):
        if rec.get("kind") == "publication":
            return ("name", "number")
        return self.compare_fields

    def line_added(self, key, rec):
        if rec.get("kind") == "publication":
            return f"опубликован федеральный закон № {rec.get('number')} от {rec.get('date')} «{rec.get('name', '')[:150]}»"
        cur = rec.get("current") or {}
        fut = ", ".join(f["date"] for f in rec.get("future") or [] if f.get("date"))
        return f"{rec.get('act')}: действующая редакция с {cur.get('date') or '?'}" + (f", будущие: {fut}" if fut else "")

    def line_changed(self, key, fields, old, new):
        parts = []
        if "current" in fields:
            a, b = fields["current"]
            parts.append(f"новая действующая редакция: с {(b or {}).get('date') or '?'} (была с {(a or {}).get('date') or '?'})")
        if "future" in fields:
            dates = [f["date"] for f in new.get("future") or []]
            parts.append("будущие редакции: " + (", ".join(dates) if dates else "нет"))
        if "consultant_last" in fields:
            parts.append(f"КонсультантПлюс: последнее изменение {fields['consultant_last'][1]} (было {fields['consultant_last'][0] or '—'})")
        if "docstate" in fields:
            parts.append(f"состояние «{fields['docstate'][0]}» → «{fields['docstate'][1]}»")
        return f"{new.get('act') or key}: " + "; ".join(parts or ["изменились сведения"])
