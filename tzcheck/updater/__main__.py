import argparse
import os
import sys
import time

from .. import refdata
from . import changes
from .arshin import ArshinSource
from .base import Context
from .eaeu import EaeuSource
from .gost import GostSource
from .ktru import KtruSource
from .laws import LawsSource
from .okpd2 import Okpd2Source

SOURCES = {"okpd2": Okpd2Source, "laws": LawsSource, "eaeu": EaeuSource, "gost": GostSource, "arshin": ArshinSource, "ktru": KtruSource}
LOCK_TTL = 6 * 3600


def take_lock() -> str | None:
    path = os.path.join(changes.updates_dir(), ".lock")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        if time.time() - os.path.getmtime(path) < LOCK_TTL:
            return None
        os.remove(path)
    except OSError:
        pass
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return None
    os.write(fd, str(os.getpid()).encode())
    os.close(fd)
    return path


def main():
    ap = argparse.ArgumentParser(prog="python -m tzcheck.updater", description="Обновление справочников проверки ТЗ")
    ap.add_argument("--only", default="", help="через запятую: " + ",".join(SOURCES))
    ap.add_argument("--offline", action="store_true", help="без сети: разобрать заново сохранённые ответы из data/raw и пересобрать отчёт")
    ap.add_argument("--full", action="store_true", help="полная выгрузка: весь каталог Росстандарта (около часа) и весь реестр «Аршин»")
    ap.add_argument("--no-browser", action="store_true", help="не запускать Playwright-фолбэк")
    ap.add_argument("--ktru-codes", default="", help="коды ОКПД 2 для КТРУ через запятую вместо списка наблюдения")
    ap.add_argument("--data-dir", default="", help="папка данных вместо tzcheck/data")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    if args.data_dir:
        os.environ["TZCHECK_DATA_DIR"] = os.path.abspath(args.data_dir)
    names = [x.strip() for x in args.only.split(",") if x.strip()] or list(SOURCES)
    bad = [x for x in names if x not in SOURCES]
    if bad:
        ap.error(f"неизвестные источники: {', '.join(bad)}; есть: {', '.join(SOURCES)}")
    lock = take_lock()
    if lock is None:
        print("обновление уже идёт (есть data/updates/.lock), выхожу", file=sys.stderr)
        sys.exit(3)
    ctx = Context(offline=args.offline, full=args.full, browser=not args.no_browser, verbose=args.verbose)
    failed = []
    try:
        for name in [n for n in SOURCES if n in names]:
            src = SOURCES[name](ctx)
            if name == "ktru" and args.ktru_codes:
                src.codes_override = [c.strip() for c in args.ktru_codes.split(",") if c.strip()]
            t0 = time.time()
            run = src.run()
            run["seconds"] = round(time.time() - t0, 1)
            run["mode"] = "offline" if args.offline else ("full" if args.full else "daily")
            changes.write_run(name, src.title, run)
            c = run["counts"]
            head = changes.STATUS_RU.get(run["status"], run["status"]) + ("" if run["status"] == "keep" else f": {run['via_title']}")
            ctx.say(f"[{name}] {head}; записей {c['after']}, +{c['added']} −{c['removed']} изменено {c['changed']}, {run['seconds']} с")
            if run["status"] == "keep":
                failed.append(name)
        path = changes.write_latest(list(SOURCES), {k: v.title for k, v in SOURCES.items()})
        ctx.say(f"отчёт: {path}")
    finally:
        try:
            os.remove(lock)
        except OSError:
            pass
    if failed:
        ctx.say("не обновлены (оставлены текущие файлы): " + ", ".join(failed))
        sys.exit(1)


if __name__ == "__main__":
    main()
