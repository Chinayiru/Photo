"""在**真实打包产物**上验证三件事：

1. 端口被占时不再崩，而是给出人话提示（曾经是 SystemExit(3) + logging 级联崩溃）
2. 数据目录落在 exe 旁边，而不是 _internal 里（曾经写进 _internal/data）
3. **在没有 Python 的环境里能跑**（模拟"拷到另一台机器双击"）

用法：`.venv/Scripts/python.exe scripts/verify_exe.py`
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
EXE = REPO / "dist" / "PhotoBridge" / "PhotoBridge.exe"
WORK = Path("H:/tmp/exeverify")


def _clean_env() -> dict[str, str]:
    """造一个**看起来像没装 Python 的机器**的环境变量。

    为什么需要这个
    --------------
    "把 exe 拷到一台没装 Python 的电脑上能跑吗" 是 L6 唯一没做完的验收项。
    在开发机上它天然通过 —— 因为开发机**有** Python，任何"其实偷偷用了
    系统 Python / venv"的缺陷都被掩盖了。这正是 `bootstrap_default_root()`
    硬编码开发机路径那类 bug 的同一个模式：开发机上有，所以看不出来。

    这里从零拼一份环境，只保留 Windows 运行必需的几项，然后：
      * 把 PATH 里所有含 python / conda / venv 的目录剔除
      * 删掉 PYTHONHOME / PYTHONPATH / PYTHONSTARTUP / VIRTUAL_ENV
        （PYTHONHOME 指错会让 exe 里那个 python312.dll 去加载**别的**
          标准库，报 "Fatal Python error: init_fs_encoding"）

    冻结后的 exe 用的是自己 `_internal` 里的 python312.dll 与标准库，
    与 PATH 无关 —— 这个测试要证明的正是"确实无关"。
    """
    keep = ("SystemRoot", "SystemDrive", "windir", "COMSPEC", "PATHEXT",
            "TEMP", "TMP", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
            "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "NUMBER_OF_PROCESSORS",
            "PROCESSOR_ARCHITECTURE", "OS")
    env: dict[str, str] = {}
    for k in keep:
        v = os.environ.get(k)
        if v is not None:
            env[k] = v

    # PATH 只留 Windows 自身目录，剔除一切 Python 相关
    sysroot = env.get("SystemRoot", r"C:\Windows")
    parts = [str(Path(sysroot) / "System32"), sysroot,
             str(Path(sysroot) / "System32" / "Wbem"),
             str(Path(sysroot) / "System32" / "WindowsPowerShell" / "v1.0")]
    env["PATH"] = os.pathsep.join(parts)

    # 显式断言：造出来的环境里真的没有任何 Python 线索
    assert not any("python" in p.lower() for p in env["PATH"].split(os.pathsep))
    for k in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "CONDA_PREFIX"):
        assert k not in env, k
    return env


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _write_cfg(work: Path, port: int, lib: Path) -> Path:
    """写出配置，返回配置文件路径。

    ⚠️ 必须把路径通过 `--config` 传给 exe，**不能**只放到 `<work>/data/`
    就以为它会读到：冻结态下 `APP_ROOT` 是 **exe 所在目录**，所以默认
    数据目录是 `dist/PhotoBridge/data/`，与这里无关。不传 `--config`
    的后果是 exe 用默认端口 8787 正常启动、一直跑 —— 测试表现为
    "超时"，看起来像崩溃，实际是我们自己没把配置接上去。
    """
    d = work / "data"
    d.mkdir(parents=True, exist_ok=True)
    cfg = d / "config.json"
    cfg.write_text(json.dumps({
        "app": {"tray": False},
        # panel_http 默认是关的（审计 F1-b：/panel 只靠回环 IP 挡不住本机浏览器
        # 发起的攻击）。本脚本恰恰是"用 HTTP 调面板接口拿配对码"的正当调用方，
        # 所以必须显式打开 —— 与 backend/tests/test_server.py 的 fixture 同理。
        "server": {"host": "127.0.0.1", "port": port, "panel_http": True},
        "roots": [{"id": "library", "path": str(lib), "mode": "readwrite"}],
        "cloud": {"enabled": False},
    }, ensure_ascii=False), encoding="utf-8")
    return cfg


def _make_lib(lib: Path) -> None:
    (lib / "2024" / "2024-05").mkdir(parents=True, exist_ok=True)
    for i in range(3):
        (lib / "2024" / "2024-05" / f"IMG_{i}.jpg").write_bytes(
            b"\xff\xd8\xff\xe0" + b"x" * 64)


def main() -> int:
    if not EXE.is_file():
        print(f"FAIL: 找不到 {EXE}，先跑 PyInstaller")
        return 1

    if WORK.exists():
        shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True)
    lib = WORK / "lib"
    _make_lib(lib)

    failures: list[str] = []

    # ── 1. 端口被占 ────────────────────────────────────────────
    holder = socket.socket()
    excl = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
    if excl is not None:
        holder.setsockopt(socket.SOL_SOCKET, excl, 1)
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    taken = holder.getsockname()[1]
    cfg = _write_cfg(WORK, taken, lib)

    print(f"[1] 端口 {taken} 已被占，启动 exe …")
    p = subprocess.run([str(EXE), "--config", str(cfg)], capture_output=True,
                       cwd=str(WORK), timeout=90)
    print(f"    returncode = {p.returncode}")
    if p.returncode == 0:
        failures.append("端口被占时竟然返回 0（应是非 0 退出码）")

    # 日志里必须有人话，且不许有 logging 内部崩溃的痕迹
    log = WORK / "data" / "logs" / "agent.log"
    text = log.read_text(encoding="utf-8", errors="replace") if log.is_file() else ""
    if "已被其他程序占用" not in text and "无法使用" not in text:
        failures.append(f"日志里没有端口占用的友好提示；日志尾部：{text[-400:]}")
    for bad in ("NameError", "Logging error", "SystemExit"):
        if bad in text:
            failures.append(f"日志里出现 {bad}（正是要修掉的崩溃）")
    print("    log tail:", text.strip().splitlines()[-1] if text.strip() else "(空)")
    holder.close()

    # ── 2. 正常启动 + 数据目录位置 ─────────────────────────────
    port = _free_port()
    cfg = _write_cfg(WORK, port, lib)
    print(f"[2] 用空闲端口 {port} 启动 exe …")
    proc = subprocess.Popen([str(EXE), "--config", str(cfg)], cwd=str(WORK),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        ok = False
        deadline = time.time() + 60
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/api/system/ping", timeout=2) as r:
                    body = json.loads(r.read())
                    print(f"    ping -> {body}")
                    ok = True
                    break
            except Exception:  # noqa: BLE001
                time.sleep(0.5)
        if not ok:
            failures.append("服务没能在 60 秒内响应 /api/system/ping")

        # 数据目录必须在 exe 旁边（WORK/data），不在 _internal 里
        if not (WORK / "data" / "photobridge.db").is_file():
            failures.append("数据库没有落在 exe 旁边的 data/ 目录")
        exe_dir = EXE.parent
        if (exe_dir / "_internal" / "data").exists():
            failures.append("数据写进了 _internal/data —— APP_ROOT 又错了")
        print(f"    db at {WORK / 'data' / 'photobridge.db'} exists="
              f"{(WORK / 'data' / 'photobridge.db').is_file()}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()

    # ── 3. 没有 Python 的环境 ─────────────────────────────────
    # 把整个 dist 目录拷到别处再跑：既模拟"拷到另一台机器"，
    # 又顺带验证 exe 不依赖仓库里的任何东西（比如误引用了源码目录）。
    print("\n[3] 在**没有 Python** 的环境里启动（模拟拷到别的电脑）")
    env = _clean_env()
    print(f"    PATH = {env['PATH']}")
    print("    已剔除 PYTHONHOME/PYTHONPATH/VIRTUAL_ENV/CONDA_PREFIX")

    port3 = _free_port()
    copy_root = WORK / "portable"
    if copy_root.exists():
        shutil.rmtree(copy_root, ignore_errors=True)
    shutil.copytree(EXE.parent, copy_root / "PhotoBridge")
    exe3 = copy_root / "PhotoBridge" / "PhotoBridge.exe"
    work3 = copy_root / "run"
    work3.mkdir(parents=True)
    lib3 = copy_root / "lib"
    _make_lib(lib3)
    cfg3 = _write_cfg(work3, port3, lib3)
    # 复制后的 exe 名字不变，但 _write_cfg 按 work3 写配置，
    # 所以数据目录会在 work3/data —— 与 exe 目录分开，便于断言。

    proc3 = subprocess.Popen([str(exe3), "--config", str(cfg3)],
                             cwd=str(work3), env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        ok3 = False
        deadline = time.time() + 90
        while time.time() < deadline:
            if proc3.poll() is not None:
                break
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{port3}/api/system/ping", timeout=2) as r:
                    body = json.loads(r.read())
                    print(f"    ping -> {body}")
                    ok3 = True
                    break
            except Exception:  # noqa: BLE001
                time.sleep(0.5)
        if not ok3:
            out, err = proc3.communicate(timeout=10)
            failures.append(
                "在无 Python 环境下服务没能起来；"
                f"returncode={proc3.returncode} stderr={err[-400:]!r}")
        else:
            # 前端必须也能出来（它打在 _internal/frontend/dist 里）
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{port3}/", timeout=5) as r:
                    html = r.read().decode("utf-8", "replace")
                if "<html" not in html.lower():
                    failures.append("无 Python 环境下根路径没有返回 HTML 界面")
                else:
                    print(f"    根路径返回 HTML（{len(html)} 字节）")
            except Exception as exc:  # noqa: BLE001
                failures.append(f"无 Python 环境下根路径打不开：{exc!r}")

            # ★ 配对 + 拉时间线 + **真的生成一张缩略图**。
            # 这一步才会用到 _internal 里的 Pillow / pillow_heif 等原生库 ——
            # 只 ping 一下证明不了解释器之外的任何东西能工作，而冻结态
            # 最常见的失败恰恰是"Python 起来了，一调原生库就 ImportError"。
            try:
                # /panel/pair-code 只允许本机调用，我们就是本机
                pr = urllib.request.Request(
                    f"http://127.0.0.1:{port3}/panel/pair-code", method="POST",
                    data=b"{}", headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(pr, timeout=10) as r:
                    code = json.loads(r.read()).get("code")
                print(f"    配对码 {code}")

                pr = urllib.request.Request(
                    f"http://127.0.0.1:{port3}/api/pair", method="POST",
                    data=json.dumps({"code": code, "name": "no-python-check"})
                    .encode("utf-8"),
                    headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(pr, timeout=10) as r:
                    token = json.loads(r.read())["client_token"]

                # 等首次索引（3 张合成图，很快）
                items: list = []
                deadline = time.time() + 60
                while time.time() < deadline:
                    tr = urllib.request.Request(
                        f"http://127.0.0.1:{port3}/api/timeline?limit=10",
                        headers={"Authorization": f"Bearer {token}"})
                    with urllib.request.urlopen(tr, timeout=10) as r:
                        items = json.loads(r.read()).get("items", [])
                    if items:
                        break
                    time.sleep(0.5)
                if not items:
                    failures.append("无 Python 环境下时间线是空的（索引器没工作）")
                else:
                    print(f"    时间线 {len(items)} 项")
                    mid = items[0]["id"]
                    # ★ 真去取一张缩略图：这会走 Pillow 解码 + WebP 编码，
                    # 也就是"打包后原生库能不能用"的直接证据。
                    tr = urllib.request.Request(
                        f"http://127.0.0.1:{port3}/api/items/{mid}/thumb?size=320",
                        headers={"Authorization": f"Bearer {token}"})
                    with urllib.request.urlopen(tr, timeout=30) as r:
                        blob = r.read()
                        ctype = r.headers.get("Content-Type", "")
                    if blob[:4] != b"RIFF" or blob[8:12] != b"WEBP":
                        failures.append(
                            f"缩略图不是 WebP（ctype={ctype} 前 16 字节={blob[:16]!r}）"
                            " —— 打包后的 Pillow/原生库可能有问题")
                    else:
                        print(f"    ★ 缩略图 {ctype} {len(blob)} 字节（RIFF/WEBP）")
            except Exception as exc:  # noqa: BLE001
                failures.append(f"无 Python 环境下配对/索引/缩略图链路失败：{exc!r}")

        # 数据目录：这次它不在 exe 旁边（配置指定在 work3），
        # 但**绝不能**出现在 exe 的 _internal 里
        if (exe3.parent / "_internal" / "data").exists():
            failures.append("无 Python 环境下数据写进了 _internal/data")
        if not (work3 / "data" / "photobridge.db").is_file():
            failures.append("无 Python 环境下数据库没有落在 --config 指定的目录")
        else:
            print(f"    db at {work3 / 'data' / 'photobridge.db'} ok")
    finally:
        proc3.terminate()
        try:
            proc3.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc3.kill()

    # ── 结论 ───────────────────────────────────────────────────
    if failures:
        print("\n=== FAILED ===")
        for f in failures:
            print(" -", f)
        return 1
    print("\n=== ALL PASS ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
