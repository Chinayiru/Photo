"""在**真实打包产物**上验证两件事：

1. 端口被占时不再崩，而是给出人话提示（曾经是 SystemExit(3) + logging 级联崩溃）
2. 数据目录落在 exe 旁边，而不是 _internal 里（曾经写进 _internal/data）

用法：`.venv/Scripts/python.exe scripts/verify_exe.py`
"""
from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
EXE = REPO / "dist" / "PhotoBridge" / "PhotoBridge.exe"
WORK = Path("H:/tmp/exeverify")


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
        "server": {"host": "127.0.0.1", "port": port},
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
        import urllib.error
        import urllib.request

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
