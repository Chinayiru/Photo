"""前端 API 契约检查 —— 防止前后端路径漂移。

为什么需要这个脚本
------------------
前后端是两套独立代码，只靠"约定"保持一致。漂移的表现极其难查：

* 前端拼错一个路径（`/api/items/delete` 写成 `/api/item/delete`）
  → 后端返回 **404 错误体**，前端 `res.json()` 成功但拿不到
  期望的字段，界面上表现为"点了删除没反应"，控制台一片安静。
* 前端调了一个后端根本没实现的接口
  → 同上，而且写前端的人会以为是后端没部署新版。
* 后端改了路径参数名（`{id}` → `{media_id}`）
  → 这个真的发生过，导致所有按 id 取图的接口全 400。

所以这里做静态检查：从 `frontend/src/` 里把形如 `/api/...` 的
字面量抠出来，与后端 `build_router()` 实际注册的路由逐条比对。

用法
----
    .venv/Scripts/python.exe scripts/check_frontend_api.py

退出码 0 = 全部对得上；1 = 有漂移（输出每一条不匹配及其所在文件）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from backend.app.routers import build_router       # noqa: E402

FRONTEND_SRC = REPO / "frontend" / "src"

#: 匹配模板字符串与普通字符串里的 API 路径。
#: 允许内部出现 `${...}`（会被归一化成 `{}`）与查询串（会被截掉）。
_PATH_RE = re.compile(r"""["'`](/api/[^"'`\s]*)["'`]""")

#: 一期前端**故意**不调的接口。每一条都要写明理由，否则这份清单会
#: 变成"漂移的垃圾桶" —— 谁都能往里加一行来让检查通过。
INTENTIONALLY_UNUSED: dict[str, str] = {
    "/api/folders":
        "一期手机端只做时间线，不做目录浏览；接口已实现并测试，"
        "留给二期或桌面面板",
    "/api/clients/me":
        "配对后不需要再查自己是谁；保留给二期隧道切换时校验 device_id",
    "/api/devices":
        "设备列表只在电脑端面板用，手机不需要看",
    "/api/devices/{client_id}":
        "撤销设备只能从电脑端做 —— 手机能撤销自己等于把令牌有效期变成摆设",
    "/panel/pair-code":
        "面板接口只接受来自本机的请求（中间件按 client_ip 判定），"
        "手机端连上去会被 404；配对码由电脑端托盘或面板生成",
    "/panel/devices":
        "同上，面板只在电脑本机浏览器打开",
    "/panel/audit":
        "同上，审计日志是给电脑前的用户看的，手机上看不到",
}


def _normalize(path: str) -> str:
    """把前端写的一条路径归一化成可与路由模式比对的形式。

    * 截掉查询串（`?size=320&v=xxx` 对匹配路由毫无影响）
    * 把 `${...}` 与 `${encodeURIComponent(...)}` 之类替换成 `{}`
    """
    path = path.split("?", 1)[0]
    path = re.sub(r"\$\{[^}]*\}", "{}", path)
    return path.rstrip("/") or "/"


def _route_patterns() -> set[str]:
    """后端实际注册的路由模式，已归一化为 `{param}` 形式。"""
    out: set[str] = set()
    for route in build_router().routes:
        if route.method == "HEAD":       # HEAD 是 GET 的自动派生，前端不会单独调
            continue
        out.add(_normalize(route.pattern))
    return out


def _frontend_paths() -> dict[str, list[str]]:
    """前端源码里出现的所有 API 路径 → 出现在哪些文件。"""
    found: dict[str, list[str]] = {}
    if not FRONTEND_SRC.is_dir():
        return found
    for f in sorted(FRONTEND_SRC.rglob("*")):
        if f.suffix not in (".ts", ".vue", ".js"):
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            continue
        for m in _PATH_RE.finditer(text):
            raw = m.group(1)
            # 带 ${...} 的路径按"前缀"匹配：前端会拼 `/api/items/${id}/thumb`，
            # 归一化后是 `/api/items/{}/thumb`，与路由 `/api/items/{media_id}/thumb`
            # 的形状一致，只是参数名不同 —— 参数名不同是允许的。
            norm = _normalize(raw)
            found.setdefault(norm, []).append(str(f.relative_to(REPO)))
    return found


def _shape(pattern: str) -> str:
    """把路径里的参数名抹掉，只留形状：`/api/items/{x}/thumb` → `/api/items/*/thumb`。"""
    return re.sub(r"\{[^}]*\}", "*", pattern)


def main() -> int:
    routes = _route_patterns()
    route_shapes = {_shape(p): p for p in routes}
    frontend = _frontend_paths()

    problems: list[str] = []

    for norm, files in sorted(frontend.items()):
        shape = _shape(norm)
        if shape in route_shapes:
            continue
        # 未匹配：可能是"故意不用"的，也可能真的是漂移
        if norm in INTENTIONALLY_UNUSED:
            continue
        where = ", ".join(sorted(set(files)))
        problems.append(f"前端调用了不存在的接口：{norm}\n    出现在：{where}")

    called_shapes = {_shape(n) for n in frontend}
    unused = sorted(
        p for p in routes
        if _shape(p) not in called_shapes and p not in INTENTIONALLY_UNUSED
    )

    print(f"后端路由 {len(routes)} 条，前端引用 {len(frontend)} 条\n")

    if problems:
        print("✗ 发现前后端路径漂移：\n")
        for p in problems:
            print(f"  {p}\n")
        return 1

    print("✓ 前端引用的每一个接口都在后端注册表里")
    print("✓ 路径参数名不一致不影响匹配（只比形状）\n")

    if unused:
        print(f"以下 {len(unused)} 条后端接口前端一期未使用（不报错，仅提示）：")
        for p in unused:
            print(f"  · {p}")
        print("\n  若这是有意为之，请加进 INTENTIONALLY_UNUSED 并写明理由。")

    if INTENTIONALLY_UNUSED:
        print(f"\n已知有意不用的 {len(INTENTIONALLY_UNUSED)} 条：")
        for p, why in sorted(INTENTIONALLY_UNUSED.items()):
            print(f"  · {p}\n      {why}")

    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    raise SystemExit(main())
