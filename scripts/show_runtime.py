"""Only redacted audit stages and resource counters; incremental follow."""
import json,sqlite3,sys,time
from pathlib import Path
root=Path(__file__).resolve().parents[1]
seen={};cursor=0;current=None

def show():
 global cursor,current
 active=root/'.local/active-live.json'
 if not active.exists():
  if not seen.get('empty'):print('No active session; offline logs: .local/offline/');seen['empty']=True
  return
 a=json.loads(active.read_text());d=Path(a['directory'])
 p=root/'.local/autonomous/budget.sqlite3' if a.get('mode')=='autonomous' else d/'budget.sqlite3'
 changed=current!=str(d)
 if changed:current=str(d);cursor=0;seen.clear();print('Mode:',a.get('mode','live'))
 if p.exists():
  with sqlite3.connect('file:'+str(p)+'?mode=ro',uri=True) as c:
   if changed:
    print('Cumulative:',dict(c.execute('select kind,n from counts')))
    if a.get('mode')=='autonomous' and c.execute("select 1 from sqlite_master where name='rolling'").fetchone():
     print('Last minute:',dict(c.execute('select kind,count(*) from rolling where at>? group by kind',(time.time()-60,))))
   if cursor==0:rows=list(reversed(list(c.execute('select rowid,stage,kind,status,error from audit order by rowid desc limit 20'))))
   else:rows=list(c.execute('select rowid,stage,kind,status,error from audit where rowid>? order by rowid limit 200',(cursor,)))
   for row in rows:cursor=row[0];print(*row[1:])
 for name in ['ALERT.json','STOPPED']:
  if (d/name).exists():
   content=(d/name).read_text()
   if seen.get(name)!=content:print(name,content);seen[name]=content

if __name__=='__main__':
 try:
  while True:
   show()
   if '--follow' not in sys.argv:break
   time.sleep(2)
 except KeyboardInterrupt:pass
