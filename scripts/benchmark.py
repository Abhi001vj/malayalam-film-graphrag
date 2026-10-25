"""Read-only local corpus benchmark; excludes HTTP, auth and network latency."""
import argparse,json,statistics,time,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from filmgraph.db import connect
from filmgraph.search import search

p=argparse.ArgumentParser();p.add_argument('--db',default='data/films.sqlite3');p.add_argument('--runs',type=int,default=20);args=p.parse_args()
questions=['How many movies did Mohanlal and Mammootty work together in?','Mohanlal actor and Priyadarshan director','Sreenivasan writer and Mohanlal actor','K J Yesudas singer','Manichitrathazhu']
timings=[];plans=[];counts={}
with connect(args.db) as con:
 for i in range(args.runs):
  q=questions[i%len(questions)];start=time.perf_counter();r=search(con,q,limit=5);timings.append((time.perf_counter()-start)*1000);counts[q]=r['total']
  start=time.perf_counter();search(con,q,limit=5,query_plan=r['query_plan']);plans.append((time.perf_counter()-start)*1000)
def stats(values):
 values=sorted(values);return {'median_ms':round(statistics.median(values),2),'p95_ms':round(values[min(len(values)-1,int(len(values)*.95))],2)}
print(json.dumps({'runs':args.runs,'server_parser':stats(timings),'validated_plan':stats(plans),'sample_counts':counts,'scope':'Local, sequential, read-only core retrieval. Not production capacity; excludes HTTP/auth/quota/network/cold starts.'},indent=2))
