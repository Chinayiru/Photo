import os,sys,tempfile
from pathlib import Path
repo=Path(sys.argv[1]).resolve()
import pytest
with tempfile.TemporaryDirectory(prefix='cross-volume-',dir=Path(__file__).resolve().parent) as td:
 assert Path(td).drive.lower()!=repo.drive.lower()
 os.environ['TEMP']=td
 raise SystemExit(pytest.main([str(repo/'backend/tests/test_sandbox.py')+'::test_cross_volume_replace_fails_with_winerror_17','-q','--junitxml='+str(Path(__file__).resolve().parent/'cross-volume.xml')]))
