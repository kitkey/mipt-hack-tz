"""Планировщик обновления справочников для сервиса updater в Docker.

Каждые несколько секунд читает control/schedule.json (время, день полного обхода, включено ли, какие справочники;
без файла берутся TZCHECK_UPDATE_AT, TZCHECK_UPDATE_FULL_WEEKDAY, TZCHECK_UPDATE_ENABLED), забирает control/request.json
от кнопки «Обновить сейчас» на вкладке «Справочники» и пишет пульс в control/scheduler.json.
"""
import datetime
import importlib.util
import os
import threading
import time

from . import control
from .control import next_run  # noqa: F401  (старый интерфейс, им пользуются тесты)

TICK = float(os.environ.get("TZCHECK_UPDATE_TICK", "3"))


def _say(msg: str):
    print(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def _environment() -> dict:
    return {"browser": importlib.util.find_spec("playwright") is not None and os.environ.get("TZCHECK_NO_BROWSER") != "1",
            "proxy": bool(os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")),
            "data_dir": control.refdata.data_dir()}


class Scheduler:
    def __init__(self):
        self.planned = None
        self.planned_for = None
        self.settings = control.load_settings()
        self.running = None
        self.env = _environment()
        self.stop = threading.Event()

    def beat(self):
        control.write_heartbeat({"next_at": control.stamp(self.planned) if self.planned else None,
                                 "next_full": bool(self.planned and self.planned.weekday() == self.settings.get("full_weekday")),
                                 "settings": {k: self.settings.get(k) for k in ("enabled", "at", "full_weekday", "sources", "saved_at")},
                                 "running": self.running, **self.env})

    def _beat_loop(self):
        while not self.stop.wait(TICK):
            try:
                self.beat()
            except OSError:
                pass

    def refresh(self, current: datetime.datetime):
        self.settings = control.load_settings()
        key = (self.settings["enabled"], self.settings["at"], self.settings["full_weekday"])
        if key != self.planned_for or self.planned is None:
            if key != self.planned_for and self.planned_for is not None:
                _say(f"новые настройки расписания: {'включено' if key[0] else 'выключено'}, {key[1]}, полный обход "
                     + (control.WEEKDAYS[key[2]] if key[2] >= 0 else "не делать"))
            self.planned = next_run(current, self.settings["at"]) if self.settings["enabled"] else None
            self.planned_for = key
            _say(f"следующее обновление: {self.planned:%Y-%m-%d %H:%M}" if self.planned else "ночное обновление выключено")

    def run(self, req: dict):
        self.running = {"id": req["id"], "sources": req["sources"], "started_at": control.stamp()}
        self.beat()
        _say(f"запуск обновления ({req['trigger']}): {', '.join(req['sources'])}" + (", полный обход" if req.get("full") else ""))
        st = control.execute(req, "updater", echo=True)
        _say(f"обновление завершено, код {st['code']}: {st['outcome']}")
        self.running = None
        self.beat()

    def tick(self, current: datetime.datetime | None = None) -> str | None:
        current = current or datetime.datetime.now()
        self.refresh(current)
        req = control.take_request()
        if req:
            self.run(req)
            return "manual"
        if self.planned and current >= self.planned:
            full = self.planned.weekday() == self.settings.get("full_weekday")
            self.run(control.make_request(self.settings["sources"], full, "schedule"))
            self.planned = None
            return "schedule"
        return None


def main():
    s = Scheduler()
    s.refresh(datetime.datetime.now())
    s.beat()
    threading.Thread(target=s._beat_loop, daemon=True).start()
    if os.environ.get("TZCHECK_UPDATE_ON_START", "0") == "1":
        s.run(control.make_request(s.settings["sources"], False, "start"))
    while True:
        try:
            s.tick()
        except OSError as e:
            _say(f"ошибка планировщика: {e}")
        time.sleep(TICK)


if __name__ == "__main__":
    main()
