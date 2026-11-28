"""Offline workbench profile for the existing Mirror; not registered in production."""
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo

TZ=ZoneInfo('Asia/Shanghai')
APP='EXAMPLE_RESOURCE_0'
TABLE='example_fixture1'
BASE_FIELDS={'任务名称':1,'任务描述':1,'优先级':3,'状态':3,'截止日期':5}
ADDITIONS={'Agent任务ID':1,'负责人':11,'发起人':11,'来源群':1,'原始消息ID':1,'日程ID':1,'日历ID':1,'创建时间':5,'完成时间':5}
STATES={'TODO':'待办','NEEDS_INFO':'待补充','IN_PROGRESS':'进行中','BLOCKED':'阻塞','PENDING_ACCEPTANCE':'待验收','DONE':'已完成','CANCELLED':'已取消'}
PRIORITIES={'normal':'中优先级','urgent':'高优先级'}


def verify_schema(fields):
    actual={f['field_name']:f for f in fields}
    errors=[]
    for name,kind in dict(BASE_FIELDS,**ADDITIONS).items():
        if name not in actual:errors.append('缺少字段:'+name)
        elif actual[name]['type']!=kind:errors.append('字段类型不符:'+name)
    for name,expected in [('状态',set(STATES.values())),('优先级',set(PRIORITIES.values()))]:
        options={o['name'] for o in actual.get(name,{}).get('property',{}).get('options',[])}
        if not expected<=options:errors.append('缺少选项:'+name+':'+','.join(sorted(expected-options)))
    if errors:raise ValueError(';'.join(errors))


def millis(value):
    if not value:return None
    d=datetime.fromisoformat(value) if isinstance(value,str) else value
    if d.tzinfo is None:raise ValueError('工作台时间必须带时区')
    return int(d.timestamp()*1000)


def map_task(task,calendar,owners,title_formatter):
    """Called after the existing Mirror confirms all owner calendars at task.version."""
    if task['status'] not in STATES or task['priority'] not in PRIORITIES:raise ValueError('未知状态或优先级')
    owners=list(dict.fromkeys(owners))
    if not owners or any(not p.startswith('ou_') for p in owners):raise ValueError('缺少可靠负责人')
    if not task.get('creator_open_id','').startswith('ou_'):raise ValueError('缺少可靠发起人')
    if not calendar.get('日程ID') or not calendar.get('日历ID'):raise ValueError('尚未确认个人日历')
    if task.get('uid') is None or not task.get('message_id'):raise ValueError('缺少稳定来源标识')
    text=task.get('description') or ''
    for label,key in [('交付物','deliverable'),('验收标准','acceptance_criteria')]:
        if task.get(key):text+=('\n' if text else '')+label+'：'+task[key]
    return {'任务名称':title_formatter(task['title']),'任务描述':text,'优先级':PRIORITIES[task['priority']],
            '状态':STATES[task['status']],'截止日期':millis(task['deadline_at']),
            'Agent任务ID':task['uid'],'负责人':[{'id':p} for p in owners],
            '发起人':[{'id':task['creator_open_id']}],'来源群':task['chat_id'],'原始消息ID':task['message_id'],
            '日程ID':calendar['日程ID'],'日历ID':calendar['日历ID'],
            '创建时间':millis(task['created_at']),**({'完成时间':millis(task['completed_at'])} if task.get('completed_at') else {})}


def recovery_filter(fields):
    if not fields.get('Agent任务ID'):raise ValueError('禁止按标题查重')
    return {'conjunction':'and','conditions':[{'field_name':name,'operator':'is','value':[fields[name]]}
            for name in ('Agent任务ID','原始消息ID','来源群')]}


def metrics(records,now,verified_current_open_id):
    """Display only. Must receive a platform-verified identity; never an ACL substitute."""
    if not verified_current_open_id or not verified_current_open_id.startswith('ou_'):raise ValueError('尚未核验当前用户')
    if now.tzinfo is None:raise ValueError('时间必须带时区')
    now=now.astimezone(TZ);today=now.replace(hour=0,minute=0,second=0,microsecond=0)
    week=today-timedelta(days=today.weekday())
    result=dict(today=0,soon=0,week_done=0,overdue=0,in_progress=0)
    for record in records:
        f=record.get('fields',record)
        if verified_current_open_id not in [p['id'] for p in f.get('负责人',[])]:continue
        state=f.get('状态');active=state not in ('已完成','已取消')
        deadline=datetime.fromtimestamp(f['截止日期']/1000,TZ) if f.get('截止日期') else None
        done=datetime.fromtimestamp(f['完成时间']/1000,TZ) if f.get('完成时间') else None
        result['today']+=bool(active and deadline and today<=deadline<today+timedelta(days=1))
        result['soon']+=bool(active and deadline and now<=deadline<now+timedelta(days=3))
        result['overdue']+=bool(active and deadline and deadline<now)
        result['week_done']+=bool(state=='已完成' and done and week<=done<=now)
        result['in_progress']+=state=='进行中'
    return result


def target_for(task_id,existing_mapping,cutover_id):
    """Keep an existing record's location; only unmirrored post-cutover tasks use workbench."""
    if existing_mapping:return existing_mapping['app_token'],existing_mapping['table_id'],existing_mapping['record_id']
    if task_id<=cutover_id:return None
    return APP,TABLE,None
