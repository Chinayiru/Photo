from pathlib import Path
import datetime, json, os, subprocess, sys, time
sys.stdout.reconfigure(encoding='utf-8',errors='replace')
OUT=Path(__file__).resolve().parent
name=sys.argv[1];cwd=sys.argv[2];cmd=sys.argv[3:]
env=os.environ.copy();env['PYTHONIOENCODING']='utf-8';env['PYTHONUTF8']='1';env['PYTHONUNBUFFERED']='1'
started=datetime.datetime.now(datetime.timezone.utc).isoformat();t=time.monotonic()
log=OUT/(name+'.log')
with log.open('w',encoding='utf-8') as f:
 try:
  p=subprocess.Popen(cmd,cwd=cwd,env=env,stdout=f,stderr=subprocess.STDOUT)
  rc=p.wait(timeout=1800)
 except subprocess.TimeoutExpired:
  subprocess.run(['taskkill','/PID',str(p.pid),'/T','/F'],stdout=f,stderr=f)
  rc=124
 except Exception as e:
  f.write(type(e).__name__+': '+str(e));rc=125
r={'name':name,'command':cmd,'cwd':cwd,'started_utc':started,'seconds':round(time.monotonic()-t,3),'exit_code':rc,'log':str(log)}
(OUT/(name+'.result.json')).write_text(json.dumps(r,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(r,ensure_ascii=False))
print(log.read_text(encoding='utf-8',errors='replace')[-3500:])
sys.exit(rc)
