import argparse
import sys

from .pipeline import check
from .report import write_all


def main():
    ap = argparse.ArgumentParser(prog="tzcheck", description="Проверка ТЗ на закупку по 223-ФЗ")
    ap.add_argument("files", nargs="+", help="основное ТЗ и приложения: docx, doc, pdf, xlsx, rtf или txt")
    ap.add_argument("-o", "--out", default="out", help="папка для отчётов")
    ap.add_argument("--no-llm", action="store_true", help="только правила, без языковой модели")
    ap.add_argument("--orchestrator", choices=["linear", "graph"], default=None,
                    help="linear: линейный конвейер (по умолчанию); graph: тот же конвейер на LangGraph с повторной проверкой замечаний с низкой уверенностью")
    args = ap.parse_args()
    path = args.files[0]
    res = check(args.files, use_llm=False if args.no_llm else None, orchestrator=args.orchestrator)
    if res["status"] != "ok":
        print(f"{path}: ошибка: {res['error']}", file=sys.stderr)
        sys.exit(1)
    paths = write_all(res, path, args.out)
    print(f"{path}: замечаний {len(res['findings'])}, режим {res['mode']}, оркестрация {res.get('orchestrator', '')}, {res['seconds']} с -> {paths}")



if __name__ == "__main__":
    main()
