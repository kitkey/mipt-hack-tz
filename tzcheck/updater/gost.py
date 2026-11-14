import os
import re
from datetime import datetime, timedelta
from urllib.parse import quote

import lxml.html

from .. import refdata
from .base import Fetched, Source, SourceError
from .browser import Browser
from .net import FetchError, now_iso

API = "https://www.rst.gov.ru/standards/rest-api/libraries/search"
API_PATH = "/standards/rest-api/libraries/search"
LIBS = {
    "national": {"id": 11, "page": "https://www.rst.gov.ru/portal/gost/home/standarts/catalognational", "all": "ГОСТ Р"},
    "interstate": {"id": 35, "page": "https://www.rst.gov.ru/portal/gost/home/standarts/cataloginter", "all": "ГОСТ"},
}
PROTECT = "https://protect.gost.ru"
HEADERS_RE = re.compile(r"cr:\s*'/standards'\s*,\s*adminsHeaders\s*:\s*\{([^}]*)\}")
PAIR_RE = re.compile(r"(?:'([^']+)'|([A-Za-z][\w.-]*))\s*:\s*'([^']*)'")
STATUS = {"действует": "Действует", "действует только в рф": "Действует", "заменен": "Заменён", "заменён": "Заменён",
          "отменен": "Отменён", "отменён": "Отменён", "принят": "Принят", "утратил силу в рф": "Утратил силу в РФ",
          "срок действия истек": "Истёк срок действия", "срок действия истёк": "Истёк срок действия"}
WATCH = [
    "ГОСТ Р 53780-2010", "ГОСТ Р 53782-2010", "ГОСТ Р 53783-2010", "ГОСТ 33984.1-2016", "ГОСТ 33984.1-2023",
    "ГОСТ Р 51631-2008", "ГОСТ Р 52382-2010", "ГОСТ Р 55964-2022", "ГОСТ Р 55965-2014", "ГОСТ Р 55969-2023",
    "ГОСТ 28911-2015", "ГОСТ 34441-2018", "ГОСТ 33605-2015", "ГОСТ 34442-2018", "ГОСТ Р 59155-2020",
    "ГОСТ OIML R 76-1-2011", "ГОСТ 8.401-80", "ГОСТ 8.417-2002", "ГОСТ Р 2.601-2019", "ГОСТ Р 2.610-2019",
]
DETAIL_LABELS = {
    "Полное обозначение": "full", "Заглавие на русском языке": "title", "Номер приказа": "order", "Дата приказа": "date_approved",
    "Дата введения в действие": "date_intro", "Код ОКС": "oks", "Обозначение заменяющего": "replaced_by",
    "Обозначение заменяющего в части": "replaced_in_part", "Отменен в части": "note", "На территории РФ пользоваться": "use_instead",
}


def library_for(key: str) -> str:
    if key.startswith("СП "):
        return "sp"
    if key.startswith(("ГОСТ Р", "ПНСТ")):
        return "national"
    return "interstate"


def clean(v) -> str | None:
    if v is None:
        return None
    s = re.sub(r"\s+", " ", str(v)).strip()
    return None if s in ("", "-", "—") else s


def norm_status(s) -> str | None:
    s = clean(s)
    if not s:
        return None
    s = re.sub(r"^[^\wА-ЯЁа-яё]+", "", s).strip()
    return STATUS.get(s.lower(), s)


def date_part(s) -> str | None:
    s = clean(s)
    if not s:
        return None
    m = re.match(r"(\d{2})\.(\d{2})\.(\d{4})(?:\s+(\d{2}):)?", s)
    if not m:
        m2 = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
        return f"{m2[3]}.{m2[2]}.{m2[1]}" if m2 else None
    d = datetime(int(m[3]), int(m[2]), int(m[1]))
    if m[4] and int(m[4]) >= 20:
        d += timedelta(days=1)
    return d.strftime("%d.%m.%Y")


def check_url(designation: str) -> str:
    path = "/sp" if designation.startswith("СП ") else "/gost"
    return f"{PROTECT}{path}?year=0&month=0&search={quote(designation)}"


def parse_headers(html: str) -> dict:
    m = HEADERS_RE.search(html)
    if not m:
        return {}
    return {(a or b): c for a, b, c in PAIR_RE.findall(m.group(1))}


def parse_record(item: dict, lib: str) -> dict:
    f = item.get("fields", {})
    status = norm_status(f.get("a90"))
    inactive = status in refdata.INACTIVE
    designation = clean(f.get("a02")) or ""
    return {
        "designation": designation, "full": clean(f.get("a02_full")), "title": clean(f.get("a03")),
        "status": status, "status_raw": clean(f.get("a90")),
        "replaced_by": clean(f.get("a22")) or (clean(f.get("a_us")) if inactive else None),
        "use_instead": clean(f.get("a_us")), "replaced_in_part": clean(f.get("a24")), "note": clean(f.get("awp")),
        "order": clean(f.get("a06")), "date_approved": date_part(f.get("a07")), "date_intro": date_part(f.get("a08")),
        "date_end": date_part(f.get("a09")) if inactive else None, "oks": clean(f.get("a25")), "kind": clean(f.get("a14")),
        "library": lib, "rst_id": item.get("id"), "last_update": f.get("last_update"),
        "source": "Росстандарт, каталог стандартов", "source_url": LIBS[lib]["page"], "check_url": check_url(designation),
        "via": "rst_api", "checked_at": now_iso(),
    }


def parse_protect_rows(html: str) -> list[dict]:
    doc = lxml.html.fromstring(html)
    out = []
    for tr in doc.xpath("//table//tbody/tr"):
        tds = tr.xpath("./td")
        links = tr.xpath(".//a[contains(@href,'/details/')]")
        if len(tds) < 3 or not links:
            continue
        cells = [" ".join(td.text_content().split()) for td in tds]
        out.append({"designation": cells[0], "title": cells[1], "status": norm_status(cells[-1]), "href": links[0].get("href")})
    return out


def parse_details(html: str) -> dict:
    doc = lxml.html.fromstring(html)
    out = {}
    for span in doc.xpath("//span[contains(@class,'tracking-wider')]"):
        label = " ".join(span.text_content().split())
        nxt = span.getnext()
        if label in DETAIL_LABELS and nxt is not None:
            out[DETAIL_LABELS[label]] = clean(nxt.text_content())
    for k in ("date_approved", "date_intro"):
        if out.get(k):
            out[k] = date_part(out[k])
    return out


def pages_count(html: str) -> int:
    m = re.search(r"Стр\.\s*\d+\s*из\s*(\d+)", html)
    return int(m.group(1)) if m else 1


class RstApi:
    def __init__(self, http):
        self.http = http
        self.hdrs = {}

    def headers(self, lib: str, fresh: bool = False) -> dict:
        if not fresh and lib in self.hdrs:
            return self.hdrs[lib]
        if not fresh:
            hit = refdata.cache_get("rst_headers", lib)
            if hit and hit.get("value") and refdata._age(hit.get("checked_at")) < 12 * 3600:
                self.hdrs[lib] = hit["value"]
                return hit["value"]
        r = self.http.get(LIBS[lib]["page"])
        h = parse_headers(r.text)
        if "App-Id" not in h:
            raise SourceError("на странице каталога Росстандарта не найден блок adminsHeaders (cr: '/standards')")
        refdata.cache_put("rst_headers", lib, h)
        self.hdrs[lib] = h
        return h

    def search(self, lib: str, query: str, page: int = 1, size: int = 50, save: bool = True) -> tuple[int, list]:
        last = None
        for attempt in (0, 1):
            h = self.headers(lib, fresh=attempt == 1)
            try:
                r = self.http.get(API, params={"query": query, "page": page, "size": size}, headers=h, save=save)
            except FetchError as e:
                last = e
                if "HTTP 500" in str(e) and not self.http.offline:
                    continue
                raise
            j = r.json()
            return int(j.get("count") or 0), j.get("data") or []
        raise last


def match(items: list, key: str, lib: str) -> dict | None:
    for it in items:
        if refdata.norm_designation((it.get("fields") or {}).get("a02") or "") == key:
            return parse_record(it, lib)
    return None


def rst_lookup(key: str, api: RstApi) -> dict | bool:
    lib = library_for(key)
    if lib == "sp":
        raise SourceError("своды правил в каталоге Росстандарта не ищутся")
    count, items = api.search(lib, key, size=20)
    return match(items, key, lib) or False


def protect_lookup(key: str, http) -> dict | bool:
    sp = key.startswith("СП ")
    num = key.split(" ")[-1]
    r = http.get(f"{PROTECT}/{'sp' if sp else 'gost'}", params={"year": 0, "month": 0, "search": num})
    for row in parse_protect_rows(r.text):
        if refdata.norm_designation(row["designation"]) != key:
            continue
        rec = {"designation": row["designation"], "title": row["title"], "status": row["status"], "status_raw": row["status"],
               "library": "sp" if sp else library_for(key), "source": "protect.gost.ru (Институт стандартизации)",
               "source_url": PROTECT + row["href"], "check_url": PROTECT + row["href"], "via": "protect", "checked_at": now_iso()}
        try:
            rec.update({k: v for k, v in parse_details(http.get(PROTECT + row["href"]).text).items() if v})
        except FetchError:
            pass
        if rec["status"] not in refdata.INACTIVE:
            rec["replaced_by"] = None
        return rec
    return False


def lookup(key: str, http) -> dict | bool:
    key = refdata.norm_designation(key)
    if library_for(key) == "sp":
        return protect_lookup(key, http)
    try:
        return rst_lookup(key, RstApi(http))
    except (FetchError, SourceError, ValueError):
        return protect_lookup(key, http)


class GostSource(Source):
    name = "gost"
    title = "Статусы стандартов (ГОСТ, ГОСТ Р, ПНСТ, СП)"
    filename = "gost.json.gz"
    compare_fields = ("status", "replaced_by", "use_instead", "replaced_in_part", "note", "date_intro", "date_end", "title")
    field_names = {"status": "статус", "replaced_by": "заменяющий", "use_instead": "пользоваться", "replaced_in_part": "заменён в части",
                   "note": "пояснение", "date_intro": "дата введения", "date_end": "дата прекращения", "title": "заглавие"}

    def __init__(self, ctx):
        super().__init__(ctx)
        self._targets = None
        self._feed = None

    def chain(self):
        return [("rst_api", "Росстандарт, API каталога стандартов (HTTP)", self._via_api),
                ("rst_browser", "Росстандарт, API каталога через браузер (Playwright)", self._via_browser),
                ("protect", "protect.gost.ru, поиск и карточки стандартов", self._via_protect)]

    def targets(self, prev: dict) -> list[str]:
        if self._targets is None:
            keys = {refdata.norm_designation(x) for x in WATCH}
            keys |= {refdata.norm_designation(k) for k in refdata.cache_keys("gost")}
            keys |= {k for k, r in prev.items() if r.get("watch")}
            self._targets = sorted(k for k in keys if k)
        return self._targets

    def feeds(self) -> dict:
        if self._feed is not None:
            return self._feed
        today = datetime.now()
        months = [(today.year, today.month)]
        months.append((today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1))
        new, changes, notes = {}, [], []
        for y, m in months:
            try:
                rows = self._feed_pages(f"{PROTECT}/gost", y, m)
                for row in rows:
                    key = refdata.norm_designation(row["designation"])
                    new[key] = {"designation": row["designation"], "title": row["title"], "status": row["status"],
                                "status_raw": row["status"], "library": library_for(key), "source": "protect.gost.ru, поступления в фонд",
                                "source_url": PROTECT + row["href"], "check_url": PROTECT + row["href"], "via": "protect_feed",
                                "feed_month": f"{m:02d}.{y}", "checked_at": now_iso()}
                ch = self._feed_changes(y, m)
                changes += ch
                notes.append(f"protect.gost.ru за {m:02d}.{y}: новых стандартов {len(rows)}, изменений и поправок {len(ch)}")
            except FetchError as e:
                notes.append(f"лента protect.gost.ru за {m:02d}.{y} недоступна: {e}")
        self._feed = {"new": new, "changes": changes, "notes": notes}
        return self._feed

    def _feed_pages(self, base: str, y: int, m: int) -> list[dict]:
        r = self.http.get(base, params={"year": y, "month": m})
        rows = parse_protect_rows(r.text)
        for p in range(2, min(pages_count(r.text), 40) + 1):
            rows += parse_protect_rows(self.http.get(base, params={"year": y, "month": m, "search": "", "page": p}).text)
        return rows

    def _feed_changes(self, y: int, m: int) -> list[dict]:
        out = []
        r = self.http.get(f"{PROTECT}/gost/changes", params={"year": y, "month": m})
        htmls = [r.text] + [self.http.get(f"{PROTECT}/gost/changes", params={"year": y, "month": m, "search": "", "page": p}).text
                            for p in range(2, min(pages_count(r.text), 40) + 1)]
        for html in htmls:
            doc = lxml.html.fromstring(html)
            for tr in doc.xpath("//table//tbody/tr"):
                cells = [" ".join(td.text_content().split()) for td in tr.xpath("./td")]
                if len(cells) < 3:
                    continue
                t = re.search(r"\bк\s+((?:ГОСТ|ПНСТ)[^,;]*?\d[\d.\-–]*\d)\s*$", cells[0])
                out.append({"title": cells[0], "kind": cells[1], "ius": cells[2], "target": refdata.norm_designation(t.group(1)) if t else None})
        return out

    def _merge(self, prev: dict, fetch_one, full: dict | None = None) -> Fetched:
        records = dict(prev)
        if full:
            for k, r in full.items():
                old = records.get(k)
                records[k] = dict(r, watch=bool(old and old.get("watch")))
        feed = self.feeds()
        for k, r in feed["new"].items():
            if k not in records:
                records[k] = r
        changed_targets = {c["target"] for c in feed["changes"] if c.get("target")}
        targets = sorted(set(self.targets(prev)) | (changed_targets & set(records)))
        found, missing, failed = 0, [], []
        for k in targets:
            try:
                rec = protect_lookup(k, self.http) if library_for(k) == "sp" else fetch_one(k)
            except (FetchError, SourceError, ValueError) as e:
                failed.append(f"{k}: {e}")
                continue
            if rec:
                rec["watch"] = True
                records[k] = rec
                found += 1
            else:
                missing.append(k)
        if targets and len(failed) * 2 >= len(targets):
            raise SourceError(f"не проверено {len(failed)} из {len(targets)} обозначений; первая ошибка: {failed[0]}")
        notes = list(feed["notes"])
        notes.append(f"проверено обозначений: {len(targets)}, найдено {found}, нет в каталоге {len(missing)}"
                     + (f": {', '.join(missing[:15])}" if missing else ""))
        amend = [c for c in feed["changes"] if c.get("target") in records]
        for c in amend[:30]:
            notes.append(f"{c['title']} ({c['kind']}, {c['ius']})")
        notes += [f"ошибка проверки {x}" for x in failed[:10]]
        return Fetched(records, LIBS["national"]["page"], now_iso()[:10], {"targets": len(targets), "missing": missing}, notes)

    def _via_api(self, prev: dict) -> Fetched:
        api = RstApi(self.http)
        for lib in LIBS:
            api.headers(lib)
        full = self._full_dump(api) if self.ctx.full else None

        def api_one(k):
            return rst_lookup(k, api)

        return self._merge(prev, api_one, full)

    def _via_browser(self, prev: dict) -> Fetched:
        if not self.ctx.browser or self.http.offline:
            raise SourceError("браузерный фолбэк выключен")
        with Browser() as br:
            pages = {}

            def browser_one(k):
                lib = library_for(k)
                if lib not in pages:
                    pages[lib] = br.open(LIBS[lib]["page"])
                url = f"{API_PATH}?query={quote(k)}&page=1&size=20"
                data = br.fetch_json(pages[lib], url, "standarts.adminsHeaders")
                rec = match(data.get("data") or [], k, lib)
                if rec:
                    rec["via"] = "rst_browser"
                return rec or False

            return self._merge(prev, browser_one)

    def _via_protect(self, prev: dict) -> Fetched:
        def protect_one(k):
            return protect_lookup(k, self.http)

        res = self._merge(prev, protect_one)
        res.url = PROTECT + "/gost"
        return res

    def _full_dump(self, api: RstApi) -> dict:
        year = datetime.now().year
        override = os.environ.get("TZCHECK_GOST_FULL_TOKENS")
        plan = {
            "national": [f"-{y:02d}" for y in range(92, 100)] + [f"-{y}" for y in range(1992, year + 1)] + ["ПНСТ"],
            "interstate": [f"-{y:02d}" for y in range(30, 100)] + [f"-{y}" for y in range(2000, year + 1)],
        }
        if override:
            plan = {lib: override.split(",") for lib in plan}
        out, by_id = {}, {}
        for lib, tokens in plan.items():
            for tok in tokens:
                page = 1
                while page <= 10:
                    count, items = api.search(lib, tok, page=page, size=1000, save=False)
                    for it in items:
                        rec = parse_record(it, lib)
                        rec["via"] = "rst_full"
                        by_id[(lib, it.get("id"))] = rec
                    if count > 10000:
                        self.ctx.say(f"[gost]   {tok}: {count} записей, видно только 10 000")
                    if not items or page * 1000 >= min(count, 10000):
                        break
                    page += 1
                self.ctx.say(f"[gost]   {lib} {tok}: собрано {len(by_id)}")
        for rec in by_id.values():
            k = refdata.norm_designation(rec["designation"])
            if k and (k not in out or (rec.get("last_update") or 0) > (out[k].get("last_update") or 0)):
                out[k] = rec
        return out

    def brief(self, rec: dict) -> dict:
        return {k: rec.get(k) for k in ("designation", "title", "status", "replaced_by", "date_end", "source_url")}

    def line_added(self, key, rec):
        st = rec.get("status") or "?"
        return f"{rec.get('designation') or key}: новый в базе, статус «{st}»" + (f", «{rec['title'][:100]}»" if rec.get("title") else "")

    def line_changed(self, key, fields, old, new):
        if "status" in fields:
            a, b = fields["status"]
            s = f"{new.get('designation') or key}: статус «{a or '—'}» → «{b or '—'}»"
            if new.get("replaced_by"):
                s += f", заменяющий {new['replaced_by']}"
            if new.get("date_end"):
                s += f", с {new['date_end']}"
            rest = {f: v for f, v in fields.items() if f not in ("status", "replaced_by", "date_end")}
            return s + ("; " + super().line_changed(key, rest, old, new).split(": ", 1)[1] if rest else "")
        return super().line_changed(key, fields, old, new)
