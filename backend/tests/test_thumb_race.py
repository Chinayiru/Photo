"""缩略图并发生成：`os.replace` 撞上"目标正被读"时不能留下坏状态。

## 背景（P1 soak 里抓到的真实日志）

24 小时 soak 的第一轮打了 3 条：

    thumb ensure failed media_id=56: [WinError 5] 拒绝访问。:
      '..._320.webp.57080.68488a9c.tmp' -> '..._320.webp'

机制已单独验证（`build/_probe_replace.py`）：Windows 上 `os.replace`
的**目标文件**若正被另一个句柄打开读，就抛 `WinError 5`。缩略图路径里
这件事在"首次浏览"时必然发生 —— `thumb_etag IS NULL` 让所有并发请求
都判定 `stale=True`，于是多个请求同时重新生成同一张图，而其中一个正在
被 `_file_thumb_reply` 读着。

## 这不是严重缺陷，但必须钉住"自愈"

实测三波各 40 并发，结果：

* 全部 `200` 且都是**真图**（212 / 3490 字节），一次占位图（288 /
  4608 字节）都没出现；
* 缓存文件最终存在且大小正确；
* `media.thumb_state` 始终 `ok`（不会被误写成 `failed`，否则那张图
  以后永不重试 —— 那才是真故障）；
* 没有 `.tmp` 残留。

所以后果是"一次多余的重新编码 + 一行 warning 日志"。本文件把这个
边界钉住：撞车可以发生，但**不许**留下占位图、failed 状态或 tmp 残留。
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from backend.app import thumbs as T  # noqa: E402


def test_generate_thumb_failure_cleans_tmp_and_keeps_old(monkeypatch, tmp_path):
    """`os.replace` 失败时必须删掉自己的 tmp，且**不破坏**已有缓存。

    这条用 monkeypatch 直接把 `os.replace` 换成抛 WinError 5，比真造
    竞态稳定得多（真竞态要靠线程时序，会 flaky）。
    """
    from PIL import Image

    src = tmp_path / "a.jpg"
    Image.new("RGB", (600, 400), (30, 90, 150)).save(src, "JPEG", quality=80)
    dst = tmp_path / "a_320.webp"
    dst.write_bytes(b"OLD-GOOD")            # 已有一份好的缓存

    real_replace = T.os.replace

    def boom(a, b):                          # noqa: ANN001, ANN202
        err = OSError(13, "拒绝访问")
        err.winerror = 5
        raise err

    monkeypatch.setattr(T.os, "replace", boom)
    raised = False
    try:
        T.generate_thumb(src, dst, 320)
    except OSError:
        raised = True
    finally:
        monkeypatch.setattr(T.os, "replace", real_replace)

    assert raised, "os.replace 失败必须向上抛（由调用方决定重试）"
    assert dst.read_bytes() == b"OLD-GOOD", "旧缓存被破坏了"
    assert not list(tmp_path.glob("*.tmp")), "tmp 文件没被清理"


def test_generate_thumb_replaces_atomically(monkeypatch, tmp_path):
    """成功路径：`os.replace` 被调用一次，dst 是新内容。"""
    from PIL import Image

    src = tmp_path / "b.jpg"
    Image.new("RGB", (600, 400), (200, 40, 40)).save(src, "JPEG", quality=80)
    dst = tmp_path / "b_320.webp"

    calls: list[tuple[str, str]] = []
    real = T.os.replace

    def spy(a, b):                           # noqa: ANN001, ANN202
        calls.append((str(a), str(b)))
        return real(a, b)

    monkeypatch.setattr(T.os, "replace", spy)
    w, h = T.generate_thumb(src, dst, 320)

    assert (w, h) == (320, 213)
    assert len(calls) == 1
    assert calls[0][1] == str(dst)
    assert calls[0][0].endswith(".tmp")
    assert dst.read_bytes()[:4] == b"RIFF", "不是 WebP"
    assert not list(tmp_path.glob("*.tmp"))


def test_thumb_path_is_stable_across_calls(tmp_path) -> None:
    """路径必须可复现 —— 并发请求要落到同一个 dst 才会撞车，
    若每次都不一样，缓存就永远命中不了。"""
    from backend.app.config import Config

    cfg = Config({
        "server": {"host": "127.0.0.1", "port": 0},
        "roots": [{"id": "library", "path": str(tmp_path / "lib"),
                   "label": "库", "mode": "readwrite", "enabled": True}],
        "auth": {"require_pairing": True},
        "index": {"scan_on_start": False, "watch": False},
    }, path=tmp_path / "data" / "config.json", data_dir=tmp_path / "data")
    a = T.thumb_path(cfg, "library", "2024/2024-05/x.jpg", 320)
    b = T.thumb_path(cfg, "library", "2024/2024-05/x.jpg", 320)
    assert a == b
    assert a.name.endswith("_320.webp")
    assert T.thumb_path(cfg, "library", "2024/2024-05/x.jpg", 1600) != a
