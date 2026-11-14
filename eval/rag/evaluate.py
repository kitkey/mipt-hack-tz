"""Evaluate only model-verified questions; scores are diagnostic, not legal validation."""
import json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from tzcheck.rag import Search

def main():
 path=ROOT/'eval/rag/questions.json'
 if not path.exists(): print('Нет проверенных вопросов'); return
 data=json.loads(path.read_text(encoding='utf8')); rows=[x for x in data if 'id' in x and 'question' in x]; search=Search(); ranks=[]
 for q in rows:
  hits=search.search(q['question'],limit=10,threshold=0.01)
  ranks.append(next((i for i,h in enumerate(hits,1) if h['id']==q['id']),None))
 metrics={'questions':len(rows),'generator_errors':len(data)-len(rows)}
 for k in (1,5,10): metrics['recall@'+str(k)]=sum(x is not None and x<=k for x in ranks)/len(rows) if rows else None
 metrics['mrr']=sum(1/x for x in ranks if x)/len(rows) if rows else None
 negatives=['условия строительства марсианской космической станции на спутнике Юпитера', 'правила продажи межзвездных телепортов и тахионных двигателей', 'протокол обмена с инопланетной цивилизацией Андромеды']
 metrics['out_of_domain_n']=len(negatives); metrics['refusal_rate']=sum(not search.search(q,limit=1) for q in negatives)/len(negatives)
 (ROOT/'eval/rag/metrics.json').write_text(json.dumps(metrics,ensure_ascii=False,indent=2),encoding='utf8'); print(json.dumps(metrics,ensure_ascii=False,indent=2))
if __name__=='__main__': main()
