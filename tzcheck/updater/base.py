import gzip
import hashlib
import json
import os
import traceback
from dataclasses import dataclass, field

from .. import refdata
from .net import Client, FetchError, now_iso


class SourceError(Exception):
    pass


@dataclass
class Fetched:
    records: dict
    url: str
    version: str | None = None
    meta: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)
    unchanged: bool = False


@dataclass
class Context:
    offline: bool = False
    full: bool = False
    browser: bool = True
    verbose: bool = False

    def say(self, msg: str):
        print(msg, flush=True)


def write_atomic(path: str, data: bytes):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def write_json(path: str, obj, indent=1):
    write_atomic(path, json.dumps(obj, ensure_ascii=False, indent=indent).encode("utf-8"))


class Source:
    name = ""
    title = ""
    filename = ""
    compare_fields: tuple = ()
    field_names: dict = {}
    min_records = 0
    drop_guard = 0.5

    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.http = Client(self.name, offline=ctx.offline)

    @property
    def path(self) -> str:
        return os.path.join(refdata.data_dir(), self.filename)

    @property
    def meta_path(self) -> str:
        base = self.filename.split(".")[0]
        return os.path.join(refdata.data_dir(), f"{base}_meta.json")

    def chain(self) -> list:
        return []

    def load(self) -> dict:
        try:
            opener = gzip.open if self.path.endswith(".gz") else open
            with opener(self.path, "rt", encoding="utf-8") as f:
                return self.from_file(json.load(f))
        except (OSError, ValueError):
            return {}

    def load_meta(self) -> dict:
        try:
            with open(self.meta_path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def from_file(self, data) -> dict:
        return data.get("records", {}) if isinstance(data, dict) else {}

    def to_file(self, records: dict):
        return {"source": self.name, "records": records}

    def serialize(self, records: dict) -> bytes:
        raw = json.dumps(self.to_file(records), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return gzip.compress(raw, mtime=0) if self.filename.endswith(".gz") else raw

    def health(self, got: Fetched, prev: dict) -> str | None:
        n = len(got.records)
        if n < self.min_records:
            return f"записей {n}, ожидалось не меньше {self.min_records}: похоже, сломался разбор или источник"
        if prev and len(prev) >= 20 and n < len(prev) * self.drop_guard:
            return f"записей {n} против {len(prev)} в текущем файле: резкое падение, данные не применены"
        return None

    def fields_for(self, rec: dict) -> tuple:
        return self.compare_fields or tuple(k for k in rec if k not in ("checked_at", "fetched_at"))

    def diff(self, prev: dict, new: dict) -> dict:
        added = sorted(k for k in new if k not in prev)
        removed = sorted(k for k in prev if k not in new)
        changed = []
        for k in sorted(new.keys() & prev.keys()):
            a, b = prev[k], new[k]
            fields = {f: [a.get(f), b.get(f)] for f in self.fields_for(b) if not self.same(f, a.get(f), b.get(f))}
            if fields:
                changed.append({"key": k, "fields": fields})
        return {"added": added, "removed": removed, "changed": changed}

    def same(self, field: str, a, b) -> bool:
        return a == b

    def brief(self, rec: dict) -> dict:
        return rec

    def line_added(self, key: str, rec: dict) -> str:
        name = rec.get("name") or rec.get("title") or rec.get("n") or ""
        return f"добавлено: {key}" + (f" «{name[:120]}»" if name else "")

    def line_removed(self, key: str, rec: dict) -> str:
        name = rec.get("name") or rec.get("title") or rec.get("n") or ""
        return f"удалено: {key}" + (f" «{name[:120]}»" if name else "")

    def line_changed(self, key: str, fields: dict, old: dict, new: dict) -> str:
        parts = []
        for f, (a, b) in fields.items():
            label = self.field_names.get(f, f)
            parts.append(f"{label}: «{_short(a)}» → «{_short(b)}»")
        return f"{key}: " + "; ".join(parts)

    def run(self) -> dict:
        prev = self.load()
        prev_meta = self.load_meta()
        errors = []
        got, via, via_title = None, "keep", "оставлен текущий файл"
        for link_id, link_title, fn in self.chain():
            self.ctx.say(f"[{self.name}] {link_title}…")
            try:
                res = fn(prev)
            except (FetchError, SourceError) as e:
                errors.append({"link": link_id, "title": link_title, "error": str(e)})
                self.ctx.say(f"[{self.name}]   не удалось: {e}")
                continue
            except Exception as e:
                errors.append({"link": link_id, "title": link_title, "error": f"{type(e).__name__}: {e}"})
                self.ctx.say(f"[{self.name}]   ошибка: {type(e).__name__}: {e}")
                if self.ctx.verbose:
                    traceback.print_exc()
                continue
            problem = self.health(res, prev)
            if problem:
                errors.append({"link": link_id, "title": link_title, "error": problem})
                self.ctx.say(f"[{self.name}]   отклонено: {problem}")
                continue
            got, via, via_title = res, link_id, link_title
            break
        at = now_iso()
        run = {"at": at, "via": via, "via_title": via_title, "errors": errors, "http_errors": self.http.log[-30:],
               "version_before": prev_meta.get("source_version") or prev_meta.get("version")}
        if got is None:
            meta = dict(prev_meta)
            meta.update({"source": self.name, "title": self.title, "checked_at": at, "last_status": "keep",
                         "last_errors": errors, "chain": [c[0] for c in self.chain()] + ["keep"]})
            write_json(self.meta_path, meta)
            run.update({"status": "keep", "counts": {"before": len(prev), "after": len(prev), "added": 0, "removed": 0, "changed": 0},
                        "version_after": run["version_before"], "lines": [], "notes": []})
            return run
        d = self.diff(prev, got.records)
        data = self.serialize(got.records)
        sha = hashlib.sha256(data).hexdigest()
        meta = {k: v for k, v in prev_meta.items() if k in self.keep_meta_keys()}
        meta.update({
            "source": self.name, "title": self.title, "file": self.filename, "via": via, "via_title": via_title,
            "chain": [c[0] for c in self.chain()] + ["keep"], "source_url": got.url, "source_version": got.version,
            "fetched_at": at, "checked_at": at, "records_count": len(got.records), "sha256": sha,
            "last_status": "ok", "last_errors": errors, "notes": got.notes,
        })
        meta.update(got.meta)
        if d["added"] or d["removed"] or d["changed"] or not os.path.exists(self.path):
            write_atomic(self.path, data)
        write_json(self.meta_path, meta)
        lines = [self.line_added(k, got.records[k]) for k in d["added"]]
        lines += [self.line_removed(k, prev[k]) for k in d["removed"]]
        lines += [self.line_changed(c["key"], c["fields"], prev[c["key"]], got.records[c["key"]]) for c in d["changed"]]
        run.update({
            "status": "updated" if (d["added"] or d["removed"] or d["changed"]) else "unchanged",
            "source_url": got.url, "version_after": got.version, "notes": got.notes,
            "counts": {"before": len(prev), "after": len(got.records), "added": len(d["added"]),
                       "removed": len(d["removed"]), "changed": len(d["changed"])},
            "added": [{"key": k, "record": self.brief(got.records[k])} for k in d["added"]],
            "removed": [{"key": k, "record": self.brief(prev[k])} for k in d["removed"]],
            "changed": d["changed"], "lines": lines,
        })
        return run

    def keep_meta_keys(self) -> set:
        return set()


def _short(v) -> str:
    if v is None or v == "":
        return "—"
    if isinstance(v, (list, dict)):
        s = json.dumps(v, ensure_ascii=False)
    else:
        s = str(v)
    return s if len(s) <= 140 else s[:137] + "…"
