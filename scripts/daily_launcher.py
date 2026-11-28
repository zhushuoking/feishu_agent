"""后台启动同一受控宿主；不自动生成批准、不重置预算。"""
import sys,subprocess,time,json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.live_boundary import Guard
from scripts.mac_live import scan_consumers
ROOT=Path(__file__).resolve().parents[1]
def launch(path,sha):
    g=Guard(path,sha)
    if g.a.get('mode')!='daily':raise SystemExit('Not a daily authorization')
    if scan_consumers():raise SystemExit('Consumer already running')
    d=Path(path).parent
    if (d/'STOPPED').exists() or (d/'READY').exists():raise SystemExit('Used session; renew explicitly, keep old budget/audit')
    p=subprocess.Popen([sys.executable,str(ROOT/'scripts/mac_live.py'),str(Path(path).resolve()),sha],cwd=ROOT,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
    for _ in range(250):
        if p.poll() is not None or (d/'STOPPED').exists():raise SystemExit('Startup failed; inspect private audit, do not restart blindly')
        if (d/'READY').exists():
            (ROOT/'.local/daily-selection.json').write_text(json.dumps({'path':str(Path(path).resolve()),'sha256':sha}))
            print('LIVE READY (background); status: ./status.sh; stop: ./stop.sh');return
        time.sleep(.2)
    p.terminate();p.wait(timeout=15);raise SystemExit('READY timeout; host stopped')
if __name__=='__main__':launch(*sys.argv[1:])
