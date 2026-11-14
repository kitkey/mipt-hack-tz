from datetime import datetime
from urllib.parse import urlencode

from .base import Fetched, Source, SourceError
from .browser import Browser
from .net import FetchError, now_iso

EAPI = "https://fgis.gost.ru/fundmetrology/eapi"
SPA = "https://fgis.gost.ru/fundmetrology/cm/"
ROWS = 100
MAX_START = 99999


def brief(it: dict) -> dict:
    return {"number": it.get("number"), "title": (it.get("title") or "").strip(), "notation": (it.get("notation") or "").strip(),
            "manufacturer": (it.get("manufacturers") or "").strip()[:200], "uuid": it.get("mit_uuid")}


def card_url(uuid: str) -> str:
    return f"{EAPI}/mit/{uuid}"


def enrich(item: dict, card: dict) -> dict:
    g = card.get("general") or {}
    mpi = [m.get("mpi") for m in card.get("mpi") or [] if m.get("mpi")]
    meth = [m.get("title") for m in card.get("meth") or [] if m.get("title")]
    item.update({"valid_from": g.get("valid_from"), "valid_to": g.get("valid_to"), "mpi_months": mpi[0] if mpi else None,
                 "certificate_url": g.get("certificate_url"), "method": meth[0][:200] if meth else None})
    return item


def search(term: str, limit: int, http, head: str = "") -> dict:
    params = {"title": term, "rows": 20}
    r = http.get(f"{EAPI}/mit", params=params)
    res = r.json().get("result") or {}
    items = [dict(brief(it), url=card_url(it.get("mit_uuid"))) for it in res.get("items") or []]
    if head:
        from ..refdata import si_head_ok
        items = [it for it in items if si_head_ok(it["title"], head)]
    for it in items[:min(limit, 2)]:
        try:
            enrich(it, http.get(card_url(it["uuid"])).json())
        except (FetchError, ValueError):
            pass
    today = datetime.now().strftime("%Y-%m-%d")
    valid = [it for it in items[:limit] if not it.get("valid_to") or it["valid_to"] >= today]
    rest = [it for it in items[:limit] if it not in valid]
    return {"total": int(res.get("count") or 0), "items": valid + rest, "url": f"{EAPI}/mit?{urlencode(params)}",
            "source": "ФГИС «Аршин», реестр утверждённых типов СИ"}


class ArshinSource(Source):
    name = "arshin"
    title = "Утверждённые типы средств измерений (ФГИС «Аршин»)"
    filename = "arshin.json.gz"
    compare_fields = ("title", "notation", "uuid")
    field_names = {"title": "наименование", "notation": "обозначение типа", "uuid": "идентификатор"}
    min_records = 500

    def chain(self):
        return [("eapi", "ФГИС «Аршин», открытый API eapi (HTTP)", self._via_http),
                ("eapi_browser", "ФГИС «Аршин», API через браузер (Playwright)", self._via_browser)]

    def _plans(self) -> list[dict]:
        if self.ctx.full:
            return [{"sort": "number asc"}, {"sort": "number desc"}]
        yy = datetime.now().year % 100
        return [{"number": f"*-{yy:02d}"}, {"number": f"*-{(yy - 1) % 100:02d}"}]

    def _collect(self, get_json, prev: dict) -> Fetched:
        records = dict(prev)
        seen = 0
        totals = []
        for plan in self._plans():
            start, total = 0, None
            while start <= MAX_START:
                res = get_json(dict(plan, rows=ROWS, start=start)).get("result") or {}
                total = int(res.get("count") or 0)
                items = res.get("items") or []
                for it in items:
                    b = brief(it)
                    if b["number"]:
                        b["checked_at"] = now_iso()
                        records[b["number"]] = b
                        seen += 1
                if not items or start + ROWS >= total:
                    break
                start += ROWS
            totals.append(f"{plan}: {total}")
        if not seen:
            raise SourceError("реестр не вернул ни одной записи")
        notes = [f"запрошено типов: {seen}; " + "; ".join(totals)]
        if not self.ctx.full:
            notes.append("режим инкремента: типы с номерами текущего и прошлого года; полный реестр: --full")
        return Fetched(records, f"{EAPI}/mit", now_iso()[:10], {"mode": "full" if self.ctx.full else "recent"}, notes)

    def _via_http(self, prev: dict) -> Fetched:
        return self._collect(lambda p: self.http.get(f"{EAPI}/mit", params=p, save=not self.ctx.full).json(), prev)

    def _via_browser(self, prev: dict) -> Fetched:
        if not self.ctx.browser or self.http.offline:
            raise SourceError("браузерный фолбэк выключен")
        with Browser() as br:
            page = br.open(SPA)
            return self._collect(lambda p: br.fetch_json(page, f"{EAPI}/mit?{urlencode(p)}"), prev)

    def brief(self, rec: dict) -> dict:
        return {k: rec.get(k) for k in ("number", "title", "notation")}

    def line_added(self, key, rec):
        return f"новый тип СИ № {key} «{rec.get('title', '')[:100]}»" + (f" ({rec['notation'][:60]})" if rec.get("notation") else "")

    def line_removed(self, key, rec):
        return f"тип СИ № {key} «{rec.get('title', '')[:100]}» больше не отдаётся реестром"
