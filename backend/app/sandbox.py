"""路径沙箱 —— 所有文件访问的唯一入口（安全核心）。

权威设计：docs/03-详细设计说明书.md §4.5。

**硬约束**：除本模块外，任何模块都禁止直接拼路径访问库文件。
自检：`grep -rn "open(" backend/app/ | grep -v sandbox.py` 的结果必须能逐条解释。

为什么这层不能省：库根 `D:\\Photos` 是**可写**的（用户已授权上传/删除）。
一旦手机端能构造 `../../Windows/System32/x.dll`，程序就从"相册工具"变成
"远程任意文件读写器"。七步校验是这个系统里唯一挡住它的东西。

七步（顺序不可调换，每一步都对应一类真实绕过手法）：
  1 形态拒绝      —— 绝对路径/盘符/UNC/设备路径
  2 段级拒绝      —— `..`、空段、NTFS 数据流 `:`、Windows 保留名、结尾空格/点
  3 字符与长度    —— 非法字符 `<>:"|?*` 与控制字符、单段 >255、总长 >1024
  4 归一化拼装    —— realpath(root) 后拼接（root 自身可能是 junction）
  5 逐段 reparse  —— **必须逐段**：中间目录是 junction 就足以越界
  6 前缀断言      —— commonpath 而非 startswith（否则 /root2 会被判为 /root 子路径）
  7 权限位        —— 与 root.mode 及 auth.allow_delete 一致
"""
from __future__ import annotations

import os
import re
from pathlib import Path, PurePosixPath
from typing import IO, Final, Literal

from .config import Config, RootConfig, TMP_DIRNAME, get as get_config
from .errors import E1004, E1009, E1015, SandboxError
from .fsopen import HandleMismatch
from .fsopen import assert_identity as _fs_assert_identity
from .fsopen import assert_same_path as _fs_assert_same_path
from .fsopen import identity as _identity
from .fsopen import norm_final as _norm_final
from .fsopen import open_verified as _fs_open_verified
from .fsopen import strip_win_prefix as _strip_win_prefix

Need = Literal["read", "write", "modify", "delete"]

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

#: Windows 文件名非法字符。':' 单独在步 2 处理（NTFS 数据流语义更具体）。
ILLEGAL_CHARS: Final[frozenset[str]] = frozenset('<>"|?*') | frozenset(chr(c) for c in range(0x20))

#: 双向文本控制字符 —— "显示出来的顺序与真实字符顺序不同"的那一批。
#:
#: 为什么要单独列：这些字符**在 Windows 上是合法文件名**，`open()` 能建、
#: 资源管理器能显示，所以步 2/步 3 的字符校验全都会放行。但它们的唯一
#: 作用就是翻转渲染方向：
#:
#:   U+202E RIGHT-TO-LEFT OVERRIDE 之后的字符按从右到左渲染
#:
#: 于是 `x\u202Eegap.png` 显示出来的后缀与真实后缀完全不同 —— 经典的
#: "RLO 伪装扩展名"手法，能让 `evil\u202Egnp.exe` 看着像 png。
#:
#: 对 PhotoBridge 这不止是"看着别扭"：照片库的全部价值就是让用户按
#: 文件名/缩略图找到自己的照片。一个显示成别的名字的文件放进库里，
#: 用户点开时看到的东西与他以为的不是同一张 —— 这正是 docs/06 §10
#: 规则 6（魔术字节校验）想防的那类欺骗，只是发生在名字层而不是内容层。
#:
#: 实测（审计报告 F10）：`POST /api/items/rename {"new_name":
#: "x\u202Eegap.png"}` 返回 200 并真的在磁盘上建出了文件；而同一接口
#: 对 `CON.png` 和含 NUL 的名字都正确返回 400 E1009 —— 校验链是通的，
#: 只是没覆盖这一档。
#:
#: 范围取 U+202A–U+202E（LRE/RLE/PDF/LRO/RLO）、U+2066–U+2069
#: （LRI/RLI/FSI/PDI），外加 U+200E/U+200F（LRM/RLM，不可见单向标记）。
#: **不含** U+200B–U+200D（零宽空格/连接符）—— 那些在中文与阿拉伯文
#: 排版里是正常的组合字符，禁掉会误伤真实文件名；也不含 U+FEFF（BOM），
#: 它更可能是复制粘贴的残留，剥掉比拒绝友好（见 `strip_bidi`）。
BIDI_CONTROLS: Final[frozenset[str]] = frozenset(
    [
        *(chr(c) for c in range(0x202A, 0x202F)),   # LRE RLE PDF LRO RLO
        *(chr(c) for c in range(0x2066, 0x206A)),   # LRI RLI FSI PDI
        "\u200e",                                    # LRM
        "\u200f",                                    # RLM
    ]
)

#: Windows 保留设备名。带扩展名的形式（`CON.txt`）同样保留。
RESERVED_NAMES: Final[frozenset[str]] = frozenset(
    {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
)

MAX_SEGMENT: Final[int] = 255
MAX_RELPATH: Final[int] = 1024
FILE_ATTRIBUTE_REPARSE_POINT: Final[int] = 0x400

#: upload_id 白名单：uuid4().hex 是 32 位十六进制；容忍 16–32 位的 [A-Za-z0-9_-]。
#: 这个校验是必需的：upload_id 会拼进临时文件路径，不校验就是路径注入。
_UPLOAD_ID_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_-]{16,32}\Z")

_DRIVE_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z]:")


class _Reject(Exception):
    """内部信号：携带拒绝的规则名。统一在 resolve() 里转成 SandboxError。"""

    __slots__ = ("rule", "detail")

    def __init__(self, rule: str, detail: str = "") -> None:
        super().__init__(f"{rule}: {detail}")
        self.rule = rule
        self.detail = detail


# --------------------------------------------------------------------------
# 七步校验
# --------------------------------------------------------------------------


def resolve(root: RootConfig, rel_path: str, need: Need = "read", *, cfg: Config | None = None) -> Path:
    """把相对路径解析为可安全访问的绝对路径。

    参数
    ----
    root : RootConfig
        媒体根配置。其 `path` 会被 realpath 后作为基准。
    rel_path : str
        相对根的路径，`/` 或 `\\` 分隔均可。**不接受绝对路径**。
    need : {"read","write","delete"}
        所需权限。与 root.mode 及 auth.allow_delete 共同决定是否放行。

    返回
    ----
    Path
        已确认位于根内、且不经过任何 reparse point 的绝对路径。
        **不保证该路径存在**（read 时由调用方处理 FileNotFoundError，
        write 时父目录可能尚需创建）。

    异常
    ----
    SandboxError
        任何一步拒绝。异常上的 `rule` 属性标明是哪一步，供审计日志使用。
    """
    try:
        norm, parts = _steps_1_to_3(rel_path)
        real_root = _real_root(root)
        _step_5_reparse(real_root, parts)
        p = _step_6_assert_inside(real_root, norm)
        _step_7_permission(root, need, cfg)
        return p
    except _Reject as r:
        # 审计 F01：停用根要表现得像"不存在"（404 E1004），不是"路径不合法"。
        # 400 会让客户端以为是自己的参数问题，而它其实什么都没做错。
        spec = E1004 if r.rule == "root_disabled" else E1009
        raise SandboxError(r.rule, r.detail or None, spec) from None


def resolve_dir(root: RootConfig, rel_path: str, *,
                cfg: Config | None = None) -> Path:
    """目录版 `resolve`：允许**空路径**（表示库根本身）。

    为什么需要它：`resolve(root, "")` / `resolve(root, ".")` 会被
    `_steps_1_to_3` 当成空路径拒掉（E1009 path_denied）—— 那个判断是对的，
    空路径在"访问某个文件"的语境下没有意义。但"列出根层有哪些子目录"
    是一个合法需求（「按文件夹」视图的第一屏）。

    这里不是绕过沙箱：清单仍然由 `_real_root()`（realpath 后的根）给出，
    权限判定仍走 `_step_7_permission`。区别只在于基准路径就是根，
    不存在"往根外走"这一步。
    """
    if rel_path.strip().strip("/\\.") == "":
        _step_7_permission(root, "read", cfg)
        return _real_root(root)
    # `need` 写死 "read"：这个函数只服务于"列某层子目录"，不提供写/删入口。
    # 留成参数会变成 check_lint 规则 10 要防的那件事 ——
    # "need 不是字面量，客户端可能把读声明成删"。
    return resolve(root, rel_path, "read", cfg=cfg)


# --------------------------------------------------------------------------
# 句柄化：把"校验过的路径"和"真正打开的文件"绑成同一个对象
# --------------------------------------------------------------------------
#
# ## 为什么单靠 resolve() 不够
#
# `resolve()` 只回答"此刻这个路径指向库内"。它返回之后、调用方 `open()`
# 之前有一个窗口：把路径上某一级目录换成 junction（目录联接）指向库外，
# 随后 `open()` 打开的就是库外的文件 —— 这就是 TOCTOU。
#
# 实测（build/_def_audit/probe2.py 的 12-toctou 组）：
#   resolve() 通过 → 删文件、删空目录 → mklink /J 指向库外 → 同一 Path
#   read_bytes() 得到 b'LOOT'。
#
# ## 修法：开完句柄之后再核一次
#
# 1. 重新 `resolve()` 一遍（换位后 `_step_5_reparse` 会直接拒）；
# 2. 比较 `fstat(fd)` 与 `lstat(path)` 的 (st_dev, st_ino)：句柄必须正是
#    校验时那个文件；
# 3. 任一条不成立就关句柄抛 SandboxError —— 不读一个字节。
#
# 只用 `O_NOFOLLOW` 是不够的（复核报告 V 已指出）：Windows 的 junction
# 不是 symlink，`O_NOFOLLOW` 拦不住目录联接。

def _handle_rule(exc: HandleMismatch) -> str:
    """把 `fsopen` 的拒绝理由翻译成本模块的错误码。"""
    return "handle_lost" if exc.reason == "lost" else "handle_mismatch"


def _assert_same_path(again: Path, path: Path) -> None:
    """复核第一步：重新解析出来的路径必须与刚才校验的完全一致。

    换位发生在 `resolve()` 与 `os.open()` 之间时，这一步要么已经在
    `resolve()` 里被 `_step_5_reparse` 拒掉，要么在这里因为落点变了而被拒。
    """
    try:
        _fs_assert_same_path(again, path)
    except HandleMismatch as exc:
        raise SandboxError(_handle_rule(exc), None, E1009) from None


def _assert_identity(fd: int, path: Path) -> None:
    """复核第二步：句柄指向的必须是 `lstat(path)` 看到的那个文件。

    `st_ino` 在 NTFS 上是文件索引，junction 换位后指向的是另一个文件，
    两者不等 → 拒绝，不读一个字节。
    """
    try:
        _fs_assert_identity(fd, path)
    except HandleMismatch as exc:
        raise SandboxError(_handle_rule(exc), None, E1009) from None


def open_read(root: RootConfig, rel_path: str, *,
              cfg: Config | None = None) -> IO[bytes]:
    """校验并打开一个库内文件用于读取，返回二进制只读句柄。

    与 `open(resolve(...))` 的区别只有一条：校验与打开是**绑定**的，
    路径在两步之间被换成 junction/硬链接时这里会抛 `handle_mismatch`
    而不是把库外内容读出去。

    异常：`SandboxError`（拒绝）与 `FileNotFoundError`（文件不存在，
    由调用方按缺失处理，保持既有 404 语义）。
    """
    p = resolve(root, rel_path, "read", cfg=cfg)
    try:
        fd = os.open(str(p), os.O_RDONLY | getattr(os, "O_BINARY", 0))
    except FileNotFoundError:
        # 文件不存在不是"沙箱拒绝"：调用方要按 404/缺失处理，保持既有语义。
        raise
    except OSError as exc:
        raise SandboxError("open_failed", type(exc).__name__, E1009) from None
    try:
        _assert_same_path(resolve(root, rel_path, "read", cfg=cfg), p)
        _assert_identity(fd, p)
    except BaseException:
        os.close(fd)
        raise
    return os.fdopen(fd, "rb", buffering=0)


def open_write(root: RootConfig, rel_path: str, *, cfg: Config | None = None,
               exclusive: bool = False) -> IO[bytes]:
    """校验并打开一个库内文件用于写入（默认截断，`exclusive=True` 时要求不存在）。

    写路径的 TOCTOU 一样存在：父目录被换成 junction 之后，`O_CREAT` 会在
    库外创建文件。这里开完句柄同样复核；`exclusive` 失败时顺手把刚建出来的
    那个文件删掉（只删身份匹配的，避免误删别人的文件）。
    """
    p = resolve(root, rel_path, "write", cfg=cfg)
    flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_BINARY", 0)
    flags |= os.O_EXCL if exclusive else os.O_TRUNC
    try:
        fd = os.open(str(p), flags, 0o644)
    except FileExistsError:
        raise
    except OSError as exc:
        raise SandboxError("open_failed", type(exc).__name__, E1009) from None
    try:
        _assert_same_path(resolve(root, rel_path, "write", cfg=cfg), p)
        _assert_identity(fd, p)
    except BaseException:
        # 不 unlink：删除用户可见对象必须走回收站（check_lint 规则 2）。
        # 代价是极小概率留下一个 0 字节文件；换位成功后那个位置本来就在
        # 攻击者可控的目录里，清理与否不改变数据安全结论。
        os.close(fd)
        raise
    return os.fdopen(fd, "wb", buffering=0)


def open_append(root: RootConfig, rel_path: str, *,
                cfg: Config | None = None) -> IO[bytes]:
    """校验并以追加方式打开库内文件（审计日志、上传临时文件用）。"""
    p = resolve(root, rel_path, "write", cfg=cfg)
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(str(p), flags, 0o644)
    except OSError as exc:
        raise SandboxError("open_failed", type(exc).__name__, E1009) from None
    try:
        _assert_same_path(resolve(root, rel_path, "write", cfg=cfg), p)
        _assert_identity(fd, p)
    except BaseException:
        os.close(fd)
        raise
    return os.fdopen(fd, "ab", buffering=0)


def open_verified(path: str | Path, mode: str = "rb", *,
                  buffering: int = 0) -> IO[bytes]:
    """打开一个**不在库根下**的文件，并复核它不是被换位来的。

    与 `open_read()` 的区别：不做 root 归属校验（调用方给的是缓存文件、
    上传临时文件，或已经过 `resolve()` 的库内路径），但仍然做
    "打开之后再核对身份"这一步 —— 也就是 `open_read()` 里消除 TOCTOU
    的那两条：重解析后路径一致、句柄 identity 与 `lstat(path)` 一致。

    为什么需要它：`exif`/`indexer` 拿到的只是一个路径，没有 root；
    上传临时文件在库内但不在任何 root 的白名单里。这些地方此前一律
    `open(path)`，与库内读取共享同一个 TOCTOU 窗口。

    实现在 `backend/app/fsopen.py`（那里不依赖 config，`video.py` 也能用），
    这里只把 `HandleMismatch` 翻译成 `SandboxError`。
    """
    try:
        return _fs_open_verified(path, mode, buffering=buffering)
    except HandleMismatch as exc:
        raise SandboxError(_handle_rule(exc), None, E1009) from None
    except FileNotFoundError:
        # 文件不存在不是"沙箱拒绝"：调用方按缺失处理（既有 404 语义）。
        raise
    except OSError as exc:
        raise SandboxError("open_failed", type(exc).__name__, E1009) from None


def _steps_1_to_3(rel_path: str) -> tuple[str, list[str]]:
    """步 1–3：形态、段级、字符与长度。返回 (归一化路径, 段列表)。"""
    # ---------- 步 1：形态 ----------
    if not isinstance(rel_path, str) or not rel_path or not rel_path.strip():
        raise _Reject("empty_path", repr(rel_path)[:80])

    # 设备路径（\\?\ 与 \\.\）必须**先于** UNC 判断：两者都以 \\ 开头
    if rel_path.startswith(("\\\\?\\", "\\\\.\\", "//?/", "//./")):
        raise _Reject("device_path")
    if rel_path.startswith(("\\\\", "//")):
        raise _Reject("unc_path")
    if rel_path.startswith(("/", "\\")):
        raise _Reject("leading_separator")
    if _DRIVE_RE.match(rel_path):
        raise _Reject("drive_letter")

    # 归一化分隔符：Windows 上 '\' 与 '/' 等价，必须一并处理，
    # 否则 "..\..\x" 会绕过只查 '/' 的段级检查。
    norm = rel_path.replace("\\", "/")

    # ---------- 步 2：段级 ----------
    raw_parts = norm.split("/")
    parts: list[str] = []
    for seg in raw_parts:
        if seg == "":
            # 允许末尾单个 '/'（"a/b/" → "a/b"），但中间的 "a//b" 是空段，拒绝
            if seg is raw_parts[-1]:
                continue
            raise _Reject("empty_segment")
        if seg == ".":
            continue                        # '.' 段无害，直接丢弃
        if seg == "..":
            # 不因为"归一化后结果仍在根内"就放行（如 sub/.. ）——
            # 规则要简单一致，且这类路径是攻击探测的典型特征。
            raise _Reject("parent_traversal")
        if ":" in seg:
            raise _Reject("ntfs_data_stream", seg)
        # 保留名判断用扩展名前的部分：CON.txt 同样保留
        if seg.split(".", 1)[0].upper() in RESERVED_NAMES:
            raise _Reject("reserved_name", seg)
        if seg.endswith((" ", ".")):
            # Windows 会静默去掉结尾空格/点 → 校验时的名字 ≠ 实际创建的名字，
            # 这个差异正是"校验通过但落到别处"的经典绕过。
            raise _Reject("trailing_space_or_dot", seg)
        parts.append(seg)

    if not parts:
        raise _Reject("empty_path")

    # ---------- 步 3：字符与长度 ----------
    for seg in parts:
        if len(seg) > MAX_SEGMENT:
            raise _Reject("segment_too_long", f"{len(seg)}>{MAX_SEGMENT}")
        bad = ILLEGAL_CHARS.intersection(seg)
        if bad:
            raise _Reject("illegal_chars", "".join(sorted(bad)))
        # 双向控制字符：**拒绝**而不是剥掉。剥掉会静默改掉用户给的名字，
        # 而拒绝能让调用方明确告诉用户"这个名字不能用" —— 用户此刻正
        # 在改名的输入框前，他改一个字比事后发现名字变了要好得多。
        bidi = BIDI_CONTROLS.intersection(seg)
        if bidi:
            raise _Reject("bidi_control", _describe_bidi(bidi))
    joined = "/".join(parts)
    if len(joined) > MAX_RELPATH:
        raise _Reject("path_too_long", str(len(joined)))

    return joined, parts


def _describe_bidi(chars: frozenset[str] | set[str]) -> str:
    """把命中的控制字符渲染成 `U+202E(RLO)` 形式，方便用户与日志定位。

    直接 `repr()` 出来是一串不可见字符，日志里看着像空字符串 ——
    运维和用户都不知道该改哪儿。
    """
    names = {
        0x202A: "LRE", 0x202B: "RLE", 0x202C: "PDF", 0x202D: "LRO", 0x202E: "RLO",
        0x2066: "LRI", 0x2067: "RLI", 0x2068: "FSI", 0x2069: "PDI",
        0x200E: "LRM", 0x200F: "RLM",
    }
    return "、".join(
        f"U+{ord(c):04X}({names.get(ord(c), '?')})" for c in sorted(chars)
    )


def strip_bidi(text: str) -> str:
    """剥掉双向控制字符（用于**只做展示**的字符串，不做安全校验）。

    与 `resolve()` 里的拒绝策略不冲突：那边处理的是"用户主动起的名字"，
    该不该用由用户决定；这边处理的是"从别处读来的已有数据"，比如
    扫描时拿到的文件名（用户可能是从别的系统拷进来的遗产文件）。
    对后者，剥掉比拒绝好 —— 拒绝会让整个目录索引失败，
    而那张照片本身完全正常，只是名字里夹了个不可见字符。
    """
    if not text:
        return text
    if BIDI_CONTROLS.isdisjoint(text):
        return text
    return "".join(c for c in text if c not in BIDI_CONTROLS)


def _real_root(root: RootConfig) -> Path:
    """步 4 的基准：realpath(root)。

    为什么不用 root.path 原值：root 自身可能就是 junction（例如用户把库
    放在一个指向别处的联接目录上）。不先解引用，后续的"逐段检查"会把
    第一段之外的所有路径都算在错误的基准上。
    """
    return Path(_strip_win_prefix(os.path.realpath(str(root.path))))


def _step_5_reparse(real_root: Path, parts: list[str]) -> None:
    """步 5：逐段检查 symlink / junction / reparse point。

    必须逐段：中间目录是 junction 就足以越界，只查最终路径会漏掉
    `junction_out/x.jpg` 这种形态。
    """
    cur = real_root
    for seg in parts:
        cur = cur / seg
        try:
            st = os.lstat(cur)              # ★ lstat：stat 会跟随链接，等于自废检查
        except FileNotFoundError:
            return                          # 后续段都不存在，没有可检查的对象
        except NotADirectoryError:
            # 前面某段是文件，后面还有段 → 路径不可能成立。
            # 交给调用方按 FileNotFoundError 处理更自然，但这里不能静默通过。
            raise _Reject("not_a_directory", str(cur)) from None
        except OSError as e:
            raise _Reject("stat_failed", type(e).__name__) from None
        if _is_reparse_or_link(cur, st):
            raise _Reject("reparse_point", seg)


def _is_reparse_or_link(path: Path, st: os.stat_result) -> bool:
    """Windows 上判断 symlink / junction / reparse point。

    为什么不能只看 `os.path.islink`：**junction（目录联接）不是 symlink**，
    islink() 返回 False，但它能把路径指向任意位置。这是 Windows 上
    最容易被忽略的越界方式，也是 docs/05 §10 指出的真实盲点。
    判断依据是 `st_file_attributes` 的 FILE_ATTRIBUTE_REPARSE_POINT (0x400) 位。
    """
    if os.path.islink(path):
        return True
    attrs = getattr(st, "st_file_attributes", 0)
    return bool(attrs & FILE_ATTRIBUTE_REPARSE_POINT)


def _step_6_assert_inside(real_root: Path, norm: str) -> Path:
    """步 6：断言结果位于根内。

    两个易错点：
      * 必须 normcase（Windows 大小写不敏感，`E:\\Photos` 与 `e:\\photos` 是同一路径）；
      * 必须 commonpath 而非 startswith —— 字符串前缀比较会把
        `D:\\photos2` 判为 `D:\\photos` 的子路径。
    """
    candidate = real_root / norm
    raw_final = os.path.realpath(str(candidate))
    if "$Extend" in raw_final or "$Deleted" in raw_final:
        # 删除挂起：文件正在被删除，Windows 把它解析到 NTFS 的暂存目录。
        # 这不是越界，而是"这个文件马上就要没了"。给出明确的规则名，
        # 免得它被当成路径穿越写进审计日志。
        raise _Reject("file_being_deleted", norm) from None
    p = Path(_strip_win_prefix(raw_final))
    a = _norm_final(str(real_root))
    b = _norm_final(str(p))
    if a == b:
        return p                          # 恰好等于根（如 rel_path 指向根本身）
    try:
        common = os.path.commonpath([a, b])
    except ValueError:
        # 不同盘符时 commonpath 抛 ValueError —— 本身就是越界的强证据
        raise _Reject("outside_root", "different drive") from None
    if common != a:
        raise _Reject("outside_root", norm)
    return p


def _step_7_permission(root: RootConfig, need: Need, cfg: Config | None) -> None:
    """步 7：权限位。

    三级语义（docs/03 §4.2）：
      readonly  → 只读浏览
      upload    → 只读 + 可新增（不能删/不能改已有文件）
      readwrite → 只读 + 新增 + 删除（进回收站）+ 移动 + 改名

    ★ 审计 F01：**第一步先看 `enabled`**。禁用的根不是"降级为只读"，
    而是"这块共享整个不存在" —— 读、写、移动、删除全都得拒。放在最前面
    而不是各分支里，是因为将来加 `Need` 值时会天然继承这条判断，不会
    因为漏写一个 elif 就把禁用根放进来。
    """
    if not getattr(root, "enabled", True):
        raise _Reject("root_disabled", root.id)
    mode = root.mode
    if need == "read":
        # 三种模式都能读，此处无需拒绝；保留分支是为了未来加 'hidden' 模式
        if mode not in ("readonly", "upload", "readwrite"):
            raise _Reject("read_not_permitted", mode)
    elif need == "write":
        if mode not in ("upload", "readwrite"):
            raise _Reject("write_not_permitted", mode)
    elif need == "modify":
        # 移动/改名：只改已有条目，不新增也不删除。`upload` 模式不许。
        # 故意**不**受 `auth.allow_delete` 影响 —— 用户关掉"删除"通常
        # 是怕照片丢，而移动/改名同样会动目录结构，所以它俩只认模式；
        # 这条也避免了"关掉删除 = 顺手把移动也锁死"的意外。
        if mode != "readwrite":
            raise _Reject("modify_not_permitted", f"mode={mode}")
    elif need == "delete":
        if mode != "readwrite":
            raise _Reject("delete_not_permitted", f"mode={mode}")
        c = cfg or get_config()
        if not c.auth.get("allow_delete", False):
            # 这条不是沙箱规则而是用户偏好，用 E1015 更准确；但保持
            # "所有路径校验从同一出口抛出"的一致性，由调用方决定映射。
            raise _Reject("delete_disabled_by_config")
    else:
        raise _Reject("unknown_need", str(need))


# --------------------------------------------------------------------------
# 便捷封装
# --------------------------------------------------------------------------


def exists(root: RootConfig, rel_path: str, cfg: Config | None = None) -> bool:
    """安全地判断路径是否存在。路径非法时返回 False 而不抛异常。"""
    try:
        return resolve(root, rel_path, "read", cfg=cfg).exists()
    except SandboxError:
        return False


def is_dir(root: RootConfig, rel_path: str, cfg: Config | None = None) -> bool:
    try:
        return resolve(root, rel_path, "read", cfg=cfg).is_dir()
    except SandboxError:
        return False


def normalize_rel(rel_path: str) -> str:
    """把路径归一成库内统一形式：`/` 分隔、无首尾斜杠、无 `.` 段。

    仅做**展示与入库**用的归一化，**不做安全检查**。
    任何真正访问文件系统的动作都必须走 resolve()。
    """
    p = PurePosixPath(rel_path.replace("\\", "/"))
    parts = [seg for seg in p.parts if seg not in ("", ".", "/")]
    return "/".join(parts)


def tmp_dir(root: RootConfig, *, create: bool = False) -> Path:
    """上传临时区目录：`<库根>/.photobridge_tmp`。

    **必须在库根下**（而不是 data/ 下）：`os.replace` 只有同卷才是原子的。
    库在 E 盘、data 在 H 盘时跨卷 replace 会抛 `OSError: [WinError 17]`，
    退化成"复制+删除"——中途断电就留下半个文件。
    这是 docs/03 §4.3.2 与 docs/05 §10.1 ① 的裁决。
    """
    real_root = _real_root(root)
    d = real_root / TMP_DIRNAME
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def tmp_path(root: RootConfig, upload_id: str, *, create_dir: bool = True) -> Path:
    """临时分片文件的绝对路径。

    upload_id 会拼进文件路径，因此**必须**白名单校验。
    没有这一步，`upload_id = "../../evil"` 就能把临时文件写到库外。
    """
    if not isinstance(upload_id, str) or not _UPLOAD_ID_RE.match(upload_id):
        raise SandboxError("bad_upload_id", f"upload_id 必须匹配 [A-Za-z0-9_-]{{16,32}}", E1009)
    return tmp_dir(root, create=create_dir) / f"{upload_id}.part"


def safe_display(rel_path: str) -> str:
    """把路径裁剪成适合写日志/审计的形式。

    审计日志需要**可定位**（记全路径），但响应体里绝不能出现绝对路径。
    本函数只处理相对路径，用于响应体与前端展示。
    """
    r = normalize_rel(rel_path)
    return r if len(r) <= 512 else "…" + r[-511:]


def is_within(child: Path, parent: Path) -> bool:
    """判断 child 是否在 parent 内（normcase + commonpath，语义同步 6）。"""
    a = os.path.normcase(str(Path(os.path.realpath(str(parent)))))
    b = os.path.normcase(str(Path(os.path.realpath(str(child)))))
    if a == b:
        return True
    try:
        return os.path.commonpath([a, b]) == a
    except ValueError:
        return False


# --------------------------------------------------------------------------
# 扫描排除目录 —— **写入口也必须认它**（审计 F14）
# --------------------------------------------------------------------------


def excluded_dir_segment(rel_path: str, root: RootConfig, *, last_is_file: bool = True) -> str:
    """返回 `rel_path` 里第一个命中 `root.exclude_dirs` 的目录段；没有则空串。

    为什么单独抽出来：排除名单原本只有索引器在用（`indexer._walk` 跳目录），
    而**写入口**（`POST /api/items/move` 的 `target_dir`、`POST /api/uploads/init`
    的 `target_dir`）完全不看它。后果不是"多扫了几个文件"，而是：

      1. 照片被移/传进 `data/`、`node_modules/`、`#recycle/`、`.photobridge_tmp/`
         —— 这些在 Windows 上都是用户真的会建的目录名，界面上完全正常可选；
      2. `index_one_file` 照收（它只看文件名），照片**先出现在时间线**；
      3. 下一次全量扫描 `_walk` 跳过那个目录，`_mark_missing` 把它标成 missing；
      4. 照片从界面消失，而 `move` / `rename` 对 `missing=1` 的行一律 `E1004`
         —— **API 侧没有任何回退路径**，只能手工去磁盘上搬回来。

    对非技术用户，这三步等于"照片被删了"。

    这也不是新规则，而是 `indexer.should_index` 的 docstring 里那条铁律的
    字面要求：「全量扫描与单文件增量是两条独立入口，必须给出完全一致的答案」。
    当前只对齐了**文件名**这一维，目录维漏了 —— 本函数补的正是目录维。

    参数 `last_is_file`：调用方给的是**文件**相对路径时（move/rename/
    `index_one_file`）传 True，最后一段是文件名、不参与目录判定（`data`
    作为文件夹被排除，`data.jpg` 作为文件名不受影响）；调用方给的是
    **目录**相对路径时（上传的 `subdir`）传 False，每一段都要判。

    ⚠️ 与 `indexer._is_excluded_dir` 逐条对齐：`name.lower() in root.exclude_dirs`，
    而 `root.exclude_dirs` 在 `config.py` 建根时已全部小写化。
    """
    if not rel_path:
        return ""
    norm = rel_path.replace("\\", "/").strip().strip("/")
    if not norm:
        return ""
    parts = norm.split("/")
    check = parts[:-1] if last_is_file else parts
    for seg in check:
        if seg.lower() in root.exclude_dirs:
            return seg
    return ""


def reject_excluded(rel_path: str, root: RootConfig, *, last_is_file: bool = True) -> None:
    """写入口守卫：目标路径落在扫描排除目录里就拒绝（审计 F14）。

    与 `resolve()` 的分工：`resolve()` 管"有没有越出库根"（安全边界），
    本函数管"这个位置在本库的语义里是不是一个能放照片的地方"（一致性边界）。
    两者都必须在写之前跑 —— 少了前者是安全漏洞，少了后者是数据消失。

    抛 `E1009 path_denied`（400）而不是 403：这不是权限问题，是参数落在
    一个不成立的位置上，与 `target_dir` 写 `..` 同类。文案点名那个目录段，
    让用户知道要换哪个名字，而不是只看到"路径不合法"。
    """
    seg = excluded_dir_segment(rel_path, root, last_is_file=last_is_file)
    if seg:
        raise SandboxError(
            "excluded_dir",
            f"目标目录「{seg}」不在照片库的索引范围内，"
            f"放进去的照片不会出现在时间线上",
            E1009,
        )


def assert_writable(root: RootConfig, need: Need = "write") -> None:
    """只校验权限位、不碰文件系统。用于"提前告知用户此操作不被允许"。"""
    try:
        _step_7_permission(root, need, None)
    except _Reject as r:
        if r.rule == "root_disabled":       # 同 resolve()：表现为"不存在"
            spec = E1004
        elif r.rule.endswith("by_config"):
            spec = E1015
        else:
            spec = E1009
        raise SandboxError(r.rule, r.detail or None, spec) from None


__all__ = [
    "FILE_ATTRIBUTE_REPARSE_POINT", "ILLEGAL_CHARS", "MAX_RELPATH", "MAX_SEGMENT",
    "Need", "RESERVED_NAMES",
    "assert_writable", "excluded_dir_segment", "exists", "is_dir", "is_within",
    "normalize_rel", "reject_excluded",
    "open_append", "open_read", "open_verified", "open_write",
    "resolve", "safe_display", "tmp_dir", "tmp_path",
]
