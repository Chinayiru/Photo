"""静态资源服务：把 `frontend/dist` 挂到根路径。

只服务**已构建好的**前端产物。开发期用 `vite dev` 起在另一个端口，
不经过这里。

为什么需要它：手机浏览器直接访问 `http://192.0.2.10:8787/` 就该
看到界面。让用户先记一个别的端口、或者手动输入一长串路径，是没必要的
摩擦 —— 局域网方案的全部卖点就是"打开就能用"。

安全上只做三件事，但每件都必须做对：

1. **路径必须落在 dist 之内**。用 `os.path.realpath` 归一后再比对
   `commonpath`，而不是字符串 `startswith`（后者会把
   `dist-evil/` 判成 `dist/` 的子路径）。这里不能复用 `sandbox.py`：
   那个模块的语义是"用户配置的媒体库根"，与程序自带的静态目录无关，
   硬套会把 dist 塞进 roots 配置里。
2. **不服务任何会被浏览器当脚本执行的东西之外的类型**。dist 里的
   `.js`/`.css`/`.html` 是**我们自己构建的**，与媒体库里的用户文件
   不同源，所以这里不需要 `transports.base.DANGEROUS_EXTS` 那套限制。
   但 `.map` 不服务（源码映射会暴露完整源码）。
3. **`index.html` 不缓存**。它引用的是带哈希的资源文件名，一旦被
   缓存住，用户升级程序后浏览器会拿旧 index 去请求已删除的旧
   `assets/index-abc123.js`，表现是白屏且刷新无效。
   带哈希的资源则相反：`immutable` 缓存一年。
"""
from __future__ import annotations

import logging
import mimetypes
import os
from pathlib import Path
from shared.security_headers import headers_for
from .core import Ctx, Reply, empty_reply, file_reply
from .errors import E1004, AppError
from .handlers.common import etag_matches, iter_file

log = logging.getLogger(__name__)

__all__ = ["StaticFiles", "DEFAULT_DIST", "EXTRA_TYPES"]

#: 构建产物的默认位置。
DEFAULT_DIST: Path = Path(__file__).resolve().parents[2] / "frontend" / "dist"

#: mimetypes 依赖注册表，不同 Windows 机器结果不一致（.webmanifest、
#: .woff2 在干净系统上认不出来）。这几个必须写死，否则 PWA 清单
#: 会以 application/octet-stream 发出，浏览器拒绝解析。
EXTRA_TYPES: dict[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".webmanifest": "application/manifest+json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".txt": "text/plain; charset=utf-8",
}

#: 带内容哈希的资源，可以永久缓存。
_HASHED_MARKERS = ("assets/",)

#: 永不服务：源码映射会暴露完整源码与注释。
_BLOCKED_SUFFIXES = (".map",)


def _content_type(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in EXTRA_TYPES:
        return EXTRA_TYPES[ext]
    guess, _ = mimetypes.guess_type(str(path))
    return guess or "application/octet-stream"


def _apply_cache(reply: Reply, rel: str, *, is_index: bool) -> None:
    """按文件类型设缓存策略。

    分三档，每一档都有明确理由：
      * 外壳 `index.html` → `no-cache`。它引用带哈希的资源名，一旦被
        缓存住，升级后浏览器会拿旧 index 去请求已删除的旧 JS，白屏。
      * `assets/*` → 一年 immutable。Vite 把内容哈希写进文件名，
        内容变了文件名就变了，不存在"看到旧代码"的可能。
      * 其余（图标、manifest）→ 一小时。它们没有哈希，但也不该
        每次启动都重新下载。
    """
    if is_index:
        reply.set("Cache-Control", "no-cache")
    elif any(rel.startswith(m) for m in _HASHED_MARKERS):
        reply.set_cache(31536000, immutable=True)
    else:
        reply.set_cache(3600)


def _apply_security(reply: Reply, path: Path) -> Reply:
    """贴上安全响应头，并把同一个 `reply` 返回（便于 `return _apply_security(…)`）。

    头本身定义在 `shared/security_headers.py`，云端伺服
    （`cloud/relay/pages.py`）发的是同一套 —— 因为这是**同一个页面**的
    两条路径。修之前这里一个头都没有（连 `nosniff` 都没有），而手机在
    家里 Wi-Fi 下走的正是这条路径。
    """
    for key, value in headers_for(_content_type(path)):
        reply.set(key, value)
    return reply


class StaticFiles:
    """一个可调用对象，交给 `Router.set_fallback()`。

    用类而不是闭包：`repr()` 在日志里能看出这是什么，排障时
    比 `<function <locals>.<lambda> at 0x...>` 有用得多。
    """

    __slots__ = ("root", "index", "enabled")

    def __init__(self, dist: str | Path | None = None) -> None:
        self.root: Path | None = None
        self.index: Path | None = None
        self.enabled = False

        candidate = Path(dist) if dist is not None else DEFAULT_DIST
        index = candidate / "index.html"
        if index.is_file():
            # 用 realpath 而不是 resolve()：resolve 在路径不存在时会抛异常，
            # 而这里只要"目录真实位置"，不存在的情况已经由上面的 is_file 排除了。
            self.root = Path(os.path.realpath(str(candidate)))
            self.index = self.root / "index.html"
            self.enabled = True
        else:
            log.info("未找到前端构建产物（%s），根路径不提供界面", candidate)

    # ------------------------------------------------------------------

    def _resolve(self, rel: str) -> Path | None:
        """把 URL 路径解析成 dist 内的真实文件；越界或不存在返回 None。"""
        assert self.root is not None
        rel = rel.lstrip("/")
        if not rel:
            return self.index
        # URL 里可能带 %2e%2e 的二次编码形式，但 Starlette 给的是已解码
        # 路径；这里再挡一次显式的 .. 段，避免依赖上游的解码行为。
        if ".." in rel.replace("\\", "/").split("/"):
            return None
        cand = Path(os.path.realpath(str(self.root / rel)))
        try:
            if os.path.commonpath([str(self.root), str(cand)]) != str(self.root):
                return None
        except ValueError:
            # 不同盘符：commonpath 会抛 ValueError。这是明确的越界。
            return None
        return cand

    async def __call__(self, ctx: Ctx) -> Reply:
        if not self.enabled or self.index is None:
            raise AppError(E1004)

        # 只服务 GET/HEAD：静态资源不存在"提交"语义，
        # 回 405 会让调试的人以为路径写错了。
        if ctx.method.upper() not in ("GET", "HEAD"):
            raise AppError(E1004)

        target = self._resolve(ctx.path)
        if target is None or target.suffix.lower() in _BLOCKED_SUFFIXES:
            raise AppError(E1004)

        if target.is_dir():
            target = target / "index.html"

        if not target.is_file():
            # 单页应用回退：任何未知路径都给 index.html，由前端自己
            # 决定显示什么。不这么做的话，用户在查看器里刷新页面
            # 会得到 404 —— 而这个 404 来自后端，前端无法接管。
            target = self.index

        return self._serve(ctx, target)

    def _serve(self, ctx: Ctx, path: Path) -> Reply:
        assert self.index is not None
        try:
            st = path.stat()
        except OSError:
            raise AppError(E1004) from None

        rel = str(path.relative_to(self.root)).replace("\\", "/")
        # ⚠️ 传**裸值**：Reply.set_etag 会自动加引号。自己再加一层
        # 会得到 `""1234-13""`，浏览器回传时匹配不上，304 永远不生效
        # —— 表现是每次刷新都重下整个 JS/CSS，而缓存头看起来完全正常。
        etag = f"{int(st.st_mtime)}-{st.st_size}"

        # 条件请求：外壳虽然设了 no-cache，但仍然允许协商 —— 没变就回
        # 304 不带体，比回一个几十 KB 的 HTML 便宜得多（手机流量是真金白银）。
        if etag_matches(ctx.header("if-none-match"), etag):
            r = empty_reply(304)
            r.set_etag(etag)
            _apply_cache(r, rel, is_index=path == self.index)
            return _apply_security(r, path)

        if path == self.index:
            # 外壳不缓存（见模块文档第 3 点）。
            reply = file_reply(
                iter_file(path),
                _content_type(path),
                st.st_size,
            )
            _apply_cache(reply, rel, is_index=True)
            reply.set_etag(etag)
            return _apply_security(reply, path)

        reply = file_reply(iter_file(path), _content_type(path), st.st_size)
        reply.set_etag(etag)
        _apply_cache(reply, rel, is_index=False)
        return _apply_security(reply, path)

    def __repr__(self) -> str:
        return f"StaticFiles(root={self.root!r}, enabled={self.enabled})"
