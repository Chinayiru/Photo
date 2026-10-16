"""端到端验证 AC-2.15：**在电脑上往库目录拷一张图，手机上不用做任何事就能看到**。

## 为什么单测不够

`backend/tests/test_watcher.py` 里已经有真实 Observer + 真实文件复制的用例，
但它们都是直接构造 `Watcher`、直接调 `_process_ready()`、直接查库。
真正的验收标准说的是另一件事：

    "直接在电脑上往库目录拷入 1 张新图，30 秒内手机时间线可见（无需手动重扫）"

这条链路上有四层是单测绕过去的：

* watchdog 的 OS 层事件到底有没有真的投递（Windows 用
  `ReadDirectoryChangesW`，网络盘/某些同步盘上会静默失效）
* 服务有没有真的把监听启起来（`watch: True` 曾经是空头承诺）
* 索引器写库之后，**时间线接口**的 WHERE 子句会不会把它挡住
* 事件总线推的 `media_changed` 有没有发出去

所以这里起**真实服务**，用 `shutil.copy2` 模拟"用户在资源管理器里粘贴"
（这正是 AC 描述的动作），然后只用 HTTP 轮询时间线来判定，全程不碰
内部对象 —— 用户能看到的才是验收标准。

## 安全边界

全程在 `tempfile.mkdtemp()` 造的合成照片库里操作，**绝不碰真实照片库**。
脚本结束校验真实库指纹与开始时一致。
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from backend.app.config import Config  # noqa: E402
from backend.app.server import serve  # noqa: E402

#: AC-2.15 的硬指标。超过它就算失败 —— 不要为了让测试通过而调大。
AC_BUDGET_SEC = 30.0

failures: list[str] = []


def check(ok: bool, label: str, extra: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f"  {extra}" if extra else ""))
    if not ok:
        failures.append(label)


def real_fingerprint() -> str:
    db = REPO / "data" / "photobridge.db"
    if not db.exists():
        return "absent"
    con = sqlite3.connect(str(db))
    try:
        rows = con.execute("SELECT COUNT(*) FROM media").fetchone()[0]
        miss = con.execute("SELECT COUNT(*) FROM media WHERE missing = 1").fetchone()[0]
        mx = con.execute("SELECT MAX(id) FROM media").fetchone()[0]
        return f"rows={rows} missing={miss} maxid={mx}"
    finally:
        con.close()


def req(url: str, token: str | None = None, *, method: str = "GET",
        body: dict | None = None):
    data = None
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    r = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(r, timeout=20) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def req_raw(url: str, token: str | None = None):
    """取原始字节（缩略图用）。要验魔数，不能经 json 解析。"""
    r = urllib.request.Request(url)
    if token:
        r.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(r, timeout=20) as resp:
        return resp.status, resp.read()


def make_image(path: Path, color: tuple[int, int, int], size=(160, 120)) -> None:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, "JPEG")


def timeline_names(base: str, token: str) -> set[str]:
    """时间线上所有文件名。只看 HTTP 响应 —— 用户能看到的才算。"""
    _, page = req(base + "/api/timeline?limit=100", token)
    return {it["name"] for it in page.get("items", [])}


def timeline_items(base: str, token: str) -> list[dict]:
    _, page = req(base + "/api/timeline?limit=100", token)
    return page.get("items", [])


def wait_for(base: str, token: str, predicate, *, budget: float) -> tuple[bool, float]:
    """轮询直到 predicate(名字集合) 为真。返回 (是否成功, 耗时秒)。"""
    t0 = time.monotonic()
    while True:
        try:
            if predicate(timeline_names(base, token)):
                return True, time.monotonic() - t0
        except urllib.error.URLError:
            pass        # 服务正在忙，继续等
        if time.monotonic() - t0 > budget:
            return False, time.monotonic() - t0
        time.sleep(0.5)


def wait_items(base: str, token: str, predicate, *, budget: float) -> tuple[bool, float]:
    """同上，但断言的是**整条时间线记录**而不只是文件名。

    ★ 为什么必须区分：判据若只写"某个文件名在不在"，一个**已经成立**
    的条件会让循环立刻返回 —— 于是后面那句断言其实是在防抖窗口
    （默认 2 秒）结束**之前**执行的，测的是旧状态。
    这个坑真实发生过：改文件夹名的用例里，文件名本来就在时间线上
    （只是 dir 还是旧的），`wait_for` 0.0 秒返回，随后的 dir 断言
    必然失败 —— 看起来像产品 bug，其实是测试没等。
    """
    t0 = time.monotonic()
    while True:
        try:
            if predicate(timeline_items(base, token)):
                return True, time.monotonic() - t0
        except urllib.error.URLError:
            pass
        if time.monotonic() - t0 > budget:
            return False, time.monotonic() - t0
        time.sleep(0.5)


def main() -> int:
    real_before = real_fingerprint()
    print(f"真实库（开始）：{real_before}")

    tmp = Path(tempfile.mkdtemp(prefix="pb_watch_"))
    library = tmp / "lib"
    library.mkdir()
    data_dir = tmp / "data"
    data_dir.mkdir()
    # 库外的一个"手机相册"目录，用来模拟"从别处拷进来"
    inbox = tmp / "inbox"
    inbox.mkdir()

    make_image(library / "已有照片.jpg", (10, 20, 30))
    make_image(library / "子目录" / "旧的.jpg", (30, 20, 10))
    print(f"合成库：{library}（初始 2 张）\n")

    cfg = Config({
        "app": {"mode": "readwrite"},
        "server": {"host": "127.0.0.1", "port": 8914},
        "roots": [{
            "id": "library", "path": str(library),
            "mode": "readwrite", "enabled": True, "recursive": True,
        }],
        "auth": {"allow_delete": True},
        # ★ 用产品默认值，不为测试放宽 —— 这里要验的正是"默认配置下能否达标"。
        "index": {"watch": True, "debounce_sec": 2.0, "batch_size": 50},
    }, path=data_dir / "config.json", data_dir=data_dir)
    cfg.save()

    state: dict = {}
    ready = threading.Event()

    async def runner() -> None:
        def on_ready(transport, st, _loop, _stop):
            state["st"] = st
            state["base"] = transport.urls[0]
            state["stop_event"] = _stop
            ready.set()

        await serve(cfg, on_ready=on_ready)

    def thread_main() -> None:
        loop = __import__("asyncio").new_event_loop()
        __import__("asyncio").set_event_loop(loop)
        state["loop"] = loop
        loop.run_until_complete(runner())

    t = threading.Thread(target=thread_main, daemon=True, name="watch-e2e")
    t.start()
    if not ready.wait(90):
        print("FAIL 服务 90 秒内未就绪")
        return 2

    base = state["base"]
    st = state["st"]

    from backend.app import auth as auth_mod
    code = auth_mod.generate_pair_code()
    auth_mod.store_pair_code(st.db, code, st.device_id, st.device_name, ttl=300.0)
    _, paired = req(base + "/api/pair", method="POST",
                    body={"code": code, "name": "watch-e2e"})
    token = paired["client_token"]

    # ---- [1] 监听必须真的在跑 ------------------------------------------
    print("[1] /api/system/info 如实报告监听状态")
    _, info = req(base + "/api/system/info", token)
    watch = info.get("watch") or {}
    check(watch.get("enabled") is True, "watch.enabled 为真", f"{watch}")
    check(watch.get("running") is True, "★ watch.running 为真（监听器真在跑）",
          f"got {watch.get('running')} reason={watch.get('reason')}")
    check(watch.get("debounce_sec") == 2.0, "上报的是实际生效的防抖值",
          f"got {watch.get('debounce_sec')}")

    # ---- [2] 初始索引 ---------------------------------------------------
    print("\n[2] 初始索引")
    ok, _ = wait_for(base, token, lambda n: len(n) >= 2, budget=60)
    check(ok, "初始 2 张已入库", f"got {sorted(timeline_names(base, token))}")

    # ---- [3] ★ AC-2.15 主场景：从库外拷入一张新图 -----------------------
    print(f"\n[3] ★ AC-2.15：往库目录拷入新图（预算 {AC_BUDGET_SEC:.0f} 秒）")
    new_src = inbox / "IMG_新照片.jpg"
    make_image(new_src, (200, 100, 50))
    dst = library / "IMG_新照片.jpg"
    shutil.copy2(new_src, dst)      # ← 用户在资源管理器里"粘贴"就是这个动作
    check(dst.exists(), "文件已落到磁盘")

    ok, dt = wait_for(base, token, lambda n: "IMG_新照片.jpg" in n,
                      budget=AC_BUDGET_SEC)
    check(ok, f"★ 手机时间线自动出现了新照片（{dt:.1f} 秒，无需手动重扫）")
    check(dt < AC_BUDGET_SEC, f"耗时 {dt:.1f}s < {AC_BUDGET_SEC:.0f}s 预算")

    # ---- [4] 拷进子目录也一样 -------------------------------------------
    print("\n[4] 拷进子目录（递归监听）")
    make_image(inbox / "子目录新图.jpg", (50, 200, 100))
    (library / "子目录").mkdir(exist_ok=True)
    shutil.copy2(inbox / "子目录新图.jpg", library / "子目录" / "子目录新图.jpg")
    ok, dt = wait_for(base, token, lambda n: "子目录新图.jpg" in n,
                      budget=AC_BUDGET_SEC)
    check(ok, f"子目录里的新图也被收录（{dt:.1f} 秒）")
    _, page = req(base + "/api/timeline?limit=100", token)
    item = next((x for x in page["items"] if x["name"] == "子目录新图.jpg"), None)
    check(item is not None and item.get("dir") == "子目录",
          "目录归属正确", f"got {item.get('dir') if item else None}")

    # ---- [5] 从电脑上删除 → 手机上消失 ----------------------------------
    print("\n[5] 在电脑上删除 → 手机时间线里消失")
    (library / "已有照片.jpg").unlink()
    ok, dt = wait_for(base, token, lambda n: "已有照片.jpg" not in n,
                      budget=AC_BUDGET_SEC)
    check(ok, f"删除后时间线不再返回它（{dt:.1f} 秒）")
    row = st.db.query_one("SELECT missing FROM media WHERE file_name = ?",
                          ("已有照片.jpg",))
    check(row is not None, "库里仍保留这一行（只标 missing，不删记录）")
    if row is not None:
        check(row["missing"] == 1, "被标为 missing=1", f"got {row['missing']}")

    # ---- [6] 不该被收录的东西 -------------------------------------------
    print("\n[6] 忽略名单：.part / 非图片 / 临时目录")
    tmp_dir = library / ".photobridge_tmp"
    tmp_dir.mkdir(exist_ok=True)
    (tmp_dir / "half.part").write_bytes(b"\xff\xd8\xff\xe0" + b"x" * 256)
    (library / "说明.txt").write_text("不是图片", encoding="utf-8")
    (library / "残留.jpg-lx2").write_bytes(b"\xff\xd8\xff\xe0" + b"y" * 128)
    # 给监听足够的时间去（错误地）收录它们
    time.sleep(6.0)
    names = timeline_names(base, token)
    check("half.part" not in names, ".part 临时分片没被收录")
    check("说明.txt" not in names, "非图片文件没被收录")
    check("残留.jpg-lx2" not in names, "白名单外的扩展名没被收录")
    n_rows = st.db.scalar("SELECT COUNT(*) FROM media WHERE ext NOT IN"
                          " ('jpg','jpeg','png','webp','heic','heif','bmp',"
                          "'gif','tif','tiff','avif')", default=0)
    check(n_rows == 0, "库里没有白名单外的扩展名", f"got {n_rows}")

    # ---- [7] 改名（OS 层 move 事件）--------------------------------------
    print("\n[7] 在电脑上改名 → 时间线跟着变")
    (library / "IMG_新照片.jpg").rename(library / "改名后的照片.jpg")
    ok, dt = wait_for(base, token, lambda n: "改名后的照片.jpg" in n,
                      budget=AC_BUDGET_SEC)
    check(ok, f"改名后的新名字出现（{dt:.1f} 秒）")
    names = timeline_names(base, token)
    check("IMG_新照片.jpg" not in names, "旧名字已从时间线消失")

    # ★ 这里要查的是"**可见的**幽灵行"，不是"有没有旧行"。
    # 设计上改名 = 源路径标 missing + 目标路径新增一行（旧行保留，
    # 只是 missing=1 被时间线过滤掉）。所以断言必须带 missing = 0 ——
    # 一开始漏了这个条件，把正确行为报成了失败。
    ghosts = st.db.query(
        "SELECT id, rel_path FROM media"
        " WHERE missing = 0 AND rel_path LIKE '%IMG_新照片%'")
    check(len(ghosts) == 0,
          "★ 没有指向旧名字的可见幽灵行（missing=0）",
          f"got {[g['rel_path'] for g in ghosts]}")
    old_row = st.db.query_one(
        "SELECT missing FROM media WHERE rel_path LIKE '%IMG_新照片%'")
    check(old_row is not None and old_row["missing"] == 1,
          "旧行保留但标为 missing=1（不删记录）",
          f"got {old_row['missing'] if old_row else '行不存在'}")

    # ★ 反向：新名字那一行必须**真的可用** —— 缩略图能取到。
    # 只查库里有行是不够的：改名后若新行的 thumb_state 停在 pending
    # 或 failed，用户在手机上看到的就是灰格子。
    new_row = st.db.query_one(
        "SELECT id FROM media WHERE rel_path = '改名后的照片.jpg'")
    check(new_row is not None, "新名字在库里有行")
    if new_row is not None:
        status, thumb = req_raw(f"{base}/api/items/{new_row['id']}/thumb?size=320",
                                token)
        is_webp = thumb[:4] == b"RIFF" and thumb[8:12] == b"WEBP"
        check(status == 200 and is_webp,
              "★ 改名后的照片缩略图可用（真 WebP）",
              f"status={status} magic={thumb[:4]!r}")

    # ---- [8] ★ 在电脑上改**文件夹**名 ------------------------------------
    # 比改文件名更危险：一次操作会改变里面每一张照片的 rel_path。
    # 探针实测 watchdog 会为目录内每个文件各报一次 moved（不只是目录本身），
    # 所以只要 `_rel_of` 没把文件级事件也一起丢掉，这里就该全部跟上。
    print("\n[8] ★ 在电脑上改文件夹名 → 里面所有照片的目录归属跟着变")
    (library / "子目录").rename(library / "改名后的目录")
    # ★ 判据必须落在 **dir 字段**上，不能落在"文件名在不在"上。
    # 文件名从一开始就在时间线上（只是 dir 还是旧的），用文件名判
    # 会让循环 0.0 秒返回，随后的断言在防抖窗口结束前执行。
    ok, dt = wait_items(
        base, token,
        lambda items: any(x["name"] == "子目录新图.jpg"
                          and x.get("dir") == "改名后的目录" for x in items),
        budget=AC_BUDGET_SEC)
    check(ok, f"★ dir 已更新为新的目录名（{dt:.1f} 秒）")
    if not ok:
        item = next((x for x in timeline_items(base, token)
                     if x["name"] == "子目录新图.jpg"), None)
        print(f"     实际 dir = {item.get('dir') if item else None}")
    stale = st.db.query(
        "SELECT rel_path FROM media WHERE missing = 0"
        " AND rel_path LIKE '子目录/%'")
    check(len(stale) == 0, "★ 没有留在旧目录路径下的可见行",
          f"got {[s['rel_path'] for s in stale]}")

    # ---- [9] 关闭监听时必须如实报告 --------------------------------------
    print("\n[9] 关掉监听后如实报告（不假装在跑）")
    st.watcher.enabled = False
    _, info2 = req(base + "/api/system/info", token)
    w2 = info2.get("watch") or {}
    check(w2.get("enabled") is False, "watch.enabled 变假", f"{w2}")
    st.watcher.enabled = True

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
    t.join(timeout=60)
    check(not t.is_alive(), "服务优雅退出", f"{time.time() - t0:.1f}s")

    real_after = real_fingerprint()
    print(f"真实库（结束）：{real_after}")
    check(real_after == real_before, "★ 真实照片库一个字节都没变")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
