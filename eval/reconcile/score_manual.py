"""Score manual labels; abort if generated candidates no longer match annotation."""
import csv
import os
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tzcheck.parse import parse_many
from tzcheck.reconcile import reconcile
EIS = Path(os.environ.get("TZCHECK_EIS_DIR", Path(__file__).resolve().parents[2] / "data_raw" / "eis_more"))
labels = list(csv.DictReader(Path(__file__).with_name("manual_labels.csv").open(encoding="utf-8-sig", newline="")))
expected = {(r["файл"],r["блок"],r["цитата"]):r for r in labels}
actual = {}
for source in sorted(EIS.glob("*.docx"))[:3]:
    findings = reconcile(parse_many([source]))
    for f in findings:
        actual[(source.name, f.block_id, f.quote)] = f
    rows = [r for r in labels if r["файл"] == source.name]
    good = sum(r["оценка"] == "верно" for r in rows)
    print(source.name, "кандидаты",len(findings),"верно",good,"precision", f"{good}/{len(rows)}" if rows else "не определён (0 кандидатов)")
if set(actual) != set(expected):
    print("Разметка устарела, различия:",set(actual) ^ set(expected))
    raise SystemExit(1)
good = sum(r["оценка"] == "верно" for r in labels)
print("ИТОГО:",good,"/",len(labels),"=",f"{good/len(labels):.1%}" if labels else "не определён")
