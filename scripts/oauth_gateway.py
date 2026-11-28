"""独立白名单网关：仅两个GET授权路径；未绑定受控Agent时一律503。"""
import json,subprocess,os,time,hashlib
from pathlib import Path
import httpx,uvicorn
from fastapi import FastAPI,Request
from fastapi.responses import PlainTextResponse,Response
ROOT=Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0,str(ROOT))
STATE=ROOT/'.local/personal-tunnel'
app=FastAPI(docs_url=None,redoc_url=None,openapi_url=None)
@app.api_route('/{path:path}',methods=['GET','POST','PATCH','DELETE','PUT','OPTIONS','HEAD'])
async def gateway(path:str,request:Request):
    if request.method!='GET' or '/'+path not in ['/calendar/authorize','/calendar/callback']:
        return PlainTextResponse('Not found',status_code=404)
    route=STATE/'route.json'
    try:
        a=json.loads(route.read_text());permit=Path(a['permit']);auth=json.loads(permit.read_text())
        if auth.get('mode')=='autonomous':
            from scripts.live_boundary import Guard
            guard=Guard(permit,a['sha256']);guard.check()
            valid=auth.get('new_oauth') and time.time()<auth.get('oauth_until',0)
        else:valid=auth['start']<=time.time()<auth['end']
        if not valid or hashlib.sha256(permit.read_bytes()).hexdigest()!=a['sha256'] or (permit.parent/'revoked').exists():
            return PlainTextResponse('Unavailable',status_code=503)
        ports=subprocess.run(['lsof','-nP','-a','-p',str(a['agent_pid']),'-iTCP:8766','-sTCP:LISTEN','-t'],capture_output=True,text=True)
        if str(a['agent_pid']) not in ports.stdout.split():return PlainTextResponse('Unavailable',status_code=503)
    except (OSError,ValueError,KeyError,RuntimeError):return PlainTextResponse('Unavailable',status_code=503)
    try:
        async with httpx.AsyncClient(trust_env=False,timeout=20,follow_redirects=False) as client:
            r=await client.get('http://127.0.0.1:8766/'+path,params=request.query_params)
        headers={k:v for k,v in r.headers.items() if k.lower() in ('location','content-type')}
        headers['Cache-Control']='no-store'
        return Response(r.content,status_code=r.status_code,headers=headers)
    except httpx.HTTPError:return PlainTextResponse('Unavailable',status_code=503)
if __name__=='__main__':
    uvicorn.run(app,host='127.0.0.1',port=18766,access_log=False,log_level='error')
