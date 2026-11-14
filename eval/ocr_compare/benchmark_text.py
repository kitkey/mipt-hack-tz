"""Reproducible CPU baseline. Run from repository root; no model calls."""
import collections, difflib, json, os, re, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ['TZCHECK_REFDATA_OFFLINE'] = '1'
from tzcheck.parse import parse
try:
    from tzcheck.pipeline import check
    pipeline_error = None
except Exception as exc:
    check = None
    pipeline_error = repr(exc)
import pymupdf

PAIRS = [('lift_src','samples/pdf/lift_src.pdf'), ('metro_src','samples/pdf/metro_src.pdf'), ('lift_fin','data_raw/pdf_test/lift_fin.pdf'), ('metro_fin','data_raw/pdf_test/metro_fin.pdf')]
def norm(s): return re.sub(r'\s+', ' ', s.casefold()).strip()
def distance(a,b):
    # Myers bit-vector Levenshtein, exact for arbitrary hashable sequences.
    if not a: return len(b)
    masks={}
    for i,c in enumerate(a): masks[c]=masks.get(c,0)|(1<<i)
    pv=(1<<len(a))-1; mv=0; score=len(a); top=1<<(len(a)-1); mask=(top<<1)-1
    for c in b:
        eq=masks.get(c,0); xv=eq|mv; xh=(((eq&pv)+pv)^pv)|eq
        ph=mv|~(xh|pv); mh=pv&xh
        score += bool(ph&top)-bool(mh&top)
        ph=((ph<<1)|1)&mask; mh=(mh<<1)&mask
        pv=(mh|~(xv|ph))&mask; mv=ph&xv
    return score

def tokens(s): return re.findall(r'\w+|[^\w\s]',norm(s))
def multirecall(a,b):
    x,y=collections.Counter(a),collections.Counter(b); n=sum(x.values())
    return {'matched':sum((x&y).values()), 'reference':n, 'candidate':sum(y.values())}
def findings(a,b):
    edges=[]
    for i,x in enumerate(a):
        for j,y in enumerate(b):
            if x['category']==y['category']:
                q=difflib.SequenceMatcher(None,norm(x['quote']),norm(y['quote']),autojunk=False).ratio()
                if q>=0.7: edges.append((q,i,j))
    useda=set(); usedb=set()
    for q,i,j in sorted(edges,reverse=True):
        if i not in useda and j not in usedb: useda.add(i); usedb.add(j)
    return {'matched':len(useda),'reference':len(a),'candidate':len(b),'method':'same category and quote similarity >= 0.7, greedy one-to-one'}
def dedupe_vmerge(doc, docx_path=None):
    """Эталон DOCX повторяет объединённую по вертикали ячейку в каждой строке таблицы (так задумано для правил),
    а в PDF она напечатана один раз. Для CER ячейка-продолжение вертикального объединения (тот же w:tc,
    что и строкой выше, по python-docx) из эталона убирается."""
    cont = {}
    if docx_path:
        import docx as _docx
        d = _docx.Document(docx_path)
        for ti, t in enumerate(d.tables):
            rows = t.rows
            for ri in range(1, len(rows)):
                try:
                    cur, up = rows[ri].cells, rows[ri - 1].cells
                except Exception:
                    continue
                seen = set()
                for k, c in enumerate(cur):
                    if k < len(up) and c._tc is up[k]._tc and id(c._tc) not in seen:
                        seen.add(id(c._tc))
                        cont.setdefault((ti, ri), []).append(" ".join(c.text.split()))
    lines = []
    for b in doc.blocks:
        if b.kind == 'row':
            drop = list(cont.get((b.table, b.row), []))
            cells = []
            for c in b.cells:
                if c and " ".join(c.split()) in drop:
                    drop.remove(" ".join(c.split()))
                    continue
                cells.append(c)
            lines.append(' | '.join(c for c in cells if c))
        else:
            lines.append(b.text)
    return chr(10).join(lines)
def main():
    results=[]
    for name,pdf in PAIRS:
        refpath=f'samples/{name}.docx'; t=time.perf_counter(); ref=parse(refpath); doc=parse(pdf); elapsed=time.perf_counter()-t
        ref_text=dedupe_vmerge(ref, refpath); rt,dt=norm(ref_text),norm(doc.text()); rw,dw=tokens(ref_text),tokens(doc.text()); rt_full=norm(ref.text())
        cells=lambda d:[norm(c) for b in d.blocks if b.kind=='row' for c in b.cells if norm(c)]
        nums=lambda s:re.findall(r'(?<!\w)\d+(?:[.,]\d+)*(?!\w)',s)
        codes=lambda s:re.findall(r'(?<!\d)\d{2}\.\d{2}\.\d{2}\.\d{3}(?!\d)',s)
        try:
            ra=check(refpath,use_llm=False) if check else None; rb=check(pdf,use_llm=False) if check else None
            finding_result=findings(ra['findings'],rb['findings']) if check else {'error':pipeline_error}
        except Exception as exc:
            finding_result={'error':repr(exc)}
        pages=len(pymupdf.open(pdf))
        results.append({'name':name,'pages':pages,'parser_pair_seconds_including_docx':round(elapsed,3),'cer':distance(rt_full,dt)/len(rt_full),'wer':distance(tokens(ref.text()),dw)/len(tokens(ref.text())),'cells_exact_multiset':multirecall(cells(ref),cells(doc)),'numbers_exact_multiset':multirecall(nums(rt),nums(dt)),'okpd_exact_multiset':multirecall(codes(rt),codes(dt)),'findings':finding_result})
        print(name, flush=True)
    Path('eval/ocr_compare/results_text.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
if __name__=='__main__': main()
