import argparse, asyncio, base64, json, sys, tempfile, time
from pathlib import Path
repo=Path(sys.argv[1]).resolve();out=Path(__file__).resolve().parent
sys.path[:0]=[str(repo),str(repo/'scripts')]
from soak_tunnel import Soak,ahttp_json
from verify_browser_e2e import Browser,find_browser,free_port
from PIL import Image

def browse(soak,ping):
 result={};br=None
 try:
  br=Browser(find_browser(),soak.tmp/'browser',free_port());br.connect()
  for name in ['Page.enable','Runtime.enable','Network.enable']:br.remember(name)
  br.remember('Emulation.setDeviceMetricsOverride',width=390,height=844,deviceScaleFactor=2,mobile=True)
  br.enable_touch()
  base=f'http://127.0.0.1:{soak.lan_port}'
  br.goto(base+'/')
  store={'pb.token':soak.client_token,'pb.device_id':ping['device_id'],'pb.base':base,'pb.cloud':base,'pb.via':'lan'}
  br.evaluate('(()=>{const s='+json.dumps(store)+';for(const [k,v] of Object.entries(s))localStorage.setItem(k,v);return true;})()')
  br.goto('about:blank');br.goto(base+'/')
  assert br.wait_for("document.querySelectorAll('[data-id]').length>=3",budget=25)
  result['timeline_cells']=br.evaluate("document.querySelectorAll('[data-id]').length")
  result['status']=br.evaluate("document.querySelector('.conn')?.innerText")
  br.evaluate("document.querySelector('[data-id]').click()")
  assert br.wait_for("document.querySelector('.v-img.main')?.naturalWidth>0",budget=20)
  time.sleep(2)
  br.drain()
  def raw_requests():
   br.drain()
   return [e['params']['request']['url'].split('?')[0] for e in br._events if e.get('method')=='Network.requestWillBeSent' and '/raw' in e['params']['request']['url']]
  result['raw_requests_before_zoom']=raw_requests()
  def zoom():
   p=br.evaluate("(()=>{const r=document.querySelector('.stage').getBoundingClientRect();return {x:r.left+r.width/2,y:r.top+r.height/2}})()")
   for i in range(2):
    br.touch('touchStart',[(p['x'],p['y'])]);br.touch('touchEnd',[]);time.sleep(.10)
  zoom()
  result['raw_decoded_after_click']=br.wait_for("document.querySelector('.v-img.raw')?.naturalWidth>0",budget=15)
  result['overlay_transform_equal']=br.evaluate("getComputedStyle(document.querySelector('.v-img.raw')).transform===getComputedStyle(document.querySelector('.v-img.main')).transform")
  result['raw_requests_after_click']=raw_requests()
  (out/'lan-viewer-original.png').write_bytes(base64.b64decode(br.send('Page.captureScreenshot',format='png')['data']))
  result['main_transform']=br.evaluate("document.querySelector('.v-img.main').style.transform")
  result['raw_src']=br.evaluate("document.querySelector('.v-img.raw')?.getAttribute('src')")
  assert result['raw_decoded_after_click'] and result['overlay_transform_equal']
  result['passed']=True
 finally:
  if br:br.close()
  (out/'lan-viewer-results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
 return result
async def main():
 with tempfile.TemporaryDirectory(prefix='pb-final-cloud-ui-',ignore_cleanup_errors=True) as td:
  soak=Soak(argparse.Namespace(workdir=td,photos=8,rounds_requests=2))
  try:
   await soak.start_cloud();await soak.start_agent();await soak.wait_tunnel();await soak.pair()
   status,ping=await ahttp_json(f'http://127.0.0.1:{soak.cloud_port}/d/{soak.device_id}/api/system/ping')
   assert status==200
   result=await asyncio.to_thread(browse,soak,ping)
   print(json.dumps(result,ensure_ascii=False,indent=2))
  finally:await soak.stop()
asyncio.run(main())
