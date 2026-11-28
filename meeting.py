"""会议写表链路；默认关闭，使用独立账本，不直接创建日程。"""
import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from meeting_config import target
class Invalid(ValueError): pass


def parse(cmd, bot, now=None):
    people = {m.key:m.open_id for m in cmd.mentions if m.key}
    if not bot or bot not in people.values(): raise Invalid('请从菜单真实@机器人')
    text = cmd.text_raw
    for key, value in people.items():
        if value == bot: text = text.replace(key, '')
    match = re.fullmatch(r'\s*会议\s+主题=(.+?)\s+时间=(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\s+时长=(\d+)\s+参会人=(.+?)\s*',text)
    if not match: raise Invalid('格式：会议 主题=内容 时间=YYYY-MM-DD HH:mm 时长=60 参会人=真实@成员')
    title, start, duration, participants = match.groups()
    if not title.strip() or len(title)>200: raise Invalid('主题需为1至200字')
    try: start = datetime.strptime(start,'%Y-%m-%d %H:%M').replace(tzinfo=ZoneInfo('Asia/Shanghai'))
    except ValueError: raise Invalid('开始时间格式或日期无效') from None
    if start <= (now or datetime.now(ZoneInfo('Asia/Shanghai'))): raise Invalid('开始时间必须晚于当前时间')
    duration = int(duration)
    if not 1 <= duration <= 1440: raise Invalid('时长需为1至1440分钟')
    owners=[]
    for key in re.split(r'[,，]\s*',participants.strip()):
        oid=people.get(key.strip(),'')
        if not re.fullmatch(r'ou_[A-Za-z0-9]+',oid) or oid==bot: raise Invalid('参会人必须从@菜单选择普通成员，不能填写昵称或@全体')
        if oid not in owners: owners.append(oid)
    return {'议题':title.strip(),'开始时间':int(start.timestamp()*1000),'结束时间':int((start+timedelta(minutes=duration)).timestamp()*1000),'负责人':[{'id':p} for p in owners],'来源群':cmd.chat_id,'原始消息ID':cmd.message_id,'状态':'待进日历'}


class Ledger:
    def __init__(self,path):
        self.path=str(path)
        binding=json.dumps(target(),sort_keys=True,ensure_ascii=False)
        Path(path).parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        with self.connect() as c:
            bound=c.execute("SELECT 1 FROM sqlite_master WHERE name='meeting_target'").fetchone()
            previous=c.execute('SELECT binding FROM meeting_target').fetchone() if bound else None
            existing=c.execute("SELECT 1 FROM sqlite_master WHERE name='meeting'").fetchone()
            populated=existing and c.execute('SELECT 1 FROM meeting LIMIT 1').fetchone()
            if (previous and previous[0]!=binding) or (not previous and populated):
                raise Invalid('会议账本目标不匹配；保留旧账本并使用新目标专用账本')
            c.execute('CREATE TABLE IF NOT EXISTS meeting_target(binding TEXT NOT NULL)')
            if not previous:c.execute('INSERT INTO meeting_target VALUES (?)',(binding,))
            c.execute('CREATE TABLE IF NOT EXISTS meeting(key TEXT PRIMARY KEY, token TEXT, payload TEXT, status TEXT, record TEXT, created REAL)')
        Path(path).chmod(0o600)
    def connect(self): return sqlite3.connect(self.path,timeout=5)
    def claim(self,key,fields):
        payload=hashlib.sha256(json.dumps(fields,sort_keys=True).encode()).hexdigest()
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            old=c.execute('SELECT token,payload,status,record FROM meeting WHERE key=?',(key,)).fetchone()
            if old:
                if old[1]!=payload: raise Invalid('同一消息内容发生变化，需人工核查')
                return None,old[2],old[3]
            token=str(uuid.uuid4())
            c.execute('INSERT INTO meeting VALUES (?,?,?,?,?,?)',(key,token,payload,'unknown',None,time.time()))
            return token,'unknown',None
    def finish(self,key,status,record=None):
        with self.connect() as c:c.execute('UPDATE meeting SET status=?,record=? WHERE key=?',(status,record,key))


class SDKTable:
    def __init__(self):
        config=target(require_ready=True)
        self.app,self.table=config['app_token'],config['table_id']
    def create(self,fields,token):
        from feishu_api import get_client
        from lark_oapi.api.bitable.v1 import CreateAppTableRecordRequest,AppTableRecord
        request=(CreateAppTableRecordRequest.builder().app_token(self.app).table_id(self.table)
                 .user_id_type('open_id').client_token(token)
                 .request_body(AppTableRecord.builder().fields(fields).build()).build())
        response=get_client().bitable.v1.app_table_record.create(request)
        if not response.success(): raise Invalid('飞书写表拒绝，错误码='+str(response.code))
        return response.data.record.record_id

    def get(self, record):
        from feishu_api import get_client
        from lark_oapi.api.bitable.v1 import GetAppTableRecordRequest
        req=(GetAppTableRecordRequest.builder().app_token(self.app).table_id(self.table)
             .record_id(record).user_id_type('open_id').build())
        response=get_client().bitable.v1.app_table_record.get(req)
        if not response.success():raise Invalid('查询被拒绝，错误码='+str(response.code))
        return response.data.record.fields


def submit(cmd,bot,scope,ledger,transport,now=None):
    if cmd.chat_id!=scope or cmd.chat_type!='group' or cmd.is_from_bot: raise Invalid('群或消息身份不在范围')
    if not cmd.message_id or not cmd.sender_open_id.startswith('ou_'): raise Invalid('缺少可靠消息或发起人身份')
    fields=parse(cmd,bot,now)
    key=hashlib.sha256((os.getenv('FEISHU_APP_ID','')+'|'+os.getenv('FEISHU_TENANT_KEY','')+'|'+scope+'|'+cmd.message_id).encode()).hexdigest()
    token,status,record=ledger.claim(key,fields)
    if token is None:return '已提交，请勿重复发送' if status=='submitted' else '上次提交结果待人工核查，未重复新增'
    try:
        record=transport.create(fields,token)
        if not record: raise RuntimeError('missing record')
    except Exception:
        # 任何发送或落账不确定都保留unknown；不自动重试。
        raise Invalid('写表结果未确认，已隔离；请人工核查，不要重复发送') from None
    ledger.finish(key,'submitted',record)
    from meeting_jobs import watch
    watch(ledger,key,cmd)
    return '会议申请已提交，日程及视频会议待自动化处理'


def route(cmd):
    if os.getenv('MEETING_ENABLED','0')!='1':return False
    if not os.getenv('LIVE_AUTHORIZATION_FILE'):raise Invalid('会议必须使用受控授权入口')
    bot=os.getenv('FEISHU_BOT_OPEN_ID','')
    text=cmd.text_raw
    for m in cmd.mentions:
        if m.open_id==bot:text=text.replace(m.key,'')
    if not re.match(r'^\s*会议(?:\s|$)',text):return False
    scope=os.getenv('MEETING_CHAT_ID','')
    if cmd.chat_id!=scope or cmd.chat_type!='group' or cmd.is_from_bot or not bot or not any(m.open_id==bot for m in cmd.mentions):return True
    import feishu_api
    try:
        result=submit(cmd,bot,os.getenv('MEETING_CHAT_ID',''),Ledger(os.environ['MEETING_DB']),SDKTable())
    except (Invalid, ValueError) as e:result=str(e)
    feishu_api.send_text_to_user(cmd.sender_open_id,result,message_key='meeting-reply:'+cmd.message_id)
    return True
