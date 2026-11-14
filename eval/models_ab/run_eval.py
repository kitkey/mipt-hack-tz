"""Run the standard label evaluation, recording document and API metrics."""
import argparse
import json
import os
import runpy
import sys
import time
from pathlib import Path
from threading import Lock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tzcheck import pipeline

ap = argparse.ArgumentParser()
ap.add_argument('--name', required=True)
ap.add_argument('--llm', action='store_true')
a = ap.parse_args()
out = ROOT / 'eval' / 'models_ab'
records, calls, mutex = [], [], Lock()
original_check = pipeline.check
if a.llm:
    import litellm
    original_completion = litellm.completion
    def measured_completion(**kwargs):
        started = time.monotonic()
        rec = {}
        try:
            resp = original_completion(**kwargs)
            usage = resp.usage
            rec.update(prompt_tokens=usage.prompt_tokens, completion_tokens=usage.completion_tokens,
                       finish_reason=resp.choices[0].finish_reason)
            return resp
        except Exception as exc:
            rec['error'] = type(exc).__name__
            raise
        finally:
            rec['seconds'] = round(time.monotonic() - started, 3)
            with mutex:
                calls.append(rec)
    litellm.completion = measured_completion

def measured_check(path, *args, **kwargs):
    result = original_check(path, *args, **kwargs)
    records.append({k: result.get(k) for k in ('file', 'status', 'mode', 'seconds', 'warnings', 'findings', 'stats')})
    (out / (a.name + '_documents.json')).write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding='utf8')
    (out / (a.name + '_calls.json')).write_text(json.dumps(calls, ensure_ascii=False, indent=2), encoding='utf8')
    return result
pipeline.check = measured_check
sys.argv = ['eval/eval_labels.py', '--out', str(out / (a.name + '.json'))] + (['--llm'] if a.llm else [])
runpy.run_path(str(ROOT / 'eval' / 'eval_labels.py'), run_name='__main__')
