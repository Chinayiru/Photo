"""Supplement only: cold-load navigation, explicitly exclude uncontrolled cross-network block 4n."""
import ast
import sys
from pathlib import Path
repo = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(repo))
sys.path.insert(0, str(repo / "scripts"))
import verify_browser_e2e as harness
Original = harness.Browser
class ColdNavigation(Original):
    def goto(self, url, *, budget=25.0):
        if url.startswith(("http://", "https://")):
            super().goto("about:blank", budget=budget)
        return super().goto(url, budget=budget)
harness.Browser = ColdNavigation
source = Path(harness.__file__).read_text(encoding="utf-8")
tree = ast.parse(source)
main = next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=="main")
def label(n, key):
    return isinstance(n,ast.Expr) and isinstance(n.value,ast.Call) and isinstance(n.value.func,ast.Name) and n.value.func.id=="print" and any(isinstance(a,ast.Constant) and isinstance(a.value,str) and key in a.value for a in n.value.args)
body = next(n.body for n in ast.walk(main) if isinstance(n,ast.Try) and any(label(c,"[4n] 跨网络") for c in n.body))
start=next(i for i,n in enumerate(body) if label(n,"[4n] 跨网络"))
end=next(i for i,n in enumerate(body) if label(n,"[4q] 真机配对"))
body[start:end]=ast.parse('print("[EXPLICITLY NOT EXECUTED] 4n cross-network timeout; original failure retained, cannot infer device result")').body
module=ast.fix_missing_locations(ast.Module(body=[main],type_ignores=[]))
exec(compile(module,harness.__file__,"exec"),harness.__dict__)
print("SUPPLEMENTAL HARNESS: full document navigation; block 4n not executed")
raise SystemExit(harness.main())
