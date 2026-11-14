"""Пишет загрузку GPU, VRAM и RAM раз в 0,5 с: python gpu_sampler.py out.csv (остановить: удалить out.csv.run)"""
import subprocess
import sys
import time
from pathlib import Path

out = Path(sys.argv[1])
flag = Path(str(out) + ".run")
flag.write_text("1")
with out.open("w", encoding="utf8") as f:
    f.write("t,vram_mib,gpu_pct,ram_free_mib\n")
    t0 = time.monotonic()
    while flag.exists():
        q = subprocess.run(["nvidia-smi", "--query-gpu=memory.used,utilization.gpu", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True)
        r = subprocess.run(["powershell", "-NoProfile", "-Command", "(Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory"],
                           capture_output=True, text=True) if int(time.monotonic() - t0) % 10 == 0 else None
        ram = str(int(r.stdout.strip()) // 1024) if r and r.stdout.strip().isdigit() else ""
        f.write(f"{time.monotonic() - t0:.1f},{q.stdout.strip().replace(' ', '')},{ram}\n")
        f.flush()
        time.sleep(0.5)
