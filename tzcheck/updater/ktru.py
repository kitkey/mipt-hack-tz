import hashlib
import json
import os
import re
import time
from urllib.parse import urlencode

import lxml.etree
import lxml.html

from .. import refdata
from .base import Fetched, Source, SourceError
from .browser import Browser
from .net import FetchError, now_iso

ZAK = "https://zakupki.gov.ru"
RSS = ZAK + "/epz/ktru/search/rss"
SEARCH = ZAK + "/epz/ktru/search/results.html"
COMMON = ZAK + "/epz/ktru/ktruCard/commonInfo.html"
DESC = ZAK + "/epz/ktru/ktruCard/ktru-description.html"
WATCH = ["28.22.16.110", "28.22.16.111", "28.22.16.112", "26.51.66.190", "26.51.33.110", "26.51.43.117", "26.51.62.120", "26.70.13.000"]



def _corpus_codes() -> list[str]:
    """Коды позиций из корпуса ТЗ и реагентов (data/ktru_watch.json): по ним правило check_ktru берёт обязательные характеристики."""
    try:
        with open(os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "ktru_watch.json"), encoding="utf-8") as f:
            return [c for c in json.load(f).get("codes", []) if c not in WATCH]
    except (OSError, ValueError):
        return []


WATCH += _corpus_codes()
MAX_CARDS = int(os.environ.get("TZCHECK_KTRU_MAX_CARDS", "40"))
COMMON_FIELDS = {"Статус": "status", "Дата включения позиции в каталог": "included", "Код позиции КТРУ": "code",
                 "Код по ОКПД2": "okpd2_text", "Наименование товара, работы, услуги": "name",
                 "Единицы измерения (количество товара, объем работы, услуги по ОКЕИ)": "unit",
                 "Дата начала обязательного применения позиции каталога": "mandatory_from",
                 "Дата окончания применения позиции каталога": "mandatory_to"}
RSS_FIELDS = {"Статус": "status", "Код позиции КТРУ": "code", "Наименование товара, работы, услуги": "name",
              "Размещено": "placed", "Обновлено": "updated", "Дата окончания обязательного применения позиции каталога": "mandatory_to"}


def _text(el) -> str:
    return " ".join(el.text_content().split()) if el is not None else ""


def parse_rss(xml: bytes) -> list[dict]:
    root = lxml.etree.fromstring(xml)
    out = []
    for item in root.iter("item"):
        content = item.findtext("{http://purl.org/rss/1.0/modules/content/}encoded") or ""
        rec = {"title": (item.findtext("title") or "").strip()}
        for label, value in re.findall(r"<strong>([^<:]+):\s*</strong>([^<]*)", content):
            key = RSS_FIELDS.get(label.strip())
            if key:
                rec[key] = value.strip() or None
        if rec.get("code"):
            out.append(rec)
    return out


def parse_common(html: str) -> dict:
    doc = lxml.html.fromstring(html)
    out = {}
    for s in doc.xpath("//section[contains(@class,'section')]"):
        t = s.xpath("./span[contains(@class,'section__title')]")
        i = s.xpath("./span[contains(@class,'section__info')]")
        if t and i:
            key = COMMON_FIELDS.get(_text(t[0]))
            if key and key not in out:
                out[key] = _text(i[0])
    if out.get("okpd2_text"):
        out["okpd2"] = out["okpd2_text"].split(":")[0].strip()
    docs = []
    for span in doc.xpath("//span[contains(@class,'section__title')][contains(.,'технических регламентах')]"):
        table = span.getnext()
        if table is not None:
            docs += [_text(td) for td in table.xpath(".//tbody/tr/td[1]") if _text(td)]
    out["tr_standards"] = docs
    return out


def parse_desc(html: str) -> list[dict]:
    doc = lxml.html.fromstring(html)
    chars, cur = [], None
    for tr in doc.xpath("//div[@id='ktruCharacteristicContent']//tbody/tr"):
        tds = tr.xpath("./td")
        first = [td for td in tds if "col_first" in (td.get("class") or "")]
        if first:
            name_div = first[0].xpath("./div")
            name = _text(name_div[0]) if name_div else _text(first[0])
            flag = _text(first[0])
            cur = {"name": name, "required": "не является обязательной" not in flag and "является обязательной" in flag, "values": [], "unit": None}
            chars.append(cur)
            tds = [td for td in tds if td not in first]
        if cur is None or not tds:
            continue
        value = _text(tds[0])
        unit = _text(tds[1]) if len(tds) > 1 else ""
        if value:
            cur["values"].append(value)
        if unit and not cur["unit"]:
            cur["unit"] = unit
    return chars


class KtruSource(Source):
    name = "ktru"
    title = "КТРУ ЕИС: позиции и обязательные характеристики"
    filename = "ktru.json.gz"
    field_names = {"name": "наименование", "updated": "обновлено", "mandatory_from": "обязательна с", "mandatory_to": "обязательна до",
                   "chars_hash": "характеристики", "status": "статус", "tr_standards": "ТР и стандарты"}
    codes_override: list | None = None

    def fields_for(self, rec):
        return ("name", "status", "updated", "mandatory_from", "mandatory_to", "chars_hash", "tr_standards")

    def chain(self):
        return [("zakupki_http", "ЕИС zakupki.gov.ru, RSS и карточки КТРУ (HTTP)", self._via_http),
                ("zakupki_browser", "ЕИС zakupki.gov.ru через браузер (Playwright)", self._via_browser)]

    def codes(self, prev: dict) -> list[str]:
        if self.codes_override:
            return self.codes_override
        codes = set(WATCH)
        codes |= {r["watch_code"] for r in prev.values() if r.get("watch_code")}
        try:
            with open(os.path.join(refdata.data_dir(), "cache", "ktru_requested.json"), encoding="utf-8") as f:
                codes |= set(json.load(f))
        except (OSError, ValueError):
            pass
        env = os.environ.get("TZCHECK_KTRU_CODES")
        if env:
            codes |= {c.strip() for c in env.split(",") if c.strip()}
        return sorted(codes)

    def _collect(self, get, prev: dict) -> Fetched:
        records = dict(prev)
        notes, cards, errors = [], 0, 0
        for code in self.codes(prev):
            try:
                items = parse_rss(get(RSS, {"searchString": code, "active": "on", "recordsPerPage": "_50"}))
            except (FetchError, lxml.etree.XMLSyntaxError) as e:
                errors += 1
                notes.append(f"{code}: RSS недоступен: {e}")
                continue
            notes.append(f"{code}: позиций КТРУ {len(items)}")
            for it in items:
                key = it["code"]
                old = records.get(key)
                if old and old.get("updated") == it.get("updated") and old.get("chars") is not None:
                    old["checked_at"] = now_iso()
                    continue
                if cards >= MAX_CARDS:
                    notes.append(f"лимит {MAX_CARDS} карточек за запуск, остальные в следующий раз")
                    break
                try:
                    common = parse_common(get(COMMON, {"itemId": key}).decode("utf-8", "replace"))
                    chars = parse_desc(get(DESC, {"itemId": key}).decode("utf-8", "replace"))
                    cards += 1
                except FetchError as e:
                    errors += 1
                    notes.append(f"{key}: карточка недоступна: {e}")
                    continue
                rec = {"kind": "position", "code": key, "watch_code": code, "okpd2": common.get("okpd2") or key.split("-")[0],
                       "name": common.get("name") or it.get("title"), "status": common.get("status") or it.get("status"),
                       "unit": common.get("unit"), "included": common.get("included"), "placed": it.get("placed"),
                       "updated": it.get("updated"), "mandatory_from": common.get("mandatory_from"),
                       "mandatory_to": common.get("mandatory_to") or it.get("mandatory_to"), "tr_standards": common.get("tr_standards", []),
                       "chars": chars, "chars_required": sum(1 for c in chars if c["required"]),
                       "chars_hash": hashlib.sha256(json.dumps(chars, ensure_ascii=False).encode()).hexdigest()[:16],
                       "url": f"{COMMON}?itemId={key}", "source": "ЕИС, каталог товаров, работ, услуг (КТРУ)", "checked_at": now_iso()}
                records[key] = rec
        if errors and not cards and not any(r.get("kind") == "position" for r in records.values()):
            raise SourceError("ЕИС не ответила ни на один запрос: " + "; ".join(notes[:3]))
        notes.insert(0, f"кодов ОКПД 2 в списке наблюдения: {len(self.codes(prev))}, загружено карточек: {cards}")
        return Fetched(records, f"{ZAK}/epz/ktru/", now_iso()[:10], {"codes": self.codes(prev)}, notes)

    def _via_http(self, prev: dict) -> Fetched:
        return self._collect(lambda url, params: self.http.get(url, params=params).content, prev)

    def _via_browser(self, prev: dict) -> Fetched:
        if not self.ctx.browser or self.http.offline:
            raise SourceError("браузерный фолбэк выключен")
        delay = float(os.environ.get("TZCHECK_ZAKUPKI_DELAY", "60"))
        with Browser() as br:
            page = br.open(SEARCH)

            def get(url, params):
                time.sleep(delay)
                return br.fetch_text(page, f"{url}?{urlencode(params)}").encode("utf-8")

            return self._collect(get, prev)

    def brief(self, rec):
        return {k: rec.get(k) for k in ("code", "okpd2", "name", "updated", "chars_required")}

    def line_added(self, key, rec):
        return f"позиция КТРУ {key} «{(rec.get('name') or '')[:80]}», обязательных характеристик {rec.get('chars_required', 0)}"

    def line_changed(self, key, fields, old, new):
        parts = []
        if "chars_hash" in fields:
            a = {c["name"]: c for c in old.get("chars") or []}
            b = {c["name"]: c for c in new.get("chars") or []}
            plus = [n for n in b if n not in a]
            minus = [n for n in a if n not in b]
            req = [n for n in b if n in a and a[n]["required"] != b[n]["required"]]
            if plus:
                parts.append("новые характеристики: " + ", ".join(plus[:8]))
            if minus:
                parts.append("удалены характеристики: " + ", ".join(minus[:8]))
            if req:
                parts.append("сменилась обязательность: " + ", ".join(req[:8]))
            if not (plus or minus or req):
                parts.append("изменились значения характеристик")
        for f in ("name", "status", "updated", "mandatory_from", "mandatory_to"):
            if f in fields:
                parts.append(f"{self.field_names[f]}: «{fields[f][0] or '—'}» → «{fields[f][1] or '—'}»")
        if "tr_standards" in fields:
            parts.append("изменился перечень ТР и стандартов")
        return f"КТРУ {key}: " + "; ".join(parts)
