"""文件访问层的路径契约（F-03）。

三件事在这里被钉住，缺一条 F-03 就等于没收敛：

1. `backend/app/fsopen.py::open_verified` **只接受绝对路径**，相对路径直接抛
   `PathContractError` —— 不猜 CWD、不拼根目录、不静默转换。
2. 同一文件"上层转换后的绝对路径"与"直接就有的绝对路径"读到**同一份字节**，
   且转换只发生在 `sandbox.resolve(root, rel, op, cfg)` 一处。
3. 收敛没有削弱沙箱校验：`..` 越界仍然被 `resolve` 的七步检查拦住。

第 4 条用 AST 扫全仓调用点：任何新加的 `open_verified(...)` 都必须传
变量/表达式而不是字符串字面量，且文件集合与登记表一致 —— 这样"又悄悄
绕过契约"会当场红，而不是等到某次跨网络调试。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from backend.app.config import Config, RootConfig
from backend.app.fsopen import PathContractError, open_verified, require_absolute
from backend.app.sandbox import SandboxError, open_verified as sandbox_open_verified
from backend.app.sandbox import resolve

APP = Path(__file__).resolve().parents[1] / "app"

#: 允许调用 `open_verified` 的文件（含 `fsopen`/`sandbox` 自身的定义与包装）。
#: 新加一处就要在这里登记 —— 登记本身就是一次"这条路径从哪来"的复核。
CALL_SITES = {
    "exif.py",
    "fsopen.py",
    "handlers/common.py",
    "indexer.py",
    "sandbox.py",
    "uploads.py",
    "video.py",
}


def _root(path: Path, mode: str = "readwrite") -> RootConfig:
    return RootConfig(
        id="test",
        path=path,
        label="测试库",
        mode=mode,  # type: ignore[arg-type]
        enabled=True,
        recursive=True,
        include_exts=frozenset({"jpg", "png"}),
        exclude_dirs=(),
        exclude_files=(),
    )


def _cfg(tmp_path: Path) -> Config:
    return Config({"auth": {"allow_delete": True}}, path=tmp_path / "config.json",
                  data_dir=tmp_path / "data")


@pytest.fixture()
def library(tmp_path: Path) -> Path:
    lib = tmp_path / "lib"
    (lib / "sub").mkdir(parents=True)
    (lib / "sub" / "ok.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 64)
    return lib


def test_relative_path_is_rejected_with_actionable_message() -> None:
    """相对路径必须被拒绝，且错误要直接告诉调用方该走哪个函数。"""
    with pytest.raises(PathContractError) as exc:
        open_verified("sub/ok.jpg")
    msg = str(exc.value)
    assert "绝对路径" in msg
    assert "sandbox.resolve" in msg          # 给出唯一正确的下一步
    assert "sub/ok.jpg" in msg               # 带上原样输入，便于定位调用方


def test_bare_filename_is_rejected_too(tmp_path: Path) -> None:
    """裸文件名（连相对路径都不是）同样拒绝 —— 它最容易被 CWD 静默解析。"""
    (tmp_path / "x.jpg").write_bytes(b"\xff\xd8\xff\xe0")
    with pytest.raises(PathContractError):
        require_absolute("x.jpg")


def test_sandbox_wrapper_does_not_swallow_contract_error(library: Path) -> None:
    """`sandbox.open_verified` 是薄包装，不能把契约错误翻成沙箱错误。"""
    with pytest.raises(PathContractError):
        sandbox_open_verified("sub/ok.jpg")
    # 反过来确认它没有变成 SandboxError：两者语义完全不同，
    # 混淆会让调用方以为是"越界"，而实际是"传错了类型"。
    assert not issubclass(PathContractError, SandboxError)


def test_absolute_path_reads_bytes(library: Path) -> None:
    target = library / "sub" / "ok.jpg"
    with open_verified(str(target), "rb") as fh:
        assert fh.read(4) == b"\xff\xd8\xff\xe0"


def test_relative_and_absolute_are_equivalent(library: Path, tmp_path: Path) -> None:
    """同一文件：`resolve(rel)` 得到的绝对路径 ≡ 直接构造的绝对路径。"""
    cfg = _cfg(tmp_path)
    root = _root(library)

    via_resolve = resolve(root, "sub/ok.jpg", "read", cfg=cfg)
    direct = library / "sub" / "ok.jpg"

    assert via_resolve.is_absolute()
    assert via_resolve.resolve() == direct.resolve()

    a = b""
    with open_verified(via_resolve, "rb") as fh:
        a = fh.read()
    with open_verified(direct, "rb") as fh:
        b = fh.read()
    assert a == b and a != b""


def test_escape_still_blocked(library: Path, tmp_path: Path) -> None:
    """收敛没有削弱沙箱：`..` 越界照旧被拦。"""
    cfg = _cfg(tmp_path)
    root = _root(library)
    with pytest.raises(SandboxError):
        resolve(root, "../outside.jpg", "read", cfg=cfg)


def test_open_verified_call_sites_pass_no_string_literals() -> None:
    """AST 扫全仓：调用点的首参不得是字符串字面量。

    字面量既可能是相对路径（`"data/x.jpg"`），也可能是写死的绝对路径
    （换台机器就错），两种都不该出现在应用代码里。真正允许字面量的只有
    `fsopen.py` 自己的测试路径，而它不调用 `open_verified`。
    """
    bad: list[str] = []
    seen_files: set[str] = set()
    for path in sorted(APP.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if name != "open_verified" or not node.args:
                continue
            seen_files.add(str(path.relative_to(APP)).replace("\\", "/"))
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                bad.append(f"{path.relative_to(APP)}:{node.lineno} -> {first.value!r}")
    assert not bad, "open_verified 不接受路径字面量：\n" + "\n".join(bad)

    # 文件集合也要对得上：新增调用点必须来这里登记（复核路径来源）。
    assert seen_files <= CALL_SITES, f"未登记的调用文件：{sorted(seen_files - CALL_SITES)}"
