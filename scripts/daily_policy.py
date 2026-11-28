"""按授权群的业务关系计算日常接收范围，不固定甲乙。"""
import sqlite3,json
from pathlib import Path

def people(a):
    result=set()
    p=Path(a['database'])
    if not p.exists():return result
    with sqlite3.connect('file:'+str(p)+'?mode=ro',uri=True) as c:
        for sender,owners in c.execute('SELECT sender_open_id,assignee_open_ids FROM task_candidate WHERE chat_id=?',(a['chat'],)):
            result.add(sender);result.update(json.loads(owners or '[]'))
        for creator,reviewer in c.execute('SELECT creator_open_id,reviewer_open_id FROM task WHERE chat_id=?',(a['chat'],)):
            result.update((creator,reviewer))
        if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_assignee'").fetchone():
            result.update(r[0] for r in c.execute('SELECT a.open_id FROM task_assignee a JOIN task t ON a.task_id=t.id WHERE t.chat_id=?',(a['chat'],)))
    return result-{None,''}

def valid_message(a,cmd):
    return cmd.chat_type=='group' and cmd.chat_id==a['chat'] and not cmd.is_from_bot and cmd.sender_open_id.startswith('ou_')
