import gzip
import hashlib
import json
import os
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlencode, urlsplit

import requests

from .. import refdata

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
CA_BUNDLE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "certs", "ca_bundle_ru.pem")
DOMAIN_DELAY = {
    "zakupki.gov.ru": float(os.environ.get("TZCHECK_ZAKUPKI_DELAY", "60")),
    "classifikators.ru": 10.0,
    "www.consultant.ru": 5.0,
}
DEFAULT_DELAY = 1.0


class FetchError(Exception):
    pass


class Response:
    def __init__(self, url, status, headers, content, fetched_at, from_store=False):
        self.url = url
        self.status = status
        self.headers = {k.lower(): v for k, v in headers.items()}
        self.content = content
        self.fetched_at = fetched_at
        self.from_store = from_store

    @property
    def text(self) -> str:
        ctype = self.headers.get("content-type", "")
        enc = "utf-8"
        if "charset=" in ctype:
            enc = ctype.split("charset=")[-1].split(";")[0].strip() or "utf-8"
        return self.content.decode(enc, errors="replace")

    def json(self):
        return json.loads(self.text)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Client:
    def __init__(self, source: str, offline: bool = False, store: bool = True, timeout: float = 60, retries: int = 3):
        self.source = source
        self.offline = offline
        self.store = store
        self.timeout = timeout
        self.retries = retries
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": UA, "Accept-Language": "ru,en;q=0.7"})
        self.session.verify = CA_BUNDLE if os.path.exists(CA_BUNDLE) else True
        self.raw_dir = os.path.join(refdata.data_dir(), "raw", source)
        self._last = {}
        self._lock = threading.Lock()
        self.log = []

    def _key(self, method: str, url: str, params) -> str:
        full = url + ("?" + urlencode(sorted(params.items()) if isinstance(params, dict) else params) if params else "")
        return hashlib.sha1(f"{method} {full}".encode()).hexdigest()[:20]

    def _wait(self, host: str):
        delay = DOMAIN_DELAY.get(host, DEFAULT_DELAY)
        with self._lock:
            last = self._last.get(host, 0)
            pause = last + delay - time.monotonic()
            if pause > 0:
                time.sleep(pause)
            self._last[host] = time.monotonic()

    def _index_path(self) -> str:
        return os.path.join(self.raw_dir, "index.json")

    def _load_index(self) -> dict:
        try:
            with open(self._index_path(), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def stored(self, method: str, url: str, params=None) -> Response | None:
        key = self._key(method, url, params)
        meta = self._load_index().get(key)
        if not meta:
            return None
        try:
            with gzip.open(os.path.join(self.raw_dir, key + ".gz"), "rb") as f:
                content = f.read()
        except OSError:
            return None
        return Response(meta["url"], meta["status"], meta.get("headers", {}), content, meta["fetched_at"], True)

    def _save(self, key: str, resp: Response):
        if not self.store:
            return
        os.makedirs(self.raw_dir, exist_ok=True)
        with gzip.open(os.path.join(self.raw_dir, key + ".gz"), "wb") as f:
            f.write(resp.content)
        with self._lock:
            idx = self._load_index()
            keep = {k: resp.headers[k] for k in ("content-type", "etag", "last-modified") if k in resp.headers}
            idx[key] = {"url": resp.url, "status": resp.status, "fetched_at": resp.fetched_at, "sha256": resp.sha256,
                        "size": len(resp.content), "headers": keep}
            tmp = self._index_path() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(idx, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self._index_path())

    def request(self, method: str, url: str, params=None, headers=None, conditional: bool = False, ok=(200,), save: bool = True,
                **kw) -> Response:
        key = self._key(method, url, params)
        if self.offline:
            resp = self.stored(method, url, params)
            if resp is None:
                raise FetchError(f"нет сохранённого ответа для {url} (режим --offline)")
            return resp
        hdrs = dict(headers or {})
        prev = self.stored(method, url, params) if conditional else None
        if prev is not None:
            if prev.headers.get("etag"):
                hdrs["If-None-Match"] = prev.headers["etag"]
            if prev.headers.get("last-modified"):
                hdrs["If-Modified-Since"] = prev.headers["last-modified"]
        host = urlsplit(url).hostname or ""
        err = None
        for attempt in range(self.retries):
            self._wait(host)
            try:
                r = self.session.request(method, url, params=params, headers=hdrs, timeout=self.timeout, **kw)
            except requests.RequestException as e:
                err = f"{type(e).__name__}: {str(e)[:200]}"
                self.log.append({"url": url, "error": err, "at": now_iso()})
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 304 and prev is not None:
                prev.from_store = True
                prev.status = 304
                return prev
            resp = Response(r.url, r.status_code, dict(r.headers), r.content, now_iso())
            if r.status_code in ok:
                if method == "GET" and save:
                    self._save(key, resp)
                return resp
            err = f"HTTP {r.status_code}"
            self.log.append({"url": url, "error": err, "at": now_iso()})
            if r.status_code in (400, 401, 403, 404, 410):
                break
            time.sleep(2 ** attempt)
        raise FetchError(f"{url}: {err}")

    def get(self, url: str, params=None, **kw) -> Response:
        return self.request("GET", url, params=params, **kw)

    def head(self, url: str, **kw) -> Response:
        return self.request("HEAD", url, allow_redirects=True, **kw)
