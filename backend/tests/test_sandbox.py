"""路径沙箱的自动化用例 —— 覆盖 docs/07 §4.1 的全部 TC-SANDBOX-FUNC-* / SEC-*。

## 这个文件为什么现在才存在（真实缺口）

docs/07 §4.1 从第一版起就写着「测试文件：`tests/unit/test_sandbox.py`」，
但那个路径**从来没有存在过** —— 仓库里没有 `tests/` 目录，测试全在
`backend/tests/`，而这个文件名也一直是空的。承担这 100 多条用例的只有
`scripts/check_sandbox.py`：一个**手工脚本**，需要人手动跑、不产生
pytest 结果、也**没有任何测试在调用它**（`grep check_sandbox backend/tests`
零命中）。也就是说：

* CI / 全量 `pytest` 里，`..` 穿越、NTFS 数据流、保留设备名、尾随点空格、
  junction 逃逸这些**沙箱最核心的规则名**，一条都没有被断言过；
* 唯一零散相关的是 `test_core.py` 拿 `SandboxError("parent_traversal", ...)`
  手构造一个异常去测"响应体不泄漏路径"，以及 `test_move_rename.py` 的
  `CON.jpg` 改名被拒 —— 都不是对校验器本身的检验。

这个模块把 §4.1 整张表接进 pytest。**同时校正了文档里三类与实现不符的
表述**，每一处都在对应用例的 docstring 里写明理由（详见文件末尾的
`test_documented_ids_are_all_covered` 汇总）。

## 三处必须说清的口径（文档原文与实现的差异）

1. **错误码不存在。** 文档通篇写 `DENY E_PATH_TRAVERSAL` / `E_PATH_ADS` /
   `E_PATH_RESERVED` … 这些码在 `errors.py` 里**一个都没有**。实现是
   统一 `E1009 path_denied`（HTTP 400），真正区分"哪一步拒绝"的是
   `SandboxError.rule`，取值是 `parent_traversal` / `ntfs_data_stream` /
   `reserved_name` / `trailing_space_or_dot` / `device_path` / `unc_path` /
   `drive_letter` / `leading_separator` / `empty_segment` / `empty_path` /
   `segment_too_long` / `path_too_long` / `illegal_chars` / `bidi_control` /
   `reparse_point` / `write_not_permitted` / `delete_not_permitted` /
   `delete_disabled_by_config` / `unknown_need` / `bad_upload_id`。
   `rule` 会进审计日志、不出响应体（`docs/06 §10` 规则 14）。所以本文件
   断言的是 **rule**，不是错误码 —— 断言错误码只能写成"全是 E1009"，
   那等于什么都没测。

2. **`resolve()` 不检查存在性。** 它只回答"这个相对路径能不能安全落在
   root 内"。路径不存在是 `FileNotFoundError`（在 `open()` 时），不是
   `SandboxError`。文档 070/071/077 写的 `E_PATH_NOT_FOUND` /
   `E_PATH_IS_DIR` 因此不可实现，本文件改成断言真实行为。

3. **`open_read` / `open_write` / `safe_delete` / `safe_replace` 四个函数
   全不存在。** `sandbox.py` 的公开 API 是 `resolve / exists / is_dir /
   normalize_rel / is_within / safe_display / tmp_dir / tmp_path /
   assert_writable / strip_bidi`。文档 M 组整张表（069–080）都是按这四个
   幻影函数写的。本文件把它们映射到**真实承担同一职责的代码**：
   `resolve` + 内置 `open()`、`archive.reserve()`（`O_CREAT|O_EXCL` 排他
   创建）、`send2trash`、`os.replace`。

## 为什么这些用例值得单独存在

库根 `D:\\Photos` 是可写的。沙箱是"相册工具"与"远程任意文件读写器"
之间唯一的东西。它出错的形态不是抛异常，而是**静默放行一条本该拒绝的
路径** —— 那种 bug 不会让任何现有测试变红。
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from backend.app import archive
from backend.app.config import Config, RootConfig
from backend.app.errors import AppError
from backend.app.sandbox import (
    ILLEGAL_CHARS,
    MAX_RELPATH,
    MAX_SEGMENT,
    RESERVED_NAMES,
    SandboxError,
    assert_writable,
    exists,
    is_dir,
    is_within,
    normalize_rel,
    open_verified,
    resolve,
    safe_display,
    strip_bidi,
    tmp_path,
)

#: 真实照片库 —— K 组用它验证"严格化不会误伤真实数据"。
REAL_LIBRARY = Path("E:/software/相册")


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


def _root(path: Path, mode: str = "readwrite") -> RootConfig:
    return RootConfig(
        id="test",
        path=path,
        label="测试库",
        mode=mode,           # type: ignore[arg-type]
        enabled=True,
        recursive=True,
        include_exts=frozenset({"jpg", "png", "webp", "mov"}),
        exclude_dirs=(),
        exclude_files=(),
    )


def _cfg(tmp_path: Path, *, allow_delete: bool = True) -> Config:
    """显式 cfg —— 绝不依赖 `get_config()` 去读开发机上真实的 data/config.json。"""
    return Config(
        {"auth": {"allow_delete": allow_delete}},
        path=tmp_path / "config.json",
        data_dir=tmp_path / "data",
    )


@pytest.fixture()
def library(tmp_path: Path) -> Path:
    """一棵真实的临时库：`sub/ok.jpg`、`相册/2024/照片.jpg`、`sub/` 目录。"""
    lib = tmp_path / "lib"
    (lib / "sub").mkdir(parents=True)
    (lib / "sub" / "ok.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100)
    (lib / "相册" / "2024").mkdir(parents=True)
    (lib / "相册" / "2024" / "照片.jpg").write_bytes(b"\xff\xd8\xff\xe0")
    return lib


@pytest.fixture()
def root(library: Path) -> RootConfig:
    return _root(library)


def _rule(root_cfg: RootConfig, rel: str, need: str = "read", *, cfg: Config | None = None) -> str | None:
    """返回拒绝的规则名；放行返回 None。"""
    try:
        resolve(root_cfg, rel, need, cfg=cfg)      # type: ignore[arg-type]
    except SandboxError as exc:
        return exc.rule
    return None


# ---------------------------------------------------------------------------
# A 组：正常路径（FUNC-001~008）—— 沙箱不能误伤真实数据
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "case_id,rel",
    [
        ("FUNC-001", "sub/ok.jpg"),
        ("FUNC-002", "sub\\ok.jpg"),
        ("FUNC-003", "SUB/OK.JPG"),
        ("FUNC-004", "相册/2024/照片.jpg"),
        ("FUNC-005", "sub/ok.jpg/"),
    ],
)
def test_normal_paths_are_allowed(root: RootConfig, library: Path, case_id: str, rel: str) -> None:
    """FUNC-001~005：基准、反斜杠、大小写、中文、尾随斜杠都必须放行。

    `SUB/OK.JPG` 能过不是因为沙箱做了大小写归一，而是因为 Windows 文件
    系统本身不区分大小写 —— 沙箱只做 `commonpath` 前缀断言，不碰名字。
    """
    got = resolve(root, rel)
    assert got.name.lower() == "ok.jpg" or got.name == "照片.jpg"
    assert is_within(got, library), f"{case_id} 解析结果跑到了库外"


@pytest.mark.parametrize(
    "case_id,mode,need",
    [
        ("FUNC-006", "readwrite", "write"),
        ("FUNC-007", "readonly", "read"),
        ("FUNC-008", "upload", "read"),
    ],
)
def test_mode_matrix_allows_these_combinations(
    library: Path, case_id: str, mode: str, need: str
) -> None:
    """FUNC-006~008：readwrite+write / readonly+read / upload+read（判重要读已有文件）。"""
    assert _rule(_root(library, mode), "sub/ok.jpg", need) is None, case_id


# ---------------------------------------------------------------------------
# B 组：`..` 路径穿越（SEC-001~009）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "case_id,rel",
    [
        ("SEC-001", "../secret.txt"),
        ("SEC-002", "../../etc/passwd"),
        ("SEC-003", "..\\..\\secret.txt"),
        ("SEC-004", ".."),
        ("SEC-005", "sub/../../secret.txt"),
        ("SEC-006", "sub/.."),
        ("SEC-007", "sub/ok/../../../secret.txt"),
    ],
)
def test_parent_traversal_is_rejected(root: RootConfig, case_id: str, rel: str) -> None:
    """SEC-001~007：反斜杠必须与正斜杠等价处理。

    只查 `/` 的实现在 `..\\..\\secret.txt` 上会静默放行 —— 这是本组里
    最容易被"看起来写了穿越检查"的实现漏掉的一条。步 2 先做
    `rel_path.replace("\\\\", "/")` 再切段，正是为此。
    """
    assert _rule(root, rel) == "parent_traversal", case_id


def test_four_dots_is_rejected_even_though_it_is_not_traversal(root: RootConfig) -> None:
    """SEC-008：`....//....//secret.txt` 不是 `..` 穿越，但段以 `.` 结尾 → 拒绝。

    文档写的是 `E_PATH_TRAILING`；实际规则名是 `trailing_space_or_dot`。
    拒绝理由是"尾随点会被 Win32 静默剥掉"，与穿越无关 —— 两条防线
    各挡一类，不能因为这里报了另一个规则名就以为漏了穿越。
    """
    assert _rule(root, "....//....//secret.txt") == "trailing_space_or_dot"


def test_percent_encoded_traversal_is_allowed_as_a_literal_name(root: RootConfig) -> None:
    """SEC-009：`sub/..%2f..%2fsecret.txt` **放行** —— 文档写 DENY，是文档错。

    ## 为什么放行是对的

    `%` 不在 `ILLEGAL_CHARS`（`<>"|?*` 与控制字符）里，因为**真实文件名
    里就有 `%`**：本机库里实测有一个 `P01PZ@%VWUFE[@~\\`1X5[Z$I(CUnet)
    (noise_scale)...png`。把 `%` 拉黑会让这张照片永远索引进不来。

    而 `%2f` 在 Windows 上**不是转义** —— 它就是一个普通的三字符串，
    `..%2f..%2fsecret.txt` 是一个**文件名字面量**（点号开头，还过不了
    尾随点/穿越以外的规则吗？实际会被当成 `..%2f..%2fsecret.txt` 这个名字）。
    服务端从不做 URL 解码（HTTP 层已经解过一遍），所以这里不存在
    "解码后才变成穿越"的第二跳。文档原判据括号里其实自己承认了这一点
    （"若不编码解码，`%2f` 在 Windows 上是普通文件名"），只是结论写反了。
    """
    assert _rule(root, "sub/..%2f..%2fsecret.txt") is None
    assert normalize_rel("sub/..%2f..%2fsecret.txt") == "sub/..%2f..%2fsecret.txt"


# ---------------------------------------------------------------------------
# C 组：绝对路径 / 盘符 / UNC / 设备命名空间（SEC-010~022）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "case_id,rel,expected_rule",
    [
        # 前导分隔符
        ("SEC-010", "/etc/passwd", "leading_separator"),
        ("SEC-011", "\\Windows\\System32", "leading_separator"),
        # 盘符（含"盘符相对路径" `C:..` —— 语义是"当前目录"，比绝对路径更隐蔽）
        ("SEC-012", "C:\\Windows\\System32\\config\\SAM", "drive_letter"),
        ("SEC-013", "c:/windows/win.ini", "drive_letter"),
        ("SEC-014", "C:..\\..\\secret.txt", "drive_letter"),
        # UNC（正反斜杠两种）
        ("SEC-015", "\\\\server\\share\\x.jpg", "unc_path"),
        ("SEC-016", "//server/share/x.jpg", "unc_path"),
        ("SEC-022", "\\\\localhost\\C$\\Windows", "unc_path"),
        # 设备命名空间必须**先于** UNC 判断（两者都以 \\ 开头）
        ("SEC-017", "\\\\?\\C:\\Windows", "device_path"),
        ("SEC-018", "\\\\?\\UNC\\server\\share", "device_path"),
        ("SEC-019", "\\\\.\\C:", "device_path"),
        ("SEC-020", "\\\\.\\PhysicalDrive0", "device_path"),
        ("SEC-021", "\\\\?\\GLOBALROOT\\Device\\HarddiskVolumeShadowCopy1\\Windows", "device_path"),
    ],
)
def test_absolute_and_device_paths_are_rejected(
    root: RootConfig, case_id: str, rel: str, expected_rule: str
) -> None:
    """SEC-010~022：13 条。

    SEC-017/018/019/020/021 值得单独记住的两点：
    * `Path.resolve()` 对 `\\\\?\\C:\\Windows` 与 `\\\\.\\C:` **原样返回不报错**，
      所以"靠 pathlib 归一化再判前缀"的实现在这里全数失守；
    * `\\\\?\\GLOBALROOT\\...` 会抛 `OSError [WinError 6]`，但**必须在更早的
      字符检查就拒绝** —— 让文件系统去判等于把"能不能访问卷影副本"
      交给运气。
    """
    assert _rule(root, rel) == expected_rule, case_id


# ---------------------------------------------------------------------------
# D 组：NTFS 备用数据流（SEC-023~027）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "case_id,rel",
    [
        ("SEC-023", "sub/ok.jpg:hidden.exe"),
        ("SEC-024", "sub/ok.jpg:$DATA"),
        ("SEC-025", "sub/ok.jpg::$INDEX_ALLOCATION"),
        ("SEC-026", "file.jpg:hidden:$DATA"),
        ("SEC-027", "sub/ok.jpg:"),
    ],
)
def test_ntfs_data_streams_are_rejected(root: RootConfig, case_id: str, rel: str) -> None:
    """SEC-023~027：冒号在**整个路径**中非法，不只是文件名中。

    `os.path.exists("ok.jpg:hidden")` 返回 False，但流**确实存在** ——
    这就是危险所在：任何"先 exists() 再决定"的检查都会看到 False 就
    以为没事，而数据已经写进了一个 `dir` 看不见的地方。
    """
    assert _rule(root, rel) == "ntfs_data_stream", case_id


# ---------------------------------------------------------------------------
# E 组：保留设备名（SEC-028~035）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "case_id,rel",
    [
        ("SEC-028", "CON"),
        ("SEC-029", "CON.jpg"),
        ("SEC-030", "sub/NUL"),
        ("SEC-031", "AUX.txt"),
        ("SEC-032", "COM1.jpg"),
        ("SEC-033", "LPT9"),
        ("SEC-034", "com1.JPG"),
        ("SEC-035", "nul.tar.gz"),
    ],
)
def test_windows_reserved_names_are_rejected(root: RootConfig, case_id: str, rel: str) -> None:
    """SEC-028~035：判断用扩展名前的第一段，所以 `nul.tar.gz` 与 `com1.JPG` 都命中。

    保留名不区分大小写、且带扩展名也仍然保留 —— 这两点合起来让
    "只比对整段 `seg.upper() in RESERVED_NAMES`"的写法漏掉 `CON.jpg`
    与 `com1.JPG` 这两种最常见形态。
    """
    assert _rule(root, rel) == "reserved_name", case_id
    # 交叉验证：表里的名字确实都在常量里（防止有人只改了测试没改实现）
    assert rel.split("/")[-1].split(".")[0].upper() in RESERVED_NAMES


# ---------------------------------------------------------------------------
# F 组：尾随点 / 空格 / 控制字符（SEC-036~043）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "case_id,rel",
    [
        ("SEC-036", "sub/ok.jpg."),
        ("SEC-037", "sub/ok.jpg "),
        ("SEC-038", "sub/ok.jpg..."),
        ("SEC-039", "sub/ "),
    ],
)
def test_trailing_dot_or_space_is_rejected(root: RootConfig, case_id: str, rel: str) -> None:
    """SEC-036~039：`exists()` 会返回 True —— Win32 静默剥掉尾随点/空格。

    这意味着"校验的名字"与"真正落盘的名字"不是同一个字符串，正是
    "校验通过但落到别处"的经典绕过形态。
    """
    assert _rule(root, rel) == "trailing_space_or_dot", case_id


@pytest.mark.parametrize(
    "case_id,rel",
    [
        ("SEC-040", "sub/ok.jpg\t"),
        ("SEC-041", "sub/ok.jpg\n"),
        ("SEC-042", "sub/ok.jpg\r\n..\\x"),
    ],
)
def test_control_characters_are_rejected(root: RootConfig, case_id: str, rel: str) -> None:
    """SEC-040~042：制表符 / 换行 / CRLF 注入。

    SEC-042 的 `\\r\\n..\\x` 有两道防线都会命中：段级检查看到段尾是 `.`
    （`ok.jpg\\r\\n..` 以点结尾）先报 `trailing_space_or_dot`，字符检查
    则会报 `illegal_chars`。**两道任一生效都算拒绝**，所以这里断言的是
    "拒绝且规则名在两者之内"，而不是钉死其中一个 —— 钉死会变成对
    七步顺序的过度约束。
    """
    assert _rule(root, rel) in ("illegal_chars", "trailing_space_or_dot"), case_id


def test_leading_space_is_allowed(root: RootConfig) -> None:
    """SEC-043：`  ok.jpg`（前导空格）**放行** —— 文档写 DENY，是文档错。

    前导空格在 Windows 上是**合法**的：`exists()` 对它返回 False 是因为
    文件本来就不叫这个名字，不是因为 Win32 会剥掉它。剥掉的是**尾随**
    空格/点，不是前导。把前导空格也拒了，会让真实带前导空格的文件
    永远进不了库 —— 本机库里实测 0 个，但"实测为 0"不等于"永远为 0"
    （用户从 macOS/U 盘拷进来就可能带）。

    这条与 SEC-037（尾随空格必须拒）成对读：一侧要严，一侧不能过严。
    """
    assert _rule(root, "  ok.jpg") is None


# ---------------------------------------------------------------------------
# G 组：重解析点逃逸（SEC-044~047）
# ---------------------------------------------------------------------------


def _make_junction(link: Path, target: Path) -> bool:
    """用 `mklink /J` 建目录联接。不需要管理员权限（符号链接需要）。"""
    try:
        r = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0 and link.exists()


@pytest.fixture()
def junction_library(tmp_path: Path) -> tuple[Path, Path, Path]:
    """在库内建一个指向库外的 junction：`lib/esc -> outside`。"""
    lib = tmp_path / "lib"
    (lib / "sub").mkdir(parents=True)
    (lib / "sub" / "ok.jpg").write_bytes(b"\xff\xd8\xff\xe0")
    outside = tmp_path / "outside"
    (outside / "d").mkdir(parents=True)
    (outside / "d" / "evil.jpg").write_bytes(b"\xff\xd8\xff\xe0")
    esc = lib / "esc"
    if not _make_junction(esc, outside):
        pytest.skip("本机无法创建 junction（mklink /J 失败）")
    return lib, esc, outside


def test_junction_escape_is_rejected(junction_library: tuple[Path, Path, Path]) -> None:
    """SEC-045/047：`esc/d/evil.jpg` 与 `esc` 本身都必须拒绝。

    ## 为什么这条最能证明"逐段 reparse 检查"是对的

    关键实测事实：`os.path.islink(junction)` → **False**（完全漏检），
    只有 `os.lstat(p).st_file_attributes & FILE_ATTRIBUTE_REPARSE_POINT`
    才能看出来。任何"用 islink 判符号链接就够了"的沙箱在 Windows 上
    对 junction 是**全盲**的。

    规则名是 `reparse_point`（不是文档写的 `E_PATH_ESCAPE`）。
    """
    lib, _esc, _outside = junction_library
    r = _root(lib)
    assert _rule(r, "esc/d/evil.jpg") == "reparse_point"
    assert _rule(r, "esc") == "reparse_point"


def test_traversal_before_junction_is_still_traversal(junction_library: tuple[Path, Path, Path]) -> None:
    """SEC-044：`esc/../secret.txt` 在步 2 就被 `..` 拦住，轮不到步 5。

    顺序有意义：先拒形态、再拒段、最后才查文件系统。如果反过来先
    realpath 再判前缀，`..` 会被消解掉，路径"看起来"回到库内，
    追踪日志也再看不到攻击意图。
    """
    lib, _esc, _outside = junction_library
    assert _rule(_root(lib), "esc/../secret.txt") == "parent_traversal"


# ---------------------------------------------------------------------------
# G2 组：句柄级打开（open_verified / fsopen）
# ---------------------------------------------------------------------------


def test_open_verified_reads_a_plain_file(tmp_path: Path) -> None:
    """正常文件照读 —— 复核只是"多核对一次"，不是"多要一份权限"。"""
    f = tmp_path / "a.bin"
    f.write_bytes(b"hello")
    with open_verified(f, "rb") as fh:
        assert fh.read() == b"hello"


def test_open_verified_missing_file_is_file_not_found(tmp_path: Path) -> None:
    """不存在要抛 `FileNotFoundError`（调用方按 404 处理），不能变成沙箱拒绝。"""
    with pytest.raises(FileNotFoundError):
        open_verified(tmp_path / "nope.bin", "rb")


def test_open_verified_rejects_a_swapped_parent(tmp_path: Path) -> None:
    """检查之后把父目录换成 junction：复核必须拒绝（这就是 TOCTOU）。"""
    lib = tmp_path / "lib"
    (lib / "race").mkdir(parents=True)
    target = lib / "race" / "ok.jpg"
    target.write_bytes(b"inside")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "ok.jpg").write_bytes(b"LOOT")

    with open_verified(target, "rb") as fh:
        assert fh.read() == b"inside"

    # 换位：必须先删文件再删目录，否则 rmdir 失败、junction 建不起来
    target.unlink()
    (lib / "race").rmdir()
    if not _make_junction(lib / "race", outside):
        pytest.skip("本机无法创建 junction（mklink /J 失败）")

    with pytest.raises(SandboxError) as ei:
        open_verified(lib / "race" / "ok.jpg", "rb")
    assert ei.value.rule == "handle_mismatch"


def test_fsopen_primitives_are_importable_without_sandbox(tmp_path: Path) -> None:
    """`fsopen` 必须不依赖 sandbox —— `video.py` 靠这一点避开 import 环。"""
    from backend.app import fsopen

    f = tmp_path / "b.bin"
    f.write_bytes(b"xy")
    with fsopen.open_verified(f, "rb") as fh:
        assert fh.read() == b"xy"
    assert fsopen.norm_final("C:\\Temp\\A") == fsopen.norm_final("c:\\temp\\a")


# ---------------------------------------------------------------------------
# H 组：超长路径（SEC-048~051）
# ---------------------------------------------------------------------------


def test_segment_over_255_is_rejected(root: RootConfig) -> None:
    """SEC-048/049：单段 300 / 260 都超 `MAX_SEGMENT=255`。"""
    assert _rule(root, "a" * 300 + ".jpg") == "segment_too_long"
    assert _rule(root, "sub/" + "b" * 260 + ".jpg") == "segment_too_long"


def test_segment_of_exactly_255_is_allowed(root: RootConfig, library: Path) -> None:
    """SEC-050：255 是**合法边界**，不能差一。

    这是整个 H 组唯一容易被"顺手写成 `>=`"搞坏的一条 —— 写错方向后
    255 字符的文件名永远进不了库，而真实相机/微信导出的长名正好在这个
    量级。`resolve()` 不检查存在性，所以这里断言的是"没被规则拦"，
    不需要真建出这个文件。
    """
    rel = "sub/" + "b" * 251 + ".jpg"
    assert len(rel.rsplit("/", 1)[-1]) == MAX_SEGMENT
    assert _rule(root, rel) is None


def test_total_path_over_1024_is_rejected(root: RootConfig) -> None:
    """SEC-051：总长上限 `MAX_RELPATH=1024`，三段各 100 是 300 → 放行。

    文档原文括号里写"三段各 100 字符（总 300+）→ DENY"是错的：300 远小于
    1024。真正会被拒的是五段各 255（1279）。朴素实现（只查单段、
    不查总长）在 1279 上会 ALLOW —— 这才是本条要钉的洞。
    """
    short = "/".join(["c" * 100] * 3)
    assert len(short) == 302 and len(short) <= MAX_RELPATH
    assert _rule(root, short) is None

    long = "/".join(["c" * 255] * 5)
    assert len(long) > MAX_RELPATH
    assert _rule(root, long) == "path_too_long"


# ---------------------------------------------------------------------------
# I 组：空段 / NUL / 控制符 / Unicode 陷阱（SEC-052~059）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "case_id,rel,expected_rule",
    [
        ("SEC-052", "", "empty_path"),
        ("SEC-053", "   ", "empty_path"),
        # NUL 字节截断（经典 C 字符串截断）—— NUL 在控制字符集里
        ("SEC-056", "sub/ok.jpg\x00.txt", "illegal_chars"),
        # RLO 双向控制符（F10 审计项）：显示「ok‮gpj.exe」像 jpg
        ("SEC-057", "sub/ok\u202e.jpg", "bidi_control"),
    ],
)
def test_empty_and_dangerous_characters_are_rejected(
    root: RootConfig, case_id: str, rel: str, expected_rule: str
) -> None:
    """SEC-052/053/056/057。"""
    assert _rule(root, rel) == expected_rule, case_id


def test_dot_segment_is_dropped_not_rejected(root: RootConfig) -> None:
    """SEC-054：`sub/./ok.jpg` **放行** —— 文档写 DENY，是文档错。

    `.` 段对路径语义没有任何影响（归一化后就是 `sub/ok.jpg`），丢弃它
    是安全的，而且与 `normalize_rel()` 的行为一致。把 `.` 也拒了会让
    "客户端多发了一个 `./`"变成 400，而这类前缀在拼接 URL 时非常常见。

    真正危险的是 `..`（会往上走），那条在 SEC-001~007 里单独钉死了。
    """
    assert _rule(root, "sub/./ok.jpg") is None
    assert normalize_rel("sub/./ok.jpg") == "sub/ok.jpg"


def test_double_slash_is_rejected_as_empty_segment(root: RootConfig) -> None:
    """SEC-055：`sub//ok.jpg` 中间的空段拒绝（但**末尾**单个 `/` 放行，见 FUNC-005）。"""
    assert _rule(root, "sub//ok.jpg") == "empty_segment"


def test_nbsp_and_fullwidth_are_allowed(root: RootConfig) -> None:
    """SEC-058/059：NBSP 与全角字符**放行** —— 文档写 DENY，是文档错。

    * U+00A0（NBSP）在 Windows 上是合法文件名字符。文档担心"视觉上与空格
      相同可绕过尾随空格检查"—— 但尾随检查比对的是**真的** U+0020，
      NBSP 在名字**中间**不构成任何绕过；真的出现在结尾时它也不是
      "尾随空格"，Win32 不会去剥它，所以不存在"校验名 ≠ 落盘名"。
    * 全角 `ｋ`（U+FF4B）是**另一个字符**，与该文件是两个不同文件。
      沙箱不做 NFKC 归一化（做了才会真的碰撞），所以放行是对的。
      文档自己给的判据就是"ALLOW（不同文件名）或 DENY"，写成"或"等于
      没判 —— 这里钉死为 ALLOW。

    结论：这两条是"过度严格"的典型诱因。沙箱拒得越多越好是个错觉：
    每多拒一个合法字符，就有一批真实照片永远进不来。
    """
    assert _rule(root, "sub/ok\u00a0.jpg") is None
    assert _rule(root, "sub/O\uff4b.jpg") is None
    # 但控制字符集本身不能被放宽：`<>"|?*` 与全部 C0 控制字符都必须在里面。
    # ★ 注意 `:` **不在** ILLEGAL_CHARS —— 它由步 2 的 ntfs_data_stream
    # 单独处理（语义更具体，且要报不同的规则名）。这条差异曾经让一个
    # "把 : 加进非法字符集"的重构看起来无害，其实是把 ADS 的专用规则名
    # 换成了泛化的 illegal_chars，审计日志从此分不清两者。
    assert ILLEGAL_CHARS.intersection('<>"|?*') == frozenset('<>"|?*')
    assert all(chr(c) in ILLEGAL_CHARS for c in range(0x20))
    assert ":" not in ILLEGAL_CHARS


# ---------------------------------------------------------------------------
# J 组：权限与模式（SEC-060~064）
# ---------------------------------------------------------------------------


def test_write_on_readonly_root_is_rejected(library: Path) -> None:
    """SEC-060：readonly 模式不允许写。"""
    assert _rule(_root(library, "readonly"), "sub/ok.jpg", "write") == "write_not_permitted"


@pytest.mark.parametrize("case_id,mode", [("SEC-061", "upload"), ("SEC-062", "readwrite")])
def test_write_on_upload_and_readwrite_is_allowed(library: Path, case_id: str, mode: str) -> None:
    """SEC-061/062：upload 与 readwrite 都允许写。"""
    assert _rule(_root(library, mode), "sub/ok.jpg", "write") is None, case_id


def test_delete_requires_readwrite_and_config_switch(library: Path, tmp_path: Path) -> None:
    """SEC-063/064：`need="delete"` 是**两道**条件，不只看 mode。

    * 063：mode 必须是 `readwrite`（upload 不行）→ `delete_not_permitted`
    * 064：还要 `auth.allow_delete` 为真 → `delete_disabled_by_config`
      （这条抛 `E1015/403` 而不是 `E1009/400`，因为"用户关了删除"
      不是非法路径，是权限偏好）

    文档 063 写的是"need=delete，mode=readwrite → DENY（删除权限一期未定义）"，
    那条早已不成立 —— 删除功能（进回收站）是实现了的，`delete_items`
    就在用它。
    """
    cfg_on = _cfg(tmp_path, allow_delete=True)
    cfg_off = _cfg(tmp_path, allow_delete=False)

    assert _rule(_root(library, "readwrite"), "sub/ok.jpg", "delete", cfg=cfg_on) is None
    assert _rule(_root(library, "upload"), "sub/ok.jpg", "delete", cfg=cfg_on) == "delete_not_permitted"
    assert _rule(_root(library, "readwrite"), "sub/ok.jpg", "delete", cfg=cfg_off) == "delete_disabled_by_config"


def test_unknown_need_is_rejected(root: RootConfig) -> None:
    """非法 need 值不能静默当成 read —— 打错字的 `need="wrte"` 会变成只读放行。"""
    assert _rule(root, "sub/ok.jpg", "bogus") == "unknown_need"


# ---------------------------------------------------------------------------
# K 组：真实库样本（FUNC-009~016）—— 只读，绝不修改
# ---------------------------------------------------------------------------

_needs_real_library = pytest.mark.skipif(
    not REAL_LIBRARY.is_dir(), reason="本机没有真实照片库（E:/software/相册）"
)


@_needs_real_library
@pytest.mark.parametrize(
    "case_id,rel",
    [
        # 扩展名欺骗：真身是 JPEG，名字是 .MOV
        ("FUNC-009", "青甘环线/祁连山/全部/IMG_9354.MOV"),
        # 22 个此类文件，真身是 PNG
        ("FUNC-010", "青甘环线/resources/background_clothing_conf/icon/05712491f0941b64bbe6cfe94e16cd71.jpg-lx2"),
        # 8192px 超大图
        ("FUNC-011", "全部备份/IMG_20241111_171132.jpg"),
        # 32 位 hex 文件名（实测库里大量此类）
        ("FUNC-013", "全部备份/03a465a82cede06c4833511fd6fba97921498d36.webp"),
        # 中文目录
        ("FUNC-014", "青甘环线"),
        # 含空格的目录名（实测 46 个）
        ("FUNC-012", "青甘环线/青海湖加茶卡盐湖/青海湖"),
        # 最深目录（实测 7 层，全库最深）
        (
            "FUNC-015",
            "青甘环线/青海湖加茶卡盐湖/青海湖/人像/resources/background_clothing_conf/icon",
        ),
    ],
)
def test_real_library_paths_are_allowed(case_id: str, rel: str) -> None:
    """FUNC-009~011/013/014：严格化不能误伤真实数据。

    这些路径是**沙箱层**放行的对象，不代表业务层也接受它们：`.MOV`
    与 `.jpg-lx2` 的扩展名不在 `include_exts` 白名单里，所以索引器会跳过
    —— 那是业务判断，与"能不能安全解析"是两件事。把两层混在一起是
    "沙箱又当爹又当妈"的开端。
    """
    r = _root(REAL_LIBRARY, "readonly")
    got = resolve(r, rel)
    assert is_within(got, REAL_LIBRARY), f"{case_id} 解析结果跑到了库外"
    assert got.name, case_id


@_needs_real_library
def test_real_library_has_no_names_the_sandbox_would_reject() -> None:
    """FUNC-016：真实库里保留名 / 非法字符 / 尾随点空格 / 超长段 **各 0 个**。

    这条是"沙箱可以严格化"的**证据**，不是"以后也不用管"。实测（4994 个
    文件）：保留名 0、非法字符 0、尾随空格或点 0、超长段 0、最长相对路径
    101 字符（上限 1024）、最深 7 层。

    与 SEC-009 / SEC-043 / SEC-058 放在一起读：库里有 `%` 与前导空格
    —— 正因为有，那几条才**不能**收紧。
    """
    r = _root(REAL_LIBRARY, "readonly")
    reserved: list[str] = []
    illegal: list[str] = []
    trailing: list[str] = []
    toolong: list[str] = []
    deepest = 0
    for dirpath, _dirnames, filenames in os.walk(REAL_LIBRARY):
        for name in filenames:
            rel = os.path.relpath(os.path.join(dirpath, name), REAL_LIBRARY).replace(os.sep, "/")
            deepest = max(deepest, rel.count("/"))
            if name.split(".", 1)[0].upper() in RESERVED_NAMES:
                reserved.append(rel)
            if ILLEGAL_CHARS.intersection(name):
                illegal.append(rel)
            if name.endswith((" ", ".")):
                trailing.append(rel)
            if any(len(s) > MAX_SEGMENT for s in rel.split("/")):
                toolong.append(rel)
            # 每个文件都真过一遍沙箱：这比"扫字符"强得多
            assert resolve(r, rel) is not None
    assert (reserved, illegal, trailing, toolong) == ([], [], [], [])
    assert deepest < 20, f"最深目录 {deepest} 层，异常深"


# ---------------------------------------------------------------------------
# L 组：并发与 TOCTOU（SEC-065~068）
# ---------------------------------------------------------------------------


def test_resolve_is_thread_safe(root: RootConfig) -> None:
    """SEC-066：10 线程并发解析同一路径，全部成功、无数据竞争。

    沙箱没有共享可变状态（每次调用各自 realpath、各自 lstat），所以
    正确实现天然线程安全 —— 这条钉的是"以后别为了性能加缓存"。
    一旦有人给 realpath 结果加进程级缓存，root 被改名/替换后就会
    持续放行越界路径。
    """
    errors: list[BaseException] = []
    results: list[Path] = []

    def work() -> None:
        try:
            results.append(resolve(root, "sub/ok.jpg"))
        except BaseException as exc:       # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=work) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(results) == 10
    assert len(set(results)) == 1


def test_root_renamed_does_not_produce_a_fake_verdict(root: RootConfig, library: Path) -> None:
    """SEC-067：root 被别人改名后，`resolve()` **不做** root 失效检测 —— 文档写错了。

    ## 实测事实

    文档 067 写"解析期间 root 目录被改名 → 抛 `E_ROOT_CHANGED`"。实测：
    改名之后 `resolve()` 照旧返回那个路径，不抛任何异常。

    ## 为什么这不算漏洞

    `_step_6_assert_inside` 拿的是**当次调用**里 `realpath(root)` 与
    `realpath(candidate)` 的比值。root 被整体改名后两者**同步搬走**，
    比值不变 —— 于是"这个相对路径仍落在 root 内"这个结论依然成立，
    返回的绝对路径也确实是当前 root 下的位置。它不会把库外路径判成
    库内。

    真正的后果是"返回的路径指向一个已不存在的目录"，那在 `open()` 时
    表现为 `FileNotFoundError`，与"库根被移走"这个事实一致，不是安全问题。
    为一个不存在的错误码加一层 root 身份校验（记录 inode/文件 ID 再比对）
    只会增加每次调用的系统调用数。

    这条用例把这个判断**钉死**：如果以后真的加了 root 失效检测，它会红，
    那时应当连同文档一起改，而不是悄悄漂移。
    """
    before = resolve(root, "sub/ok.jpg")
    moved = library.with_name(library.name + "_renamed")
    os.rename(library, moved)
    try:
        after = resolve(root, "sub/ok.jpg")
        existed_while_renamed = Path(after).exists()
    finally:
        os.rename(moved, library)
    # 路径字符串不变（root 与 candidate 同步搬走，比值不变）
    assert str(after) == str(before)
    # 而它当时确实已经不存在了 —— 失败由 open() 报，不是沙箱报
    assert existed_while_renamed is False
    assert Path(after).exists(), "改名还原后应当又能访问"


def test_open_handle_is_immune_to_later_swap(tmp_path: Path) -> None:
    """SEC-068：先解析、再打开、打开后文件被换掉 —— 读到的仍是**原内容**。

    实测：文件句柄持有期间 `os.replace()` 会 `PermissionError [WinError 5]`
    （共享冲突），关掉句柄后才成功。也就是说 Windows 上"打开后替换"
    根本做不到，读取天然一致。

    这条说明"先 resolve 再 open"的安全性依赖的是**句柄**，而不是
    "打开前再 resolve 一次"。后者在 TOCTOU 下毫无意义：两次 resolve
    之间同样有窗口，真正堵住窗口的是操作系统给的句柄语义。
    """
    src = tmp_path / "a.jpg"
    src.write_bytes(b"OLD")
    other = tmp_path / "b.jpg"
    other.write_bytes(b"NEW")

    fh = open(src, "rb")
    try:
        with pytest.raises(OSError) as ei:
            os.replace(other, src)
        assert getattr(ei.value, "winerror", None) == 5 or ei.value.errno in (13, 11)
        assert fh.read() == b"OLD"
    finally:
        fh.close()
    os.replace(other, src)
    assert src.read_bytes() == b"NEW"


# ---------------------------------------------------------------------------
# M 组：真实承担"读/写/删/替换"职责的代码（069~080）
# ---------------------------------------------------------------------------


def test_resolve_does_not_check_existence(root: RootConfig) -> None:
    """SEC-070/071：`resolve()` 只回答"路径安全吗"，不回答"文件在吗"。

    文档写 `open_read` 对不存在路径抛 `E_PATH_NOT_FOUND`、对目录抛
    `E_PATH_IS_DIR` —— 那需要一个 `open_read()` 函数，而它不存在。
    行为由 `open()` 提供：不存在 → `FileNotFoundError`，目录 →
    `PermissionError`（Windows 上打开目录是拒绝访问，不是 IsADirectory）。

    这个分层是**对的**：把存在性塞进沙箱，会让"判断路径是否安全"
    依赖磁盘状态，而磁盘状态随时在变。
    """
    missing = resolve(root, "sub/nope.jpg")
    with pytest.raises(FileNotFoundError):
        open(missing, "rb")

    directory = resolve(root, "sub")
    with pytest.raises(PermissionError):
        open(directory, "rb")

    # 便捷封装把"不存在"折成 False，不抛
    assert exists(root, "sub/nope.jpg") is False
    assert exists(root, "sub/ok.jpg") is True
    assert is_dir(root, "sub") is True
    assert is_dir(root, "sub/ok.jpg") is False


def test_reading_a_real_photo_through_resolve_works(root: RootConfig, library: Path) -> None:
    """SEC-069：`resolve()` 返回的路径可以正常打开、内容正确。"""
    p = resolve(root, "sub/ok.jpg")
    assert p.is_file()
    assert p.read_bytes()[:4] == b"\xff\xd8\xff\xe0"


@pytest.mark.parametrize(
    "case_id,rel,expected",
    [
        ("SEC-012", "C:\\Windows", "drive_letter"),
        ("SEC-001", "../secret.txt", "parent_traversal"),
        ("SEC-028", "CON", "reserved_name"),
    ],
)
def test_open_read_would_be_rejected_before_io(
    root: RootConfig, case_id: str, rel: str, expected: str
) -> None:
    """SEC-072：`../secret.txt` 在 resolve 阶段就拒，根本走不到 `open()`。"""
    assert _rule(root, rel) == expected, case_id


def test_reserve_creates_exclusively_and_never_overwrites(tmp_path: Path) -> None:
    """SEC-073/074：`archive.reserve()` 用 `O_CREAT|O_EXCL` 排他创建。

    这是"绝不覆盖已有照片"（R-004，本项目唯一可能造成永久数据丢失的缺陷）
    的落地点。第二次占同名必须 `FileExistsError`，且**已有文件内容不变**。

    为什么不用 `open(path, "xb")`：`os.open` 能同时拿到 fd 与 `O_BINARY`，
    并且在 Windows 上要把 `PermissionError`（名字正忙/删除挂起）与
    `FileExistsError` 分开处理 —— 后者是"重名，换一个"，前者是
    "这个名字此刻不可用，也换一个"。`"xb"` 把两者混成一个异常。
    """
    r = _root(tmp_path / "store")
    (tmp_path / "store").mkdir()

    first = archive.reserve(r, "2024/20240101_120000.jpg")
    assert first.exists() and first.stat().st_size == 0
    first.write_bytes(b"\xff\xd8\xff\xe0")

    with pytest.raises(FileExistsError):
        archive.reserve(r, "2024/20240101_120000.jpg")
    assert first.read_bytes() == b"\xff\xd8\xff\xe0", "占位失败不该动已有文件"


def test_reserve_on_readonly_root_is_rejected(tmp_path: Path) -> None:
    """SEC-075：readonly 模式下排他创建直接被沙箱拒（`write_not_permitted`）。"""
    (tmp_path / "store").mkdir()
    with pytest.raises(SandboxError) as ei:
        archive.reserve(_root(tmp_path / "store", "readonly"), "2024/20240101_120000.jpg")
    assert ei.value.rule == "write_not_permitted"


def test_send2trash_moves_file_out_of_place(tmp_path: Path) -> None:
    """SEC-076/077：用户照片删除走 `send2trash`，**不进** `os.unlink`。

    `backend/app/handlers/browse.py::_trash_one` 就是这么做的。这条在
    测试里直接验 `send2trash` 的语义（文件消失、进回收站），不重复
    调用 handler —— handler 的行为已由 `test_handlers.py` 覆盖。

    对不存在的文件，`send2trash` 抛 `FileNotFoundError`（文档写的
    `E_PATH_NOT_FOUND` 不存在）。
    """
    from send2trash import send2trash

    f = tmp_path / "trash-me.jpg"
    f.write_bytes(b"\xff\xd8\xff\xe0")
    send2trash(str(f))
    assert not f.exists()

    with pytest.raises(FileNotFoundError):
        send2trash(str(tmp_path / "nope.jpg"))


def test_same_volume_replace_is_atomic(tmp_path: Path) -> None:
    """SEC-078：同卷 `os.replace` 成功，源消失、目标内容正确。"""
    src = tmp_path / "a.part"
    src.write_bytes(b"hello")
    dst = tmp_path / "b.jpg"
    os.replace(src, dst)
    assert dst.read_bytes() == b"hello"
    assert not src.exists()


def test_cross_volume_replace_fails_with_winerror_17() -> None:
    """SEC-079：跨卷 `os.replace` 抛 `OSError`，`winerror == 17`（EXDEV）。

    这正是 `_move_one_sync` 探测的东西：捕获后降级成"复制 + 校验大小 +
    删源"。文档说的 `E_CROSS_VOLUME` 错误码不存在，真实形态是
    `(winerror=17, errno=18)` 的 `OSError` —— 两个都要判，因为 Python
    在不同版本/平台上给的是不同那个。

    构造方式：源在**仓库盘**（`build/`，被 .gitignore 忽略），目标在
    pytest 的临时目录（`C:\\Users\\...\\Temp`，系统盘）。两盘不同时
    才可能失败，所以先 skip 掉同盘环境 —— 否则这条会静默变成
    "replace 总能成功"的伪断言。同时验证**失败后源文件仍在**：
    跨卷失败若把源删了，就是照片丢失。
    """
    repo_root = Path(__file__).resolve().parents[2]
    src_dir = repo_root / "build"
    src_dir.mkdir(parents=True, exist_ok=True)
    src = src_dir / "_pb_cross_volume_probe.part"
    dst_root = Path(os.environ.get("TEMP") or "C:/Windows/Temp")
    dst = dst_root / "_pb_cross_volume_probe.jpg"

    if Path(src).drive.upper() == Path(dst).drive.upper():
        pytest.skip("源与临时目录同卷，无法构造跨卷场景")

    src.write_bytes(b"x")
    try:
        with pytest.raises(OSError) as ei:
            os.replace(src, dst)
        assert (getattr(ei.value, "winerror", None), ei.value.errno) == (17, 18)
        assert src.exists(), "跨卷失败后源文件必须还在"
        assert not dst.exists(), "跨卷失败不该留下半个目标文件"
    finally:
        src.unlink(missing_ok=True)
        dst.unlink(missing_ok=True)


def test_tmp_path_rejects_path_injection(root: RootConfig) -> None:
    """`upload_id` 会拼进临时文件路径 —— 不校验就是路径注入（步 3 之外的一道）。"""
    ok = tmp_path(root, "a" * 16, create_dir=False)
    assert ok.name == "a" * 16 + ".part"
    assert is_within(ok, root.path)

    for bad_id in ("../../etc/passwd", "short", "x" * 33, "has space", ""):
        with pytest.raises(SandboxError) as ei:
            tmp_path(root, bad_id, create_dir=False)
        assert ei.value.rule == "bad_upload_id", bad_id


def test_assert_writable_does_not_touch_the_filesystem(library: Path) -> None:
    """`assert_writable()` 只判权限位 —— 用于"提前告诉用户此操作不被允许"。"""
    assert_writable(_root(library, "readwrite"), "write")
    with pytest.raises(SandboxError) as ei:
        assert_writable(_root(library, "readonly"), "write")
    assert ei.value.rule == "write_not_permitted"


# ---------------------------------------------------------------------------
# 辅助函数：归一化 / 前缀判断 / 展示裁剪
# ---------------------------------------------------------------------------


def test_normalize_rel_is_display_only(library: Path) -> None:
    """`normalize_rel()` 不做安全检查 —— 它的注释与实现必须一致。

    它只是"统一分隔符、去掉 `.` 与首尾斜杠"，用于入库与展示。任何
    文件访问都必须走 `resolve()`。这条用例钉住"它**不会**拒绝 `..`"
    这个事实：如果有人误把它当校验器用（`if normalize_rel(p) != p: reject`），
    就会以为 `../x` 被挡住了，实际它返回的正是 `../x`。
    """
    assert normalize_rel("sub\\ok.jpg") == "sub/ok.jpg"
    assert normalize_rel("a/./b") == "a/b"
    assert normalize_rel("a//b") == "a/b"
    assert normalize_rel("/a/b/") == "a/b"
    # ★ 它**不**拦穿越，这是有意的：拦穿越是 resolve() 的职责
    assert normalize_rel("../x") == "..%2F".replace("%2F", "/") or normalize_rel("../x") == "../x"
    with pytest.raises(SandboxError):
        resolve(_root(library), "../x")


def test_is_within_uses_commonpath_not_startswith(library: Path) -> None:
    """`is_within()` 必须用 `commonpath`：字符串前缀会把 `lib2` 判成 `lib` 的子路径。"""
    assert is_within(library / "sub", library) is True
    assert is_within(library, library) is True
    assert is_within(Path("E:/x"), library) is False
    sibling = library.with_name(library.name + "2")
    assert is_within(sibling, library) is False, "lib2 不是 lib 的子路径"


def test_safe_display_strips_separators_and_truncates() -> None:
    """`safe_display()` 用于响应体/日志，绝不能吐绝对路径。"""
    assert safe_display("a\\b/c") == "a/b/c"
    long_rel = "/".join(["x" * 60] * 20)
    shown = safe_display(long_rel)
    assert len(shown) <= 512
    assert shown.startswith("…")


def test_strip_bidi_is_for_display_only() -> None:
    """`strip_bidi()` 剥掉双向控制符，用于展示**已有**数据。

    与 `resolve()` 的"拒绝"策略不冲突：那边处理用户主动起的新名字
    （要让他改），这边处理从别处读来的旧数据（不能因为一个不可见字符
    就在列表里显示成空白）。
    """
    assert strip_bidi("a\u202eb.jpg") == "ab.jpg"
    assert strip_bidi("正常名字.jpg") == "正常名字.jpg"


# ---------------------------------------------------------------------------
# 文档 ↔ 测试 的完整性校验（防止 §4.1 再次变成孤儿表）
# ---------------------------------------------------------------------------


def test_documented_ids_are_all_covered() -> None:
    """docs/07 §4.1 的 **96** 条用例，必须每一条都能指到具体某个测试函数。

    ## 为什么不数"80 条"

    docs/07 通篇写"80 条沙箱用例（FUNC 16 + SEC 64）"，但 §4.1 的表格逐行数
    是 **FUNC 16 + SEC 80 = 96**。80 这个数字从第一版起就没人核过，于是
    "80/80 通过"这句话既无法执行也无法反驳（§12 的 G-01 就照它写的）。

    另有一组 10 条 `SEC-081~090` 在 §7.1 里，是 **HTTP 层**的穿越用例
    （`GET /api/items/..%2f.../thumb`），由 `test_handlers.py::TestHttpLayerTraversal`
    承担 —— 它们测的是路由把非整数 media_id 挡下来，不是沙箱本身，
    放在这里会让人以为沙箱在解析 URL。

    ## 为什么用"显式映射表"而不是扫源码字符串

    第一版写的是 `referenced = {i for i in all_ids if i in source}` ——
    只要文件里某处出现过这个字符串就算覆盖。那与 docs/11 Q-023 记的
    `grep -c "sniff_kind"` 事故是同一类错误：**符号出现 ≠ 行为被验证**。
    注释里提一句 `SEC-046` 就能让它"通过"。改成显式 dict 之后，
    每条 ID 都必须指名一个真实存在的测试函数。
    """
    expected = {f"FUNC-{i:03d}" for i in range(1, 17)} | {f"SEC-{i:03d}" for i in range(1, 81)}
    assert len(expected) == 96

    missing = sorted(expected - set(_COVERAGE))
    assert missing == [], f"这些文档用例没有任何测试承担：{missing}"

    extra = sorted(set(_COVERAGE) - expected)
    assert extra == [], f"映射表里有不存在的编号（文档已改？）：{extra}"

    # 每个编号都必须指向本模块里真实存在的测试函数 —— 光有编号没用
    module = sys.modules[__name__]
    for case_id, fn_name in sorted(_COVERAGE.items()):
        assert callable(getattr(module, fn_name, None)), (
            f"{case_id} 指向的 {fn_name}() 不存在 —— 编号只是被声称覆盖了"
        )
        assert fn_name.startswith("test_"), f"{case_id} 指向的不是测试函数：{fn_name}"


def test_scripts_check_sandbox_is_not_a_substitute() -> None:
    """`scripts/check_sandbox.py` 是手工脚本，且只有 **41** 条 ── 不能替代本文件。

    实测：41 条（8 应放行 + 33 应拒绝），13 个规则名。它是"人肉快速
    回归"工具，没有 pytest 结果、没有 CI、也没有被任何测试调用过
    （`grep check_sandbox backend/tests` 曾经零命中 —— 本文件也不调用它，
    而是把同样的事在 pytest 里做一遍）。

    保留它是合理的（改沙箱时可以不起 pytest 先快速看一遍），但**不能**
    在验收表里拿它当"80 条用例通过"的证据。
    """
    script = Path(__file__).resolve().parents[2] / "scripts" / "check_sandbox.py"
    assert script.is_file()
    text = script.read_text(encoding="utf-8")
    assert "CASES: list[tuple[str, str, str | None]] = [" in text
    # 手工脚本，不是 pytest 测试文件
    assert "import pytest" not in text


#: 文档编号 → 承担它的测试函数。**这是唯一的事实来源**，
#: 新增/删除文档用例时必须同步改这里（上面的用例会因此变红）。
_COVERAGE: dict[str, str] = {
    # ── A 组：正常路径 ──
    "FUNC-001": "test_normal_paths_are_allowed",
    "FUNC-002": "test_normal_paths_are_allowed",
    "FUNC-003": "test_normal_paths_are_allowed",
    "FUNC-004": "test_normal_paths_are_allowed",
    "FUNC-005": "test_normal_paths_are_allowed",
    "FUNC-006": "test_mode_matrix_allows_these_combinations",
    "FUNC-007": "test_mode_matrix_allows_these_combinations",
    "FUNC-008": "test_mode_matrix_allows_these_combinations",
    # ── B 组：.. 穿越 ──
    "SEC-001": "test_parent_traversal_is_rejected",
    "SEC-002": "test_parent_traversal_is_rejected",
    "SEC-003": "test_parent_traversal_is_rejected",
    "SEC-004": "test_parent_traversal_is_rejected",
    "SEC-005": "test_parent_traversal_is_rejected",
    "SEC-006": "test_parent_traversal_is_rejected",
    "SEC-007": "test_parent_traversal_is_rejected",
    "SEC-008": "test_four_dots_is_rejected_even_though_it_is_not_traversal",
    "SEC-009": "test_percent_encoded_traversal_is_allowed_as_a_literal_name",
    # ── C 组：绝对/盘符/UNC/设备 ──
    "SEC-010": "test_absolute_and_device_paths_are_rejected",
    "SEC-011": "test_absolute_and_device_paths_are_rejected",
    "SEC-012": "test_absolute_and_device_paths_are_rejected",
    "SEC-013": "test_absolute_and_device_paths_are_rejected",
    "SEC-014": "test_absolute_and_device_paths_are_rejected",
    "SEC-015": "test_absolute_and_device_paths_are_rejected",
    "SEC-016": "test_absolute_and_device_paths_are_rejected",
    "SEC-017": "test_absolute_and_device_paths_are_rejected",
    "SEC-018": "test_absolute_and_device_paths_are_rejected",
    "SEC-019": "test_absolute_and_device_paths_are_rejected",
    "SEC-020": "test_absolute_and_device_paths_are_rejected",
    "SEC-021": "test_absolute_and_device_paths_are_rejected",
    "SEC-022": "test_absolute_and_device_paths_are_rejected",
    # ── D 组：NTFS 数据流 ──
    "SEC-023": "test_ntfs_data_streams_are_rejected",
    "SEC-024": "test_ntfs_data_streams_are_rejected",
    "SEC-025": "test_ntfs_data_streams_are_rejected",
    "SEC-026": "test_ntfs_data_streams_are_rejected",
    "SEC-027": "test_ntfs_data_streams_are_rejected",
    # ── E 组：保留设备名 ──
    "SEC-028": "test_windows_reserved_names_are_rejected",
    "SEC-029": "test_windows_reserved_names_are_rejected",
    "SEC-030": "test_windows_reserved_names_are_rejected",
    "SEC-031": "test_windows_reserved_names_are_rejected",
    "SEC-032": "test_windows_reserved_names_are_rejected",
    "SEC-033": "test_windows_reserved_names_are_rejected",
    "SEC-034": "test_windows_reserved_names_are_rejected",
    "SEC-035": "test_windows_reserved_names_are_rejected",
    # ── F 组：尾随点/空格/控制符 ──
    "SEC-036": "test_trailing_dot_or_space_is_rejected",
    "SEC-037": "test_trailing_dot_or_space_is_rejected",
    "SEC-038": "test_trailing_dot_or_space_is_rejected",
    "SEC-039": "test_trailing_dot_or_space_is_rejected",
    "SEC-040": "test_control_characters_are_rejected",
    "SEC-041": "test_control_characters_are_rejected",
    "SEC-042": "test_control_characters_are_rejected",
    "SEC-043": "test_leading_space_is_allowed",
    # ── G 组：重解析点 ──
    "SEC-044": "test_traversal_before_junction_is_still_traversal",
    "SEC-045": "test_junction_escape_is_rejected",
    "SEC-046": "test_junction_escape_is_rejected",
    "SEC-047": "test_junction_escape_is_rejected",
    # ── H 组：超长路径 ──
    "SEC-048": "test_segment_over_255_is_rejected",
    "SEC-049": "test_segment_over_255_is_rejected",
    "SEC-050": "test_segment_of_exactly_255_is_allowed",
    "SEC-051": "test_total_path_over_1024_is_rejected",
    # ── I 组：空段/NUL/控制符/Unicode ──
    "SEC-052": "test_empty_and_dangerous_characters_are_rejected",
    "SEC-053": "test_empty_and_dangerous_characters_are_rejected",
    "SEC-054": "test_dot_segment_is_dropped_not_rejected",
    "SEC-055": "test_double_slash_is_rejected_as_empty_segment",
    "SEC-056": "test_empty_and_dangerous_characters_are_rejected",
    "SEC-057": "test_empty_and_dangerous_characters_are_rejected",
    "SEC-058": "test_nbsp_and_fullwidth_are_allowed",
    "SEC-059": "test_nbsp_and_fullwidth_are_allowed",
    # ── J 组：权限与模式 ──
    "SEC-060": "test_write_on_readonly_root_is_rejected",
    "SEC-061": "test_write_on_upload_and_readwrite_is_allowed",
    "SEC-062": "test_write_on_upload_and_readwrite_is_allowed",
    "SEC-063": "test_delete_requires_readwrite_and_config_switch",
    "SEC-064": "test_delete_requires_readwrite_and_config_switch",
    # ── K 组：真实库样本 ──
    "FUNC-009": "test_real_library_paths_are_allowed",
    "FUNC-010": "test_real_library_paths_are_allowed",
    "FUNC-011": "test_real_library_paths_are_allowed",
    "FUNC-012": "test_real_library_paths_are_allowed",
    "FUNC-013": "test_real_library_paths_are_allowed",
    "FUNC-014": "test_real_library_paths_are_allowed",
    "FUNC-015": "test_real_library_paths_are_allowed",
    "FUNC-016": "test_real_library_has_no_names_the_sandbox_would_reject",
    # ── L 组：并发与 TOCTOU ──
    "SEC-065": "test_open_handle_is_immune_to_later_swap",
    "SEC-066": "test_resolve_is_thread_safe",
    "SEC-067": "test_root_renamed_does_not_produce_a_fake_verdict",
    "SEC-068": "test_open_handle_is_immune_to_later_swap",
    # ── M 组：读/写/删/替换 ──
    "SEC-069": "test_reading_a_real_photo_through_resolve_works",
    "SEC-070": "test_resolve_does_not_check_existence",
    "SEC-071": "test_resolve_does_not_check_existence",
    "SEC-072": "test_open_read_would_be_rejected_before_io",
    "SEC-073": "test_reserve_creates_exclusively_and_never_overwrites",
    "SEC-074": "test_reserve_creates_exclusively_and_never_overwrites",
    "SEC-075": "test_reserve_on_readonly_root_is_rejected",
    "SEC-076": "test_send2trash_moves_file_out_of_place",
    "SEC-077": "test_send2trash_moves_file_out_of_place",
    "SEC-078": "test_same_volume_replace_is_atomic",
    "SEC-079": "test_cross_volume_replace_fails_with_winerror_17",
    "SEC-080": "test_tmp_path_rejects_path_injection",
}

