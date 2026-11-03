import sys, tempfile
from pathlib import Path
repo = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(repo))
sys.path.insert(0, str(repo / "scripts"))
import verify_exe
# Existing script recursively resets a fixed H:/tmp directory; replace only its test location.
verify_exe.WORK = Path(tempfile.mkdtemp(prefix="pb-final-exe-")) / "work"
assert not verify_exe.WORK.exists()
raise SystemExit(verify_exe.main())
