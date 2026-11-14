import gzip
import hashlib
import json
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
DAY = 86400
TTL = {"gost": 7 * DAY, "gost_miss": DAY, "arshin": 7 * DAY, "mirror_gost": 14 * DAY}
INACTIVE = {"Заменён", "Отменён", "Утратил силу в РФ", "Истёк срок действия"}
LATIN_TO_CYR = str.maketrans("ABCEHKMOPTXaceopx", "АВСЕНКМОРТХасеорх")
_mirrors: dict = {}
_down: dict = {}
_lock = threading.Lock()


def data_dir() -> str:
    return os.environ.get("TZCHECK_DATA_DIR") or DATA_DIR


def offline() -> bool:
    return os.environ.get("TZCHECK_REFDATA_OFFLINE", "") == "1"


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _age(iso: str | None) -> float:
    if not iso:
        return float("inf")
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds()
    except ValueError:
        return float("inf")


def norm_designation(s: str) -> str:
    s = (s or "").replace(" ", " ").replace("–", "-").replace("—", "-").replace("№", " ")
    s = re.sub(r"\s+", " ", s).strip()
    m = re.match(r"^([^\d]*)(.*)$", s)
    prefix, rest = m.group(1), m.group(2)
    tokens = []
    for t in prefix.split():
        if re.search(r"[А-ЯЁа-яё]", t) or t in ("P", "p"):
            t = t.translate(LATIN_TO_CYR)
        tokens.append(t.upper())
    prefix = " ".join(tokens)
    prefix = re.sub(r"^ГОСТР\b", "ГОСТ Р", prefix)
    rest = re.sub(r"\s*-\s*", "-", rest.upper())
    return f"{prefix} {rest}".strip()


def _mirror(name: str):
    path = os.path.join(data_dir(), name)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}
    with _lock:
        hit = _mirrors.get(path)
        if hit and hit[0] == mtime:
            return hit[1]
    try:
        opener = gzip.open if path.endswith(".gz") else open
        with opener(path, "rt", encoding="utf-8") as f:
            data = json.load(f)
        recs = data.get("records", {}) if isinstance(data, dict) else {}
    except (OSError, ValueError):
        recs = {}
    with _lock:
        _mirrors[path] = (mtime, recs)
    return recs


def _cache_path(ns: str, key: str) -> str:
    h = hashlib.sha1(key.encode("utf-8")).hexdigest()[:24]
    return os.path.join(data_dir(), "cache", ns, h + ".json")


def cache_get(ns: str, key: str) -> dict | None:
    try:
        with open(_cache_path(ns, key), encoding="utf-8") as f:
            item = json.load(f)
        return item if item.get("key") == key else None
    except (OSError, ValueError):
        return None


def cache_put(ns: str, key: str, value):
    path = _cache_path(ns, key)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"key": key, "checked_at": now_iso(), "value": value}, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except OSError:
        pass


def cache_keys(ns: str) -> list[str]:
    folder = os.path.join(data_dir(), "cache", ns)
    out = []
    try:
        names = os.listdir(folder)
    except OSError:
        return out
    for n in names:
        if not n.endswith(".json"):
            continue
        try:
            with open(os.path.join(folder, n), encoding="utf-8") as f:
                out.append(json.load(f)["key"])
        except (OSError, ValueError, KeyError):
            continue
    return out


def _host_down(host: str) -> bool:
    return _down.get(host, 0) > time.monotonic()


def _mark_down(host: str, seconds: int = 300):
    _down[host] = time.monotonic() + seconds


def _client(source: str):
    from .updater.net import Client
    return Client(source, store=False, timeout=12, retries=1)


def _gost_out(rec: dict, origin: str, stale: bool = False) -> dict:
    out = dict(rec)
    out["origin"] = origin
    out["stale"] = stale
    out["inactive"] = rec.get("status") in INACTIVE
    return out


def gost_status(designation: str) -> dict | None:
    try:
        key = norm_designation(designation)
        if not re.search(r"\d", key):
            return None
        rec = _mirror("gost.json.gz").get(key)
        if rec and _age(rec.get("checked_at")) < TTL["mirror_gost"]:
            return _gost_out(rec, "mirror")
        hit = cache_get("gost", key)
        if hit is not None:
            ttl = TTL["gost"] if hit.get("value") else TTL["gost_miss"]
            if _age(hit.get("checked_at")) < ttl:
                return _gost_out(hit["value"], "cache") if hit.get("value") else None
        if offline() or _host_down("gost"):
            if rec:
                return _gost_out(rec, "mirror", True)
            return _gost_out(hit["value"], "cache", True) if hit and hit.get("value") else None
        from .updater import gost
        try:
            val = gost.lookup(key, _client("gost"))
        except Exception:
            _mark_down("gost")
            if rec:
                return _gost_out(rec, "mirror", True)
            return _gost_out(hit["value"], "cache", True) if hit and hit.get("value") else None
        cache_put("gost", key, val or None)
        return _gost_out(val, "network") if val else None
    except Exception:
        return None


SI_STOP = {"для", "по", "на", "с", "со", "и", "или", "в", "из", "от", "до", "под", "при", "типа", "тип", "шт", "мм", "не", "менее", "более"}
SI_GENERIC = {"набор", "прибор", "приборы", "устройство"}
SI_SKIP = ("комплект", "комплекс", "систем", "стенд", "установк", "оборудован", "набор", "оснастк", "инструмент")
SI_BROAD = ("измерител", "датчик", "преобразовател", "анализатор", "счетчик", "счётчик", "индикатор", "калибратор", "генератор", "источник")
SI_ADJ = ("ый", "ий", "ой", "ая", "яя", "ое", "ее", "ые", "ие", "ых", "их", "ого", "его", "ому", "ему", "ыми", "ими", "ую", "юю")
SI_ENDINGS = ("ами", "ями", "ого", "его", "ому", "ему", "ыми", "ими", "ая", "яя", "ое", "ее", "ые", "ие", "ый", "ий", "ой", "ых", "их",
              "ов", "ев", "ам", "ям", "ах", "ях", "ом", "ем", "а", "я", "ы", "и", "у", "ю", "е", "о", "ь", "й")


def _stem(w: str) -> str:
    for e in SI_ENDINGS:
        if w.lower().endswith(e) and len(w) - len(e) >= 4:
            return w[:-len(e)]
    if w.lower().endswith(("ов", "ев")) and len(w) - 2 >= 3:
        return w[:-2]
    return w


def _adjective(w: str) -> bool:
    return len(w) > 4 and w.lower().endswith(SI_ADJ)


def si_head_ok(title: str, head: str) -> bool:
    return any(w.lower().startswith(head.lower()) for w in re.findall(r"[А-ЯЁа-яё-]+", title or "")[:2])


def si_terms(query: str, anchor: str = "") -> list[tuple[str, str, str]]:
    text = re.split(r"[(:;,•\n]|\s(?:и|или|а также)\s", query or "", maxsplit=1)[0]
    words = [m for m in re.finditer(r"[А-ЯЁа-яёA-Za-z-]{2,}", text) if m.group(0).lower() not in SI_STOP]
    if anchor:
        idx = next((i for i, m in enumerate(words) if anchor.lower()[:5] in m.group(0).lower()), None)
        if idx is None:
            return []
        words = words[idx:]
    while len(words) > 1 and words[0].group(0).lower() in SI_GENERIC:
        words = words[1:]
    words = [m for m in words if not re.search(r"[A-Za-z]", m.group(0))][:3]
    h = next((i for i, m in enumerate(words) if not _adjective(m.group(0))), None)
    if h is None or words[h].group(0).lower().startswith(SI_SKIP) or words[h].group(0).lower() in SI_GENERIC:
        return []
    stems = [_stem(m.group(0)) for m in words]
    head = stems[h]
    if len(head) < 3:
        return []
    least = h + 2 if words[h].group(0).lower().startswith(SI_BROAD) else h + 1
    out = []
    for n in range(len(stems), least - 1, -1):
        t = "*" + "*".join(stems[:n]) + "*"
        if t not in [x[0] for x in out]:
            out.append((t, text[words[0].start():words[n - 1].end()], head))
    return out


def _arshin_local(term: str, limit: int, head: str = "") -> dict | None:
    recs = _mirror("arshin.json.gz")
    if not recs:
        return None
    rx = re.compile(".*".join(re.escape(p) for p in term.strip("*").split("*")), re.I)
    found = [r for r in recs.values() if rx.search(r.get("title", ""))]
    items = [r for r in found if not head or si_head_ok(r.get("title", ""), head)]
    items.sort(key=lambda r: r.get("number", ""), reverse=True)
    return {"total": len(found), "items": items[:limit], "local": True}


def _arshin_fallback(hit: dict | None, term: str, limit: int, head: str = ""):
    if hit and hit.get("value"):
        return hit["value"], hit.get("checked_at")
    local = _arshin_local(term, limit, head)
    if not local:
        return None, None
    try:
        with open(os.path.join(data_dir(), "arshin_meta.json"), encoding="utf-8") as f:
            checked = json.load(f).get("checked_at")
    except (OSError, ValueError):
        checked = None
    return local, checked


def si_type(query: str, anchor: str = "", limit: int = 3) -> list[dict]:
    try:
        for term, words, head in si_terms(query, anchor):
            key = f"{term}|{head}"
            hit = cache_get("arshin", key)
            if hit is not None and _age(hit.get("checked_at")) < TTL["arshin"]:
                res, checked = hit.get("value"), hit.get("checked_at")
            elif offline() or _host_down("arshin"):
                res, checked = _arshin_fallback(hit, term, limit, head)
            else:
                from .updater import arshin
                try:
                    res, checked = arshin.search(term, limit, _client("arshin"), head), now_iso()
                    cache_put("arshin", key, res)
                except Exception:
                    _mark_down("arshin")
                    res, checked = _arshin_fallback(hit, term, limit, head)
            if res and res.get("items"):
                return [dict(it, total=res["total"], search=term, query=words, checked_at=checked, local=bool(res.get("local")))
                        for it in res["items"][:limit]]
        return []
    except Exception:
        return []


def tr_lists_for(designation: str) -> list[dict]:
    try:
        key = norm_designation(designation)
        if not re.search(r"\d", key):
            return []
        out = []
        for rec in _mirror("eaeu.json.gz").values():
            if rec.get("kind") != "list":
                continue
            for row in rec.get("rows", []):
                if row.get("key") == key:
                    out.append({"tr": rec.get("tr"), "list_id": rec.get("list_id"), "edition": rec.get("edition"),
                                "element": row.get("element"), "status_text": row.get("status_text"), "url": rec.get("url"),
                                "checked_at": rec.get("checked_at")})
        return out
    except Exception:
        return []


def law_edition(act: str) -> dict | None:
    try:
        return _mirror("laws.json").get(act)
    except Exception:
        return None


def ktru_items(okpd2_code: str) -> list[dict]:
    try:
        recs = _mirror("ktru.json.gz")
        items = [r for r in recs.values() if r.get("kind") == "position" and (r.get("okpd2") or "").startswith(okpd2_code)]
        if not items:
            path = os.path.join(data_dir(), "cache", "ktru_requested.json")
            try:
                with open(path, encoding="utf-8") as f:
                    asked = json.load(f)
            except (OSError, ValueError):
                asked = {}
            if okpd2_code not in asked:
                asked[okpd2_code] = now_iso()
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(asked, f, ensure_ascii=False, indent=1)
        return items
    except Exception:
        return []


def msk(iso: str | None, with_time: bool = False) -> str:
    if not iso:
        return "?"
    try:
        dt = datetime.fromisoformat(iso).astimezone(timezone(timedelta(hours=3)))
    except ValueError:
        return iso
    return dt.strftime("%d.%m.%Y %H:%M") if with_time else dt.strftime("%d.%m.%Y")
