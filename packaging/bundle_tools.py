"""把 ffmpeg/ffprobe 铺进冻结产物：`dist/PhotoBridge/tools/ffmpeg/`。

## 为什么需要这一步

`video.find_bin()` 的查找顺序是：配置 `video.ffmpeg_dir` → 环境变量
`PHOTOBRIDGE_FFMPEG_DIR` → **exe 旁边的 `tools/ffmpeg(/bin)`** → PATH。
发布包不能指望用户机器上有 ffmpeg，也不能指望用户自己去改配置，所以打包后
必须把这两个 exe 拷到 exe 旁边 —— 这就是"开箱能播视频"的最后一公里。

## 为什么先核对哈希再拷

`build/tools/ffmpeg/` 不受版本控制（`build/` 在 `.gitignore` 里）。拷之前先按
`packaging/ffmpeg-pin.json` 对一遍，能拦住"拿着半年前那版 ffmpeg 打发布包"。

用法：

    .venv/Scripts/python.exe packaging/bundle_tools.py
    .venv/Scripts/python.exe packaging/bundle_tools.py --exe-dir dist/PhotoBridge
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from check_ffmpeg_pin import check as check_pin  # noqa: E402
from check_ffmpeg_pin import load_pin  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="把冻结的 ffmpeg 铺进发布目录")
    ap.add_argument("--src", default=str(REPO / "build" / "tools" / "ffmpeg"))
    ap.add_argument("--exe-dir", default=str(REPO / "dist" / "PhotoBridge"))
    ap.add_argument("--skip-verify", action="store_true",
                    help="跳过哈希核对（只在调试时用；发布流程必须核对）")
    args = ap.parse_args(argv)

    src = Path(args.src)
    dst = Path(args.exe_dir) / "tools" / "ffmpeg"
    if not dst.parent.parent.exists():
        print(f"找不到发布目录：{dst.parent.parent}（先跑 PyInstaller）",
              file=sys.stderr)
        return 1

    pin = load_pin()
    if not args.skip_verify:
        problems = check_pin(src, pin)
        if problems:
            for p in problems:
                print(f"FAIL {p}", file=sys.stderr)
            print("拒绝拷贝：源目录里的 ffmpeg 与冻结清单不一致。", file=sys.stderr)
            return 1

    dst.mkdir(parents=True, exist_ok=True)
    for name in pin["files"]:
        shutil.copy2(src / name, dst / name)
        print(f"copied {name} → {dst / name}")

    # 许可证：GPL 构建分发时必须随附
    lic = src / "LICENSE"
    if lic.exists():
        shutil.copy2(lic, dst / "LICENSE")
        print("copied LICENSE")
    else:
        print("提示：源目录没有 LICENSE 文件 —— 分发 GPL 构建时必须随附许可证",
              file=sys.stderr)

    total = sum((dst / n).stat().st_size for n in pin["files"])
    print(f"完成：{len(pin['files'])} 个二进制，共 {total / (1 << 20):.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
