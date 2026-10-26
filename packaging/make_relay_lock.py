"""从 `requirements.lock` 里切出中继端要用的那一小块，生成 `cloud/requirements.lock`。

## 为什么要单独一份

中继（`cloud/relay/`）只跑 starlette + uvicorn + websockets，跟电脑端那套
（Pillow、pillow-heif、pystray、watchdog...）没有交集。给云主机装全量锁文件
等于把一堆用不上的 C 扩展搬上 2 vCPU 的小机器。

但审计 F05 要的是**同样的强度**：`--require-hashes` + 每个发布物带 sha256，
并且不得出现 `--trusted-host` / `--index-url` 这类可以绕开校验的参数。所以
这份锁不是手抄版本号，而是**从已验证的 `requirements.lock` 里原样切出来**的
——哈希值一字不改，切片的可信度继承自生成它的那一次解析。

## 生成

    .venv/Scripts/python.exe packaging/make_relay_lock.py
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "requirements.lock"
DST = ROOT / "cloud" / "requirements.lock"

#: 中继的直接依赖 + 它们的传递依赖（闭包按 2026-10-26 的锁文件算出）。
RELAY_PACKAGES = (
    "anyio",
    "click",
    "colorama",
    "h11",
    "idna",
    "starlette",
    "typing-extensions",
    "uvicorn",
    "websockets",
)

HEADER = """\
# PhotoBridge 云端中继依赖锁（自动生成，勿手改）
#
# 生成：.venv/Scripts/python.exe packaging/make_relay_lock.py
# 安装：python -m pip install --require-hashes -r cloud/requirements.lock
#
# ★ 为什么单独一份：中继只要 starlette + uvicorn + websockets，电脑端那套
#   （Pillow/pillow-heif/pystray/watchdog...）一个都不需要。装全量锁会把一堆
#   用不上的 C 扩展搬上 2 vCPU 的云主机。
# ★ 强度不打折：每个包都有 sha256，安装时强制 `--require-hashes`（审计 F05）。
#   版本与哈希从 requirements.lock 原样切出，不手抄。
#
# 直接依赖 3 个，闭包共 {count} 个包。
"""


def parse(text: str) -> dict[str, str]:
    """返回 {包名: 段落原文}。"""
    out: dict[str, str] = {}
    name: str | None = None
    buf: list[str] = []
    for line in text.splitlines():
        if line and not line[0].isspace() and not line.startswith("#"):
            if name:
                out[name] = "\n".join(buf)
            name = line.split("==", 1)[0].split("[", 1)[0].strip().lower()
            buf = [line]
        elif name is not None:
            buf.append(line)
    if name:
        out[name] = "\n".join(buf)
    return {k: v.rstrip() + "\n" for k, v in out.items()}


def main() -> int:
    if not SRC.exists():
        print(f"缺少 {SRC}", file=sys.stderr)
        return 1
    src = parse(SRC.read_text(encoding="utf-8"))
    missing = [p for p in RELAY_PACKAGES if p not in src]
    if missing:
        print(f"锁文件里找不到：{', '.join(missing)}", file=sys.stderr)
        return 1

    body = []
    for pkg in RELAY_PACKAGES:
        block = src[pkg]
        if "--hash=" not in block:
            print(f"{pkg} 没有哈希，拒绝生成（F05 要求每个包都带 sha256）",
                  file=sys.stderr)
            return 1
        body.append(block)

    DST.write_text(
        HEADER.format(count=len(RELAY_PACKAGES)) + "\n" + "\n".join(body),
        encoding="utf-8",
        newline="\n",
    )
    print(f"已生成 {DST}（{len(RELAY_PACKAGES)} 个包）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

