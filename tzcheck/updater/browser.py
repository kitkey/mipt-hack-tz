import json
import os

from .net import UA, FetchError

DISABLED = os.environ.get("TZCHECK_NO_BROWSER") == "1"


def _playwright():
    if DISABLED:
        raise FetchError("браузер отключён (TZCHECK_NO_BROWSER=1)")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise FetchError("playwright не установлен: pip install playwright && python -m playwright install chromium") from e
    return sync_playwright


class Browser:
    def __init__(self, wait_ms: int = 3000):
        self.wait_ms = wait_ms
        self._pw = None
        self._browser = None
        self._ctx = None

    def __enter__(self):
        sync_playwright = _playwright()
        self._pw = sync_playwright().start()
        try:
            self._browser = self._pw.chromium.launch(headless=True)
        except Exception as e:
            self._pw.stop()
            raise FetchError(f"не запустился chromium: {str(e)[:200]} (python -m playwright install chromium)") from e
        self._ctx = self._browser.new_context(user_agent=UA, locale="ru-RU", ignore_https_errors=True)
        return self

    def __exit__(self, *exc):
        for x in (self._ctx, self._browser):
            try:
                x.close()
            except Exception:
                pass
        if self._pw:
            self._pw.stop()

    def open(self, url: str, timeout_ms: int = 60000):
        page = self._ctx.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        except Exception as e:
            page.close()
            raise FetchError(f"браузер не открыл {url}: {str(e)[:200]}") from e
        page.wait_for_timeout(self.wait_ms)
        return page

    def html(self, url: str) -> str:
        page = self.open(url)
        try:
            return page.content()
        finally:
            page.close()

    def fetch_text(self, page, url: str, headers_expr: str = "{}") -> str:
        js = f"async () => {{ const r = await fetch({json.dumps(url)}, {{headers: {headers_expr}}}); return [r.status, await r.text()]; }}"
        try:
            status, text = page.evaluate(js)
        except Exception as e:
            raise FetchError(f"fetch в браузере не удался: {str(e)[:200]}") from e
        if status != 200:
            raise FetchError(f"fetch в браузере: HTTP {status} ({url[:120]})")
        return text

    def fetch_json(self, page, url: str, headers_expr: str = "{}") -> dict:
        return json.loads(self.fetch_text(page, url, headers_expr))
