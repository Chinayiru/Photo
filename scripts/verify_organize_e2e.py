"""端到端验证：改名与移动**真的改了磁盘上的文件**，而且能原样改回来。

为什么单测不够
--------------
`backend/tests/test_move_rename.py` 直接调 handler，中间隔了一层真实 HTTP、
一层鉴权、一层 dispatch。而"改名/移动"最怕的失败模式恰恰在层与层之间：

* 接口返回 200，但磁盘上的文件根本没动（用户看到"已改名"，下次扫描
  又变回旧名字）
* 文件动了，但数据库没更新（下次扫描把照片标成 missing，用户以为丢了）
* 移动时覆盖了目标目录的同名文件（**永久数据丢失**，不可恢复）

所以这里走真实 HTTP，每一步都用 `os.stat` / 读字节来确认磁盘事实，
而不是相信接口的返回值。

## 安全边界

全程在一个 `tempfile.mkdtemp()` 造的合成照片库里操作，**绝不碰真实照片库**。
脚本结束时会校验真实库指纹与开始时一致（行数 / missing / max id）。
"""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from _e2e_common import real_db_fingerprint  # noqa: E402

from backend.app.config import Config  # noqa: E402
from backend.app.server import serve  # noqa: E402

failures: list[str] = []


def check(ok: bool, label: str, extra: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f"  {extra}" if extra else ""))
    if not ok:
        failures.append(label)


def real_fingerprint() -> str:
    """真实库指纹（默认配置的 data_dir，不是临时库）。实现见 `_e2e_common`。"""
    return real_db_fingerprint(REPO / "data" / "photobridge.db")


def req(url: str, token: str | None = None, *, method: str = "GET",
        body: dict | None = None, raw: bool = False):
    data = None
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    r = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(r, timeout=20) as resp:
        payload = resp.read()
        return resp.status, (payload if raw else json.loads(payload.decode("utf-8")))


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_library(root: Path) -> dict[str, Path]:
    """造一个带子目录的合成库，返回 {名字: 路径}。"""
    from PIL import Image

    files: dict[str, Path] = {}
    specs = [
        ("相册A/a.jpg", (200, 100, 50)),
        ("相册A/b.jpg", (50, 200, 100)),
        ("相册B/c.jpg", (100, 50, 200)),
    ]
    for rel, color in specs:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (120, 90), color).save(p, "JPEG")
        files[rel] = p
    return files


def main() -> int:
    real_before = real_fingerprint()
    print(f"真实库（开始）：{real_before}")

    tmp = Path(tempfile.mkdtemp(prefix="pb_organize_"))
    library = tmp / "lib"
    library.mkdir()
    data_dir = tmp / "data"
    data_dir.mkdir()
    files = make_library(library)
    print(f"合成库：{library}（{len(files)} 张）\n")

    cfg = Config({
        "app": {"mode": "readwrite"},
        "server": {"host": "127.0.0.1", "port": 8913},
        "roots": [{
            "id": "library", "path": str(library),
            "mode": "readwrite", "enabled": True, "recursive": True,
        }],
        "auth": {"allow_delete": True, "allow_move": True},
        "index": {"batch_size": 50},
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

    t = threading.Thread(target=thread_main, daemon=True, name="organize-e2e")
    t.start()
    if not ready.wait(90):
        print("FAIL 服务 90 秒内未就绪")
        return 2

    base = state["base"]
    st = state["st"]

    # ---- 配对 --------------------------------------------------------
    from backend.app import auth as auth_mod
    code = auth_mod.generate_pair_code()
    auth_mod.store_pair_code(st.db, code, st.device_id, st.device_name, ttl=300.0)
    _, paired = req(base + "/api/pair", method="POST",
                    body={"code": code, "name": "organize-e2e"})
    token = paired["client_token"]

    # ---- 等索引跑完 --------------------------------------------------
    deadline = time.time() + 60
    while time.time() < deadline:
        _, page = req(base + "/api/timeline?limit=50", token)
        if len(page.get("items", [])) >= len(files):
            break
        time.sleep(0.4)

    print("[1] 索引结果")
    _, page = req(base + "/api/timeline?limit=50", token)
    by_name = {it["name"]: it for it in page["items"]}
    check(len(by_name) == len(files), f"时间线有 {len(files)} 张",
          f"got {len(by_name)}")
    a_id = by_name.get("a.jpg", {}).get("id")
    check(a_id is not None, "找到 a.jpg")
    if a_id is None:
        return 1

    # ---- 改名 --------------------------------------------------------
    print("\n[2] 改名：a.jpg → 假期合影.jpg")
    old_path = library / "相册A" / "a.jpg"
    new_path = library / "相册A" / "假期合影.jpg"
    before_sha = sha(old_path)
    status, body = req(f"{base}/api/items/rename", token, method="POST",
                       body={"id": a_id, "new_name": "假期合影.jpg"})
    check(status == 200, "改名返回 200", f"got {status}")
    check(body.get("to") == "相册A/假期合影.jpg", "响应 to 是新路径",
          f"got {body.get('to')}")
    # ★ 磁盘事实，而不是相信响应
    check(not old_path.exists(), "旧文件名在磁盘上已消失")
    check(new_path.exists(), "新文件名在磁盘上存在")
    check(new_path.exists() and sha(new_path) == before_sha,
          "改名后字节完全一致（没被重新编码）")

    _, detail = req(f"{base}/api/items/{a_id}", token)
    check(detail.get("name") == "假期合影.jpg", "详情接口返回新名字",
          f"got {detail.get('name')}")
    check(detail.get("rel_path") == "相册A/假期合影.jpg", "详情 rel_path 已更新")
    check(detail.get("missing") is False, "没有被标成 missing")

    # ---- 改名回来（验证可逆）------------------------------------------
    print("\n[3] 改回来：假期合影.jpg → a.jpg")
    status, _ = req(f"{base}/api/items/rename", token, method="POST",
                    body={"id": a_id, "new_name": "a.jpg"})
    check(status == 200, "改回原名成功")
    check(old_path.exists() and sha(old_path) == before_sha, "文件回到原位且字节一致")

    # ---- 改名拒绝：越界 -------------------------------------------------
    print("\n[4] 改名必须拒绝的危险输入")
    for bad, why in [
        ("../escape.jpg", "带 .. 的路径"),
        ("sub/x.jpg", "带路径分隔符"),
        ("evil.exe", "非图片扩展名"),
        ("noext", "没有扩展名"),
        ("CON.jpg", "Windows 保留名"),
    ]:
        try:
            req(f"{base}/api/items/rename", token, method="POST",
                body={"id": a_id, "new_name": bad})
            check(False, f"拒绝「{why}」", "却返回了成功")
        except urllib.error.HTTPError as exc:
            check(exc.code == 400, f"拒绝「{why}」", f"got {exc.code}")
    check(old_path.exists(), "被拒后原文件安然无恙")
    check(not (library.parent / "escape.jpg").exists(), "没有文件被写到库外")

    # ---- 移动 --------------------------------------------------------
    print("\n[5] 移动：相册A/a.jpg → 相册B/")
    status, body = req(f"{base}/api/items/move", token, method="POST",
                       body={"ids": [a_id], "target_dir": "相册B"})
    check(status == 200, "移动返回 200", f"got {status}")
    moved = (body.get("moved") or [{}])[0]
    check(moved.get("to") == "相册B/a.jpg", "响应 to 正确", f"got {moved.get('to')}")
    check(not old_path.exists(), "源文件已不在原目录")
    check((library / "相册B" / "a.jpg").exists(), "文件出现在目标目录")
    check(sha(library / "相册B" / "a.jpg") == before_sha, "移动后字节一致")
    check(not body.get("failed"), "没有 failed 项", f"got {body.get('failed')}")

    # ---- 移动到不存在的目录（自动创建）---------------------------------
    print("\n[6] 移动到不存在的目录：新目录/子目录/")
    status, body = req(f"{base}/api/items/move", token, method="POST",
                       body={"ids": [a_id], "target_dir": "新目录/子目录"})
    check(status == 200 and (library / "新目录" / "子目录" / "a.jpg").exists(),
          "自动创建了多级目录")

    # ---- 冲突：绝不覆盖 -------------------------------------------------
    print("\n[7] 冲突：目标目录已有同名文件 → 绝不覆盖")
    target_dir = library / "相册B"
    # 造一个**内容不同**的同名文件当受害者 —— 如果只校验"文件还在"，
    # 覆盖之后它当然也"还在"，测不出任何东西
    from PIL import Image as _Image
    victim = target_dir / "a.jpg"
    victim.parent.mkdir(parents=True, exist_ok=True)
    _Image.new("RGB", (64, 64), (7, 7, 7)).save(victim, "JPEG")
    victim_sha = sha(victim)
    victim_size = victim.stat().st_size
    # 此时 a.jpg 在 新目录/子目录 下（第 [6] 步移过去的）
    src_now = library / "新目录" / "子目录" / "a.jpg"
    check(src_now.exists(), "被移动的源文件存在")
    check(src_now.stat().st_size != victim_size, "两个同名文件内容确实不同")

    status, body = req(f"{base}/api/items/move", token, method="POST",
                       body={"ids": [a_id], "target_dir": "相册B"})
    check(status == 200, "移动返回 200")
    moved = (body.get("moved") or [{}])[0]
    check(moved.get("to") == "相册B/a_1.jpg", "重名被改成 a_1.jpg",
          f"got {moved.get('to')}")
    check(moved.get("renamed") is True, "renamed 标记为 true")
    # ★ 这一条是整个脚本最重要的一行：目标文件必须一个字节都没变
    check(sha(victim) == victim_sha, "★ 原同名文件字节未变（没有被覆盖）")
    check(victim.stat().st_size == victim_size, "★ 原同名文件大小未变")
    check((target_dir / "a_1.jpg").exists(), "新文件落在 a_1.jpg")
    check(sha(target_dir / "a_1.jpg") == before_sha, "新文件是原来的那张（内容对）")

    # ---- 时间线仍能查到，且目录正确 -------------------------------------
    print("\n[8] 时间线契约")
    _, page = req(base + "/api/timeline?limit=50", token)
    item = next((x for x in page["items"] if x["id"] == a_id), None)
    check(item is not None, "移动后的照片仍在时间线里")
    if item:
        check(item.get("dir") == "相册B", "时间线 dir 已更新", f"got {item.get('dir')}")
        check(item.get("name") == "a_1.jpg", "时间线 name 已更新")

    # ---- 越界移动必须被拒 ------------------------------------------------
    print("\n[9] 移动必须拒绝的危险目标")
    for bad, why in [
        ("../outside", "带 .. 的目录"),
        ("C:/Windows", "绝对路径"),
    ]:
        try:
            req(f"{base}/api/items/move", token, method="POST",
                body={"ids": [a_id], "target_dir": bad})
            check(False, f"拒绝「{why}」", "却返回了成功")
        except urllib.error.HTTPError as exc:
            check(exc.code == 400, f"拒绝「{why}」", f"got {exc.code}")
    check(not (tmp / "outside").exists(), "没有目录被建到库外")

    # ---- 审计 ----------------------------------------------------------
    print("\n[10] 审计日志")
    audit = data_dir / "audit.log"
    lines = audit.read_text(encoding="utf-8").splitlines() if audit.exists() else []
    actions = [ln.split("\t")[1] for ln in lines if len(ln.split("\t")) > 1]
    check("rename" in actions, "审计里有 rename", f"actions={sorted(set(actions))}")
    check("move" in actions, "审计里有 move")
    # 表索引与文件必须一致（这是合并 audit.py 时修掉的缺陷）
    rows = st.db.query("SELECT action, COUNT(*) AS n FROM audit_log"
                       " GROUP BY action")
    table = {r["action"]: r["n"] for r in rows}
    check(table.get("rename", 0) >= 1, "audit_log 表里也有 rename", f"{table}")
    check(table.get("move", 0) >= 1, "audit_log 表里也有 move")

    # ---- 重扫后记录不漂移 -------------------------------------------------
    print("\n[11] 重扫后数据库与磁盘一致")
    st.db.execute("UPDATE media SET missing = 1")   # 全部打脏，逼重扫修正
    req(f"{base}/api/index/rescan", token, method="POST", body={})
    deadline = time.time() + 60
    ok = False
    while time.time() < deadline:
        n = st.db.scalar("SELECT COUNT(*) FROM media WHERE missing = 0")
        if n and n >= len(files):
            ok = True
            break
        time.sleep(0.4)
    check(ok, "重扫后所有照片都回到 missing=0")

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
    t.join(timeout=45)
    check(not t.is_alive(), "服务优雅退出", f"{time.time() - t0:.1f}s")

    real_after = real_fingerprint()
    print(f"真实库（结束）：{real_after}")
    check(real_after == real_before, "★ 真实照片库一个字节都没变")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
