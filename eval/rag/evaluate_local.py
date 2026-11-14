"""Measure rank metrics by document+clause, plus false positives on absent answers."""
import json,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from tzcheck.rag import Search,norm_document

def metrics(rows,ranked):
 pos=[(q,ranked[i]) for i,q in enumerate(rows) if q['origin'] in ('real_reference','synthetic_title')]
 neg=[(q,ranked[i]) for i,q in enumerate(rows) if q['origin'] in ('absent_standard','out_of_domain')]
 ranks=[]
 for q,hits in pos:
  ranks.append(next((i for i,h in enumerate(hits,1) if norm_document(h['document'])==norm_document(q['document']) and h['number']==q['number']),None))
 return {'positive_n':len(pos),'real_reference_n':sum(q['origin']=='real_reference' for q,_ in pos),'synthetic_title_n':sum(q['origin']=='synthetic_title' for q,_ in pos),'recall@1':sum(x==1 for x in ranks)/len(ranks) if ranks else None,'recall@5':sum(x is not None and x<=5 for x in ranks)/len(ranks) if ranks else None,'recall@10':sum(x is not None and x<=10 for x in ranks)/len(ranks) if ranks else None,'mrr':sum(1/x for x in ranks if x)/len(ranks) if ranks else None,'negative_n':len(neg),'refusal_accuracy':sum(not h for _,h in neg)/len(neg) if neg else None,'false_answer_n':sum(bool(h) for _,h in neg)}

def main():
 q=json.loads((ROOT/'eval/rag/questions_local.json').read_text(encoding='utf8'));s=Search(); start=time.monotonic(); ranked=[]
 for i,x in enumerate(q):
  ranked.append(s.search(x['question'],limit=10))
  if i%50==0: print('queries',i,'/',len(q),flush=True)
 m={'bm25':metrics(q,ranked),'seconds':round(time.monotonic()-start,1)}
 (ROOT/'eval/rag/metrics_local.json').write_text(json.dumps(m,ensure_ascii=False,indent=2),encoding='utf8'); print(json.dumps(m,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
