"""sandbox.py 的手工验收脚本（不依赖 pytest，直接跑）。

覆盖 docs/03 §4.5 与 docs/07 列出的绕过手法。每一条都必须被拒绝，
规则名要对得上 —— 规则名会写进审计日志，是事后追查的依据。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from app.config import RootConfig  # noqa: E402
from app.sandbox import SandboxError, is_within, normalize_rel, resolve, tmp_path  # noqa: E402

BASE = Path(__file__).resolve().parent.parent / "data" / "_sandbox_test"
ROOT = RootConfig(id="library", path=BASE, mode="readwrite", recursive=True)

#: (相对路径, need, 期望规则名 或 None 表示应当放行)
CASES: list[tuple[str, str, str | None]] = [
    # --- 应当放行 ---
    ("a.jpg", "read", None),
    ("sub/b.jpg", "read", None),
    ("sub/b.jpg", "write", None),
    ("sub/b.jpg", "delete", None),
    ("sub/./b.jpg", "read", None),
    ("sub\\b.jpg", "read", None),          # 反斜杠分隔符等价
    ("sub/b.jpg/", "read", None),          # 末尾斜杠无害
    (".photobridge_tmp/abc123.jpg", "read", None),

    # --- 步 1：形态 ---
    ("", "read", "empty_path"),
    ("   ", "read", "empty_path"),
    ("/etc/passwd", "read", "leading_separator"),
    ("\\windows\\x", "read", "leading_separator"),
    ("C:/Windows/x", "read", "drive_letter"),
    ("c:x", "read", "drive_letter"),
    ("//server/share/x", "read", "unc_path"),
    ("\\\\server\\share\\x", "read", "unc_path"),
    ("//?/C:/x", "read", "device_path"),
    ("\\\\?\\C:\\x", "read", "device_path"),
    ("\\\\.\\PhysicalDrive0", "read", "device_path"),

    # --- 步 2：段级 ---
    ("..", "read", "parent_traversal"),
    ("../x", "read", "parent_traversal"),
    ("sub/../../x", "read", "parent_traversal"),
    ("sub\\..\\..\\x", "read", "parent_traversal"),
    ("a//b", "read", "empty_segment"),
    ("a.jpg:stream", "read", "ntfs_data_stream"),
    ("sub/b.jpg:Zone.Identifier", "read", "ntfs_data_stream"),
    ("CON", "read", "reserved_name"),
    ("CON.txt", "read", "reserved_name"),
    ("sub/NUL", "read", "reserved_name"),
    ("com1.jpg", "read", "reserved_name"),
    ("lpt9", "read", "reserved_name"),
    ("a.jpg ", "read", "trailing_space_or_dot"),
    ("a.jpg.", "read", "trailing_space_or_dot"),

    # --- 步 3：字符与长度 ---
    ("a<b>.jpg", "read", "illegal_chars"),
    ('a"b.jpg', "read", "illegal_chars"),
    ("a|b.jpg", "read", "illegal_chars"),
    ("a?b.jpg", "read", "illegal_chars"),
    ("a*b.jpg", "read", "illegal_chars"),
    ("a\nb.jpg", "read", "illegal_chars"),
    ("x" * 300 + ".jpg", "read", "segment_too_long"),
    ("/".join(["x" * 100] * 20), "read", "path_too_long"),
]

PERM_CASES: list[tuple[str, str, str | None]] = [
    ("a.jpg", "read", None),
    ("a.jpg", "write", "write_not_permitted"),
    ("a.jpg", "delete", "delete_not_permitted"),
]


def main() -> int:
    BASE.mkdir(parents=True, exist_ok=True)
    (BASE / "sub").mkdir(exist_ok=True)
    (BASE / "a.jpg").write_bytes(b"x")
    (BASE / "sub" / "b.jpg").write_bytes(b"x")
    (BASE / ".photobridge_tmp").mkdir(exist_ok=True)

    fails = 0
    for rel, need, want in CASES:
        try:
            got = resolve(ROOT, rel, need)  # type: ignore[arg-type]
            rule = None
        except SandboxError as e:
            got, rule = None, e.rule
        ok = rule == want
        fails += not ok
        print(f"{'ok ' if ok else 'FAIL'} {need:6} {rel[:44]!r:48} want={want!r:24} got={rule!r}")

    # 只读 / 仅上传 根的权限矩阵
    readonly = RootConfig(id="ro", path=BASE, mode="readonly", recursive=True)
    upload = RootConfig(id="up", path=BASE, mode="upload", recursive=True)
    for rc, label in ((readonly, "readonly"), (upload, "upload")):
        for rel, need, want in PERM_CASES:
            try:
                resolve(rc, rel, need)  # type: ignore[arg-type]
                rule = None
            except SandboxError as e:
                rule = e.rule
            # upload 模式允许 write，readonly 不允许任何写
            expect = want
            if rc is upload and need == "write":
                expect = None
            if rc is readonly and need in ("write", "delete"):
                expect = "write_not_permitted" if need == "write" else "delete_not_permitted"
            ok = rule == expect
            fails += not ok
            print(f"{'ok ' if ok else 'FAIL'} [{label}] {need:6} want={expect!r:24} got={rule!r}")

    # upload_id 白名单
    for bad in ("../../evil", "short", "a" * 33, "abc/def", "abc def", ""):
        try:
            tmp_path(ROOT, bad)
            print(f"FAIL bad_upload_id accepted: {bad!r}")
            fails += 1
        except SandboxError as e:
            print(f"ok  bad_upload_id rejected: {bad!r:20} rule={e.rule}")

    # 工具函数
    assert normalize_rel("sub\\b.jpg") == "sub/b.jpg"
    assert normalize_rel("/a/b/") == "a/b"
    assert is_within(BASE / "sub", BASE)
    assert not is_within(BASE.parent / "other", BASE)
    print("ok  normalize_rel / is_within")

    print(f"\n{'ALL PASS' if fails == 0 else str(fails) + ' FAILURES'}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
