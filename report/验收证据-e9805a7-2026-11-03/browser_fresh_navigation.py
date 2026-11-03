"""Run unchanged browser assertions, forcing QR navigation to load a new document."""
import sys
from pathlib import Path
repo = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(repo))
sys.path.insert(0, str(repo / "scripts"))
import verify_browser_e2e as harness
Original = harness.Browser


class FreshNavigation(Original):
    def goto(self, url, *, budget=25.0):
        if "#" in url:
            super().goto("about:blank", budget=budget)
        return super().goto(url, budget=budget)


harness.Browser = FreshNavigation
raise SystemExit(harness.main())
