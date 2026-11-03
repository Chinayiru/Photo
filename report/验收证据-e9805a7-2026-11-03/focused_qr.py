from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import hashlib
import json
import sys
import tempfile
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
REPO = Path(sys.argv[1]).resolve()
OUT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))
from PIL import Image
from backend.app import auth, fsopen
from backend.app.config import Config
from verify_acceptance_e2e import Svc, call, pair
from verify_browser_e2e import Browser, find_browser


def main():
    results = {}
    with tempfile.TemporaryDirectory(prefix="pb-final-focused-", ignore_cleanup_errors=True) as td:
        tmp = Path(td)
        lib = tmp / "lib"
        lib.mkdir()
        for i in range(60):
            im = Image.new("RGB", (256, 192), (i * 3 % 255, i * 7 % 255, 110))
            im.save(lib / f"valid-{i:02d}.jpg", quality=90)
        (lib / "corrupt.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"invalid" * 30)
        cfg = Config({
            "server": {"host": "127.0.0.1", "port": 0, "panel_http": True},
            "roots": [{"id": "library", "path": str(lib), "mode": "readwrite"}],
            "index": {"scan_on_start": False, "watch": False},
        }, path=tmp / "data" / "config.json", data_dir=tmp / "data")
        cfg.save()
        svc = Svc(cfg)
        svc.start()
        browser = None
        try:
            st = svc.st
            asyncio.run(st.indexer.scan_all())
            token = pair(svc.base, st, "final-focused")
            _, page, _ = call(svc.base, "/api/timeline?limit=100", token=token)
            valid = [x["id"] for x in page["items"] if x["name"].startswith("valid-")]
            assert len(valid) == 60

            def fetch(mid):
                status, blob, headers = call(svc.base, f"/api/items/{mid}/thumb?size=320", token=token, raw=True)
                return {"id": mid, "status": status, "bytes": len(blob),
                        "webp": blob[:4] == b"RIFF" and blob[8:12] == b"WEBP"}

            rounds = []
            with concurrent.futures.ThreadPoolExecutor(max_workers=60) as pool:
                for _ in range(3):
                    rows = list(pool.map(fetch, valid))
                    rounds.append({"requests": len(rows), "all_200_webp": all(x["status"] == 200 and x["webp"] for x in rows),
                                   "failed": [x for x in rows if x["status"] != 200 or not x["webp"]]})
            results["valid_concurrent_thumbnails"] = rounds
            corrupt = next(x["id"] for x in page["items"] if x["name"] == "corrupt.jpg")
            _, _, _ = call(svc.base, f"/api/items/{corrupt}/thumb?size=320", token=token, raw=True)
            time.sleep(.2)
            status, body, hdr = call(svc.base, f"/api/items/{corrupt}/thumb?size=320", token=token)
            results["corrupt_thumbnail"] = {"status": status, "body": body}

            relative = Path("build/final-relative-input.bin")
            relative.parent.mkdir(parents=True, exist_ok=True)
            relative.write_bytes(b"relative-path-fixture")
            p = REPO / relative
            try:
                f = fsopen.open_verified(relative)
                with f:
                    result = {"opened": True, "bytes": len(f.read())}
            except OSError as exc:
                result = {"opened": False, "error": type(exc).__name__, "reason": getattr(exc, "reason", None)}
            with fsopen.open_verified(p) as f:
                result["absolute_same_file_ok"] = f.read() == b"relative-path-fixture"
            results["relative_verified_open"] = result

            # Use new documents for QR links; the old harness only changes fragments.
            from verify_browser_e2e import free_port
            browser = Browser(find_browser(), tmp / "browser-profile", free_port())
            browser.connect()
            browser.remember("Page.enable")
            browser.remember("Runtime.enable")
            browser.remember("Network.enable")
            browser.remember("Emulation.setDeviceMetricsOverride", width=390, height=844,
                             deviceScaleFactor=2, mobile=True)

            def fresh(url):
                browser.goto("about:blank")
                browser.goto(url)

            def code():
                value = auth.generate_pair_code()
                auth.store_pair_code(st.db, value, st.device_id, st.device_name)
                return value

            fresh(svc.base + "/#code=" + code() + "&did=deadbeefdeadbeef")
            browser.wait_for("!!document.querySelector('.err')", budget=15, label="fresh wrong did error")
            wrong = browser.evaluate("({error:(document.querySelector('.err')||{}).textContent||'', paired:!!localStorage.getItem('pb.token'), inputs:document.querySelectorAll('input').length})")
            results["fresh_wrong_device_qr"] = wrong
            shot = browser.send("Page.captureScreenshot", format="png")["data"]
            (OUT / "fresh-wrong-device.png").write_bytes(base64.b64decode(shot))

            fresh(svc.base + "/#code=" + code() + "&did=" + st.device_id)
            ok = browser.wait_for("!!localStorage.getItem('pb.token') && document.querySelectorAll('[data-id]').length > 0", budget=25, label="fresh correct did pairing")
            results["fresh_correct_device_qr"] = {"paired_timeline": ok,
                "cells": browser.evaluate("document.querySelectorAll('[data-id]').length")}
            # Reproduce the long harness's clear-storage + same-document URL change.
            browser.evaluate("localStorage.clear()")
            browser.goto(svc.base + "/#code=" + code() + "&did=deadbeefdeadbeef")
            time.sleep(2)
            results["same_document_after_storage_clear"] = browser.evaluate("({error:(document.querySelector('.err')||{}).textContent||'', paired:!!localStorage.getItem('pb.token'), cells:document.querySelectorAll('[data-id]').length,inputs:document.querySelectorAll('input').length})")
            # Recover using a new document; remaining observations are independent.
            fresh(svc.base + "/#code=" + code() + "&did=" + st.device_id)
            browser.wait_for("document.querySelectorAll('[data-id]').length > 0", budget=25)
            before = time.monotonic()
            e = browser.evaluate("(() => {const e=document.querySelector('[data-id]');e.click();return true;})()")
            browser.wait_for("!!document.querySelector('.viewer')", budget=15)
            time.sleep(1)
            results["viewer"] = browser.evaluate("({open:!!document.querySelector('.viewer'),buttons:[...document.querySelectorAll('.viewer button')].map(b=>({text:b.textContent,title:b.title})),images:[...document.querySelectorAll('.viewer img')].map(i=>({class:i.className,decoded:i.naturalWidth>0,src:i.currentSrc.split('?')[0]}))})")
            # Preserve only request paths/statuses, never headers or token-bearing query strings.
            browser.drain()
            results["network_responses"] = [{"path": str(e["params"]["response"]["url"]).split("?")[0], "status": e["params"]["response"]["status"]}
                for e in browser._events if e.get("method") == "Network.responseReceived"]
            shot = browser.send("Page.captureScreenshot", format="png")["data"]
            (OUT / "focused-viewer.png").write_bytes(base64.b64decode(shot))
            browser.evaluate("document.querySelector('.viewer button').click()")
            browser.evaluate("(() => {const b=[...document.querySelectorAll('button')].find(x=>x.querySelector('.tab-t')?.textContent.trim()==='我的'); if(!b)return false;b.click();return true;})()")
            browser.wait_for("!!document.querySelector('img[src*=\"entrances/qr\"]')", budget=10, label="settings entrance QR")
            browser.evaluate("document.querySelector('img[src*=\"entrances/qr\"]').scrollIntoView({block:'center'})")
            time.sleep(1)
            results["settings_qr_dom"] = browser.evaluate("[...document.querySelectorAll('img[src*=\"entrances/qr\"]')].map(i=>({src:i.getAttribute('src'),decoded:i.naturalWidth>0,complete:i.complete}))")
            results["settings_qr_fetch"] = browser.evaluate("(async()=>{const u='/api/system/entrances/qr?kind=lan';const n=await fetch(u);const body=await n.json();const h=await fetch(u,{headers:{Authorization:'Bearer '+localStorage.getItem('pb.token')}});return {withoutHeader:{status:n.status,body},withHeader:{status:h.status,contentType:h.headers.get('content-type'),bytes:(await h.arrayBuffer()).byteLength}}})()")
            shot = browser.send("Page.captureScreenshot", format="png")["data"]
            (OUT / "settings-qr-confirmed.png").write_bytes(base64.b64decode(shot))
        finally:
            if browser:
                browser.close()
            results["service_stopped"] = svc.stop()
            (OUT / "focused-qr-results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
            import logging
            logging.shutdown()
    (OUT / "focused-qr-results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(results, ensure_ascii=False, indent=2))
    assert all(x["all_200_webp"] for x in results["valid_concurrent_thumbnails"])
    assert not results["fresh_wrong_device_qr"]["paired"]
    assert "另一台电脑" in results["fresh_wrong_device_qr"]["error"]
    assert results["fresh_correct_device_qr"]["paired_timeline"]
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
