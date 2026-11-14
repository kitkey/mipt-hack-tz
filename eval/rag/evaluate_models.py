"""Compare the same local evaluation across dense, RRF and optional reranker."""
import json,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from tzcheck.rag.models import HybridSearch,MODELS
from evaluate_local import metrics

def main():
 model=sys.argv[1];rerank='--rerank' in sys.argv
 queries=json.loads((ROOT/'eval/rag/questions_local.json').read_text(encoding='utf8')); t=time.monotonic();search=HybridSearch(model,rerank=rerank)
 results={}
 for mode in ('dense','rrf'):
  ranks=[]
  for i,q in enumerate(queries):
   ranks.append(search.search(q['question'],mode=mode))
   if i%50==0: print(model,mode,i,flush=True)
  results[mode]=metrics(queries,ranks)
 results['seconds']=round(time.monotonic()-t,1); results['reranker']=rerank
 dest=ROOT/'eval/rag'/('metrics_'+model.replace('/','--')+('_rerank' if rerank else '')+'.json')
 dest.write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf8'); print(json.dumps(results,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
