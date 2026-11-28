"""个人日历传输范围；复用原版OAuth和同步，不允许历史日程。"""
import json,sqlite3
from urllib.parse import urlsplit,unquote
from scripts.live_boundary import Denied

def classify(a,url,body,method):
    u=urlsplit(str(url));method=(method or 'GET').upper()
    if not a.get('personal_calendar'):return None
    if u.netloc=='accounts.feishu.cn' and u.path=='/oauth/v3/token':
        if method!='POST' or not isinstance(body,dict) or body.get('client_id')!=a['app_id']:raise Denied('OAuth client outside scope')
        grant=body.get('grant_type')
        if grant=='authorization_code' and body.get('redirect_uri')==a['oauth_base_url']+'/calendar/callback':return 'oauth'
        if grant=='refresh_token':return 'oauth'
        raise Denied('OAuth grant outside scope')
    if u.netloc!='open.feishu.cn':return None
    if u.path=='/open-apis/authen/v1/user_info' and method=='GET':return 'oauth'
    if u.path=='/open-apis/calendar/v4/calendars/primary' and method=='POST':return 'calendar_read'
    prefix='/open-apis/calendar/v4/calendars/'
    if not u.path.startswith(prefix):return None
    parts=[unquote(x) for x in u.path[len(prefix):].split('/')]
    if len(parts) not in (2,3) or parts[1]!='events':raise Denied('calendar API outside scope')
    cal=parts[0];eid=parts[2] if len(parts)==3 else None
    with sqlite3.connect('file:'+a['database']+'?mode=ro',uri=True) as c:
        rows=c.execute('SELECT p.event_id FROM personal_calendar_event p JOIN task t ON p.task_uid=t.uid WHERE t.chat_id=? AND t.id>? AND p.calendar_id=?',(a['chat'],a['calendar_cutoff'],cal)).fetchall()
    if not rows:raise Denied('calendar not mapped to new authorized task')
    if eid is None and method=='POST':return 'calendar_create'
    if eid is not None and eid in {r[0] for r in rows if r[0]}:
        if method=='GET':return 'calendar_read'
        if method=='PATCH':return 'calendar_update'
        if method=='DELETE':return 'calendar_delete'
    raise Denied('event outside scope')
