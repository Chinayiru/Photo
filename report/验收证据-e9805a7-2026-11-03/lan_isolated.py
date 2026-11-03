import asyncio, sys, tempfile
from pathlib import Path
repo = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(repo))
sys.path.insert(0, str(repo / "scripts"))
from PIL import Image
from backend.app.config import Config
import verify_lan_e2e as harness
tmp = Path(tempfile.mkdtemp(prefix="pb-final-lan-"))
lib = tmp / "lib"
lib.mkdir()
for i in range(8):
    Image.new("RGB", (128,96), (i*25,100,90)).save(lib / f"photo-{i}.jpg")
cfg = Config({"server":{"host":"0.0.0.0","port":8912,"panel_http":False},
              "roots":[{"id":"library","path":str(lib),"mode":"readwrite"}],
              "index":{"watch":False,"scan_on_start":True}},
             path=tmp / "data/config.json", data_dir=tmp / "data")
cfg.save()
harness.get_cfg = lambda: cfg
raise SystemExit(harness.main())
