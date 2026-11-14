import argparse
from . import build, Search

def main():
 p=argparse.ArgumentParser(); sub=p.add_subparsers(dest='cmd',required=True); sub.add_parser('build'); q=sub.add_parser('search'); q.add_argument('query'); q.add_argument('--limit',type=int,default=5); a=p.parse_args()
 if a.cmd=='build': print('Фрагментов:',build())
 else:
  import json
  print(json.dumps(Search().search(a.query,limit=a.limit),ensure_ascii=False,indent=2))
if __name__=='__main__': main()
