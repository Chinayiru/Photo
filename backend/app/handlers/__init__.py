"""路由处理函数。

每个模块导出一组 `async def f(ctx: Ctx) -> Reply`，全部只依赖 `ctx`，
不 import starlette/fastapi —— 二期云端隧道复用同一批函数
（设计原则 DP-4「两条通路，一套 API」）。

## 为什么不用 FastAPI 的依赖注入

FastAPI 的 `Depends` 把"取数据库"和"HTTP 请求"绑定在一起，
而隧道通路上根本没有 `Request` 对象。用 `Ctx` 作为唯一入参，
业务函数对两条通路都成立。
"""
from __future__ import annotations

__all__ = ["browse", "common", "system"]
