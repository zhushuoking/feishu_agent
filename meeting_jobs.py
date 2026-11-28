"""会议回写有界查询及一次性最终通知；只查本地已提交的record_id。"""
import json, sqlite3, time
from urllib.parse import urlsplit


def init(ledger):
    with ledger.connect() as c:
        c.execute('CREATE TABLE IF NOT EXISTS meeting_watch(key TEXT PRIMARY KEY, sender TEXT, chat TEXT, message TEXT, tries INTEGER DEFAULT 0, next_at REAL, deadline REAL, state TEXT DEFAULT "waiting", reply TEXT)')


def watch(ledger,key,cmd):
    init(ledger)
    with ledger.connect() as c:
        c.execute('INSERT OR IGNORE INTO meeting_watch(key,sender,chat,message,next_at,deadline) VALUES (?,?,?,?,?,?)',(key,cmd.sender_open_id,cmd.chat_id,cmd.message_id,time.time()+30,time.time()+600))


def result_text(fields):
    # 文本字段可能由API返回字符串或文本片段；不把普通日程链接冒充视频链接。
    def text(name):
        v=fields.get(name,'')
        if isinstance(v,list):return ''.join(x.get('text','') for x in v if isinstance(x,dict))
        return v if isinstance(v,str) else ''
    if text('状态')!='已进日历' or not text('日程ID') or not text('日历ID'):return None
    value=fields.get('会议链接','')
    url=value.get('link','') if isinstance(value,dict) else text('会议链接')
    url=url if isinstance(url,str) else ''
    u=urlsplit(url)
    if u.scheme=='https' and u.hostname=='vc.feishu.cn' and u.path.startswith('/j/'):
        return '会议已创建（依据自动化回写）；视频会议：'+url
    return '日程已创建（依据自动化回写），但未取得有效视频会议链接；请核查自动化。'


def tick(ledger,transport,sender,now=None):
    init(ledger);now=time.time() if now is None else now
    with ledger.connect() as c:
        # 上次发送结果未知不重发；上次查询中断也停止自动处理。
        rows=c.execute('SELECT w.key,m.record,w.sender,w.tries,w.deadline FROM meeting_watch w JOIN meeting m ON m.key=w.key WHERE w.state="waiting" AND m.status="submitted" AND w.next_at<=? ORDER BY w.next_at LIMIT 1',(now,)).fetchall()
    for key,record,person,tries,deadline in rows:
        with ledger.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            if c.execute('UPDATE meeting_watch SET state="checking",tries=tries+1 WHERE key=? AND state="waiting"',(key,)).rowcount!=1:continue
        reply=None
        if tries>=6 or now>=deadline:reply='会议申请已提交，但未在限定时间取得自动化完成结果，请查表；不会重复创建。'
        else:
            try:reply=result_text(transport.get(record))
            except Exception:
                with ledger.connect() as c:c.execute('UPDATE meeting_watch SET state="query_unknown" WHERE key=?',(key,))
                continue
        if reply is None:
            with ledger.connect() as c:c.execute('UPDATE meeting_watch SET state="waiting",next_at=? WHERE key=?',(now+60,key))
            continue
        with ledger.connect() as c:c.execute('UPDATE meeting_watch SET state="notify_unknown",reply=? WHERE key=?',(reply,key))
        # 发送前已落账；崩溃或None不盲目重复通知。
        try:mid=sender(person,reply,message_key='meeting-final:'+key)
        except Exception:mid=None
        with ledger.connect() as c:c.execute('UPDATE meeting_watch SET state=? WHERE key=?',('notified' if mid else 'notify_unknown',key))
