"""端到端验证：绑到真实局域网地址，模拟手机走完整用户路径。

验证的是一条完整路径，而不是单个函数：
  1. 服务绑 0.0.0.0 时，对外公布的地址列表必须**只有真实网卡那一个**
  2. 手机访问该地址拿到前端页面
  3. 未配对时受保护接口必须拒绝
  4. 用配对码换 token
  5. 用 token 拉时间线，并取一张缩略图，确认是真图（RIFF/WebP）不是占位图
"""
from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from backend.app.config import get as get_cfg, local_ipv4_addresses  # noqa: E402
from backend.app.server import serve  # noqa: E402

failures: list[str] = []


def check(ok: bool, label: str, extra: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f"  {extra}" if extra else ""))
    if not ok:
        failures.append(label)


def get(url: str, token: str | None = None, *, raw: bool = False):
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=15) as r:
        body = r.read()
        return r.status, (body if raw else json.loads(body.decode("utf-8"))), dict(r.headers)


def main() -> int:
    cfg = get_cfg()
    cfg._raw.setdefault("server", {})
    cfg._raw["server"]["port"] = 8912
    cfg._raw["server"]["allow_lan"] = True
    cfg._raw["server"]["host"] = "0.0.0.0"

    state: dict = {}
    ready = threading.Event()

    async def runner() -> None:
        # on_ready 是**同步**回调（server.py 直接调用，不 await）。
        def on_ready(transport, st, loop, _stop):
            state["urls"] = list(transport.urls)
            state["st"] = st
            state["loop"] = loop
            state["stop_event"] = _stop
            ready.set()

        await serve(cfg, on_ready=on_ready)

    def thread_main() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        state["loop"] = loop
        loop.run_until_complete(runner())

    t = threading.Thread(target=thread_main, daemon=True, name="e2e-server")
    t.start()
    if not ready.wait(60):
        print("FAIL 服务 60 秒内未就绪")
        return 2

    urls = state["urls"]
    print(f"\n[1] 对外公布的地址\n  urls = {urls}")
    real = local_ipv4_addresses()
    check(len(urls) == len(real), "地址数量与 local_ipv4_addresses() 一致",
          f"{len(urls)} vs {len(real)}")
    for bogus in ("26.26.192.40", "192.168.226.1", "192.168.64.1", "192.168.32.1"):
        check(not any(bogus in u for u in urls), f"未公布虚拟网卡 {bogus}")
    check(all(u.startswith("http://") for u in urls), "地址格式正确")

    base = urls[0]
    print(f"\n[2] 手机访问 {base}")
    try:
        req = urllib.request.Request(base + "/")
        with urllib.request.urlopen(req, timeout=15) as r:
            html = r.read().decode("utf-8", "replace")
        check("<div id=\"app\"" in html or "<script" in html, "根路径返回前端 HTML")
    except Exception as exc:  # noqa: BLE001
        check(False, "根路径可访问", repr(exc))
        return 1

    print("\n[3] 未配对时必须拒绝")
    try:
        get(base + "/api/timeline")
        check(False, "未带 token 拉时间线被拒绝")
    except urllib.error.HTTPError as exc:
        check(exc.code == 401, "未带 token 拉时间线返回 401", f"got {exc.code}")

    print("\n[4] 配对码换 token")
    st = state["st"]
    from backend.app import auth as auth_mod
    code = auth_mod.generate_pair_code()
    auth_mod.store_pair_code(st.db, code, st.device_id, st.device_name, ttl=300.0)
    body = json.dumps({"code": code, "name": "e2e-android"}).encode()
    req = urllib.request.Request(base + "/api/pair", data=body,
                                headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        paired = json.loads(r.read().decode("utf-8"))
    token = paired.get("client_token")
    check(bool(token), "配对返回 client_token", f"len={len(token or '')}")
    check(paired.get("device_id") == st.device_id, "返回本机 device_id")

    print("\n[5] 用 token 拉时间线与缩略图")
    status, page, _ = get(base + "/api/timeline?limit=5", token)
    items = page.get("items", [])
    check(status == 200 and len(items) == 5, "时间线返回 5 项", f"got {len(items)}")
    check(page.get("next_cursor") is not None, "next_cursor 非空")

    first = items[0]
    mid = first["id"]
    status, thumb, headers = get(f"{base}/api/items/{mid}/thumb?size=320",
                                 token, raw=True)
    is_webp = thumb[:4] == b"RIFF" and thumb[8:12] == b"WEBP"
    check(status == 200, "缩略图返回 200")
    check(is_webp, "缩略图是真 WebP（RIFF/WEBP 魔数）",
          f"magic={thumb[:4]!r}{thumb[8:12]!r} bytes={len(thumb)}")
    placeholder = Path(REPO / "backend" / "app" / "thumbs.py").read_text(encoding="utf-8")
    check("placeholder" in placeholder, "（占位图机制存在于 thumbs.py）")

    print("\n[6] 时间线字段与契约")
    for field in ("id", "name", "taken_at", "thumb_state", "thumb_v", "root", "dir"):
        check(field in first, f"TimelineItem 含字段 {field}")
    check(first["thumb_v"] is not None, "thumb_v 恒有值（修复过的死锁）")

    print(f"\n{'=' * 56}")
    if failures:
        print(f"失败 {len(failures)} 项：")
        for f in failures:
            print("  -", f)
    else:
        print("=== ALL PASS ===")
    ev = state.get("stop_event")
    lp = state.get("loop")
    t0 = time.time()
    if ev is not None and lp is not None:
        lp.call_soon_threadsafe(ev.set)
    # 给足时间：drain 本身上限 20 秒，再加传输层停止与数据库关闭。
    # 这个 join 只是测试脚本的容忍度，不参与产品逻辑。
    t.join(timeout=45)
    dt = time.time() - t0
    check(not t.is_alive(), "服务优雅退出", f"{dt:.1f}s")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
