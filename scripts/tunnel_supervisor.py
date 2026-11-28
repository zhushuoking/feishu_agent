"""有界临时隧道，仅转发独立白名单网关；关闭时回收两个子进程。"""
import json,os,signal,subprocess,time,re,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
STATE=ROOT/'.local/personal-tunnel'
def supervise():
    os.umask(0o077);STATE.mkdir(parents=True,exist_ok=True,mode=0o700)
    gateway=subprocess.Popen([sys.executable,str(ROOT/'scripts/oauth_gateway.py')],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    tunnel=None
    def stop(*_):raise SystemExit(0)
    signal.signal(signal.SIGTERM,stop)
    try:
        import requests
        for _ in range(50):
            if gateway.poll() is not None:raise RuntimeError('gateway exited')
            try:
                r=requests.get('http://127.0.0.1:18766/not-allowed',timeout=1)
                if r.status_code==404:break
            except requests.RequestException:pass
            time.sleep(.1)
        else:raise RuntimeError('gateway not ready')
        check=subprocess.run(['lsof','-nP','-a','-p',str(gateway.pid),'-iTCP:18766','-sTCP:LISTEN','-t'],capture_output=True,text=True)
        if str(gateway.pid) not in check.stdout.split():raise RuntimeError('gateway listener mismatch')
        logpath=STATE/'cloudflared.log'
        with logpath.open('ab') as log:
            tunnel=subprocess.Popen([str(ROOT/'.local/bin/cloudflared'),'tunnel','--no-autoupdate','--protocol','http2','--url','http://127.0.0.1:18766'],stdin=subprocess.DEVNULL,stdout=log,stderr=log)
        (STATE/'process.json').write_text(json.dumps({'tunnel_pid':tunnel.pid,'gateway_pid':gateway.pid,'supervisor_pid':os.getpid()}))
        while tunnel.poll() is None and gateway.poll() is None:
            if time.time()>=json.loads((STATE/'lease.json').read_text())['end']:break
            if logpath.stat().st_size>1024*1024:break
            text=logpath.read_text(errors='replace');matches=re.findall(r'https://[a-z0-9-]+\.trycloudflare\.com',text)
            if matches:(STATE/'endpoint.json').write_text(json.dumps({'base_url':matches[-1],'tunnel_pid':tunnel.pid,'gateway_pid':gateway.pid,'supervisor_pid':os.getpid(),'edge_connected':'Registered tunnel connection' in text}))
            time.sleep(1)
    except BaseException as e:
        (STATE/'supervisor-error.json').write_text(json.dumps({'error_type':type(e).__name__,'reason':str(e) if isinstance(e,RuntimeError) else ''}))
    finally:
        for p in [tunnel,gateway]:
            if p is not None and p.poll() is None:p.terminate()
        for p in [tunnel,gateway]:
            if p is not None:
                try:p.wait(timeout=5)
                except subprocess.TimeoutExpired:p.kill();p.wait()
        (STATE/'STOPPED').write_text(json.dumps({'stopped':True}))
if __name__=='__main__':supervise()
