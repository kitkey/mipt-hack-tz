"""Скорость по строкам движка Strata: python engine_stats.py <лог движка> <с какой строки>"""
import re
import sys

rows = open(sys.argv[1], encoding="utf-8", errors="replace").read().splitlines()[int(sys.argv[2]) if len(sys.argv) > 2 else 0:]
pat = re.compile(r"prompt (\d+) tokens = (\d+) reused \+ (\d+) read in (\d+) ms .*?, (\d+) generated in (\d+) ms(?:.*?drafts accepted (\d+) of (\d+))?")
P = R = PM = G = GM = DA = DT = 0
n = 0
for r in rows:
    m = pat.search(r)
    if not m:
        continue
    n += 1
    p, reused, read, pms, g, gms = map(int, m.groups()[:6])
    P += p; R += read; PM += pms; G += g; GM += gms
    if m.group(7):
        DA += int(m.group(7)); DT += int(m.group(8))
print(f"requests {n}; prompt tokens {P} (read {R}) in {PM/1000:.0f} s = {R/(PM/1000) if PM else 0:.0f} tok/s; "
      f"generated {G} in {GM/1000:.0f} s = {G/(GM/1000) if GM else 0:.1f} tok/s; drafts {DA}/{DT}")
