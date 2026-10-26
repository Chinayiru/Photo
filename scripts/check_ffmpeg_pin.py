"""核对磁盘上的 ffmpeg/ffprobe 是否就是 `packaging/ffmpeg-pin.json` 冻结的那一份。

## 为什么需要这个脚本（审查 Q-03）

`video.ffmpeg_dir` 指向的是一份**不受版本控制**的二进制（`build/tools/ffmpeg/`，
`build/` 整体在 `.gitignore` 里）。也就是说：源码、锁文件、SBOM 都钉死了，
唯独真正解码用户视频的那个 50 MB 程序是"拿到什么用什么"。

这带来两个具体问题：

1. **供应链**：换掉 ffmpeg.exe 就能换掉解码行为，而仓库里没有任何东西会因此报错；
2. **可复现**：两台机器打包出来的发布包，用户看到的播放行为可能不同
   （不同版本的 HEVC 解码器对 10-bit 的处理不一样），而版本号看起来一模一样。

这个脚本把"哪一份"变成可核对的断言：跑一遍，对不上就退出码 1。
发布前必须过这一关（`docs/08 §打包` 的检查清单里列了它）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PIN = REPO / "packaging" / "ffmpeg-pin.json"


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_pin(path: Path = PIN) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def find_bin_dir(explicit: str = "") -> Path:
    """按 `video.find_bin()` 的顺序找一份**可用的目录**，返回它。

    优先用显式参数，其次读配置里的 `video.ffmpeg_dir`，最后试
    `build/tools/ffmpeg`（开发机上的固定位置）。
    """
    if explicit:
        return Path(explicit)
    try:
        sys.path.insert(0, str(REPO / "backend"))
        from app import config as cfgmod

        cfg = cfgmod.load()
        conf_dir = str((cfg.video or {}).get("ffmpeg_dir") or "")
        if conf_dir:
            return Path(conf_dir)
    except Exception:  # noqa: BLE001 - 读不到配置不是错误，继续往下找
        pass
    return REPO / "build" / "tools" / "ffmpeg"


def check(bin_dir: Path, pin: dict) -> list[str]:
    """返回问题列表（空 = 通过）。"""
    problems: list[str] = []
    for name, want in pin["files"].items():
        path = bin_dir / name
        if not path.exists():
            problems.append(f"缺少 {path}")
            continue
        size = path.stat().st_size
        if size != want["size"]:
            problems.append(f"{name} 大小不符：{size} != {want['size']}")
        got = sha256_of(path)
        if got != want["sha256"]:
            problems.append(f"{name} sha256 不符：{got} != {want['sha256']}")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="核对 ffmpeg 二进制与冻结清单")
    ap.add_argument("--dir", default="", help="ffmpeg 所在目录（默认读配置）")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    pin = load_pin()
    bin_dir = find_bin_dir(args.dir)
    if not args.quiet:
        print(f"冻结版本：{pin['version']}")
        print(f"来源：{pin['source_url']}")
        print(f"目录：{bin_dir}")
    problems = check(bin_dir, pin)
    if problems:
        for p in problems:
            print(f"FAIL {p}", file=sys.stderr)
        print(f"\n{len(problems)} 项不符。发布前必须用清单里的那一份 ffmpeg。",
              file=sys.stderr)
        return 1
    if not args.quiet:
        for name, want in pin["files"].items():
            print(f"ok   {name}  sha256={want['sha256'][:16]}…  {want['size']} B")
        print("ffmpeg 与冻结清单一致")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
