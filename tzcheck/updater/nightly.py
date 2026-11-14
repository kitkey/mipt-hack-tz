"""Планировщик обновления справочников для сервиса updater в Docker: каждый день в TZCHECK_UPDATE_AT, раз в неделю полный обход."""
import datetime
import os
import subprocess
import sys
import time


def _run(args: list[str]) -> int:
    print(f"{datetime.datetime.now():%Y-%m-%d %H:%M} запуск обновления {' '.join(args)}".strip(), flush=True)
    code = subprocess.call([sys.executable, "-m", "tzcheck.updater", *args])
    print(f"{datetime.datetime.now():%Y-%m-%d %H:%M} обновление завершено, код {code}", flush=True)
    return code


def next_run(now: datetime.datetime, at: str) -> datetime.datetime:
    h, m = (int(x) for x in at.split(":"))
    nxt = now.replace(hour=h, minute=m, second=0, microsecond=0)
    return nxt if nxt > now else nxt + datetime.timedelta(days=1)


def main():
    at = os.environ.get("TZCHECK_UPDATE_AT", "04:30")
    full_weekday = int(os.environ.get("TZCHECK_UPDATE_FULL_WEEKDAY", "6"))
    if os.environ.get("TZCHECK_UPDATE_ON_START", "0") == "1":
        _run([])
    while True:
        nxt = next_run(datetime.datetime.now(), at)
        print(f"следующее обновление: {nxt:%Y-%m-%d %H:%M}", flush=True)
        while datetime.datetime.now() < nxt:
            time.sleep(min(600.0, max(1.0, (nxt - datetime.datetime.now()).total_seconds())))
        _run(["--full"] if nxt.weekday() == full_weekday else [])


if __name__ == "__main__":
    main()
