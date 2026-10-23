"""云端托管手机端页面（`frontend/dist`）。

## 为什么云端必须自己会发页面

P1 的目标是"人在外面也能看照片"（`docs/01` G-1）。而手机出门后手里只有
一个东西：域名。`https://域名/` 若回 404，那整条云端隧道**在产品上不可
达** —— 隧道通着、`/d/{id}/api/**` 全 200，但用户打不开一个能点的界面。
实测确认过这个缺口（`build/_probe_cloud_entry.py`）：

```
GET /              [404] ct='text/plain' 'Not Found'
GET /index.html    [404] ct='text/plain' 'Not Found'
GET /d/{id}/api/system/ping  [200]
```

一期不需要这段代码，因为页面由电脑端 `backend/app/static.py` 伺服，而手机
永远先访问电脑。二期有了云端，"页面从哪来"就分成两种情况：

  * **在家**：扫码进的是局域网地址，页面仍由电脑端发（一期路径，零改动）；
  * **在外面**：只有域名可用，页面必须由云端发。

两条路发的是**同一份构建产物**，所以这里刻意不复制 `static.py` 的全部
设计，只保留在本模块成立的那部分约束。

## 为什么不复用 `backend/app/static.py`

云端进程不 import 电脑端任何模块（`cloud/relay/*` 只依赖 `shared/`）。
理由与 `Dockerfile` 的构建上下文相同：`shared/` 是两端**共享的同一份物理
文件**，而 `backend/` 是电脑端独有的。让云端 import `backend.app.static`
会把"手机端页面"和"电脑端业务"绑成一次发布，而两者的升级节奏不同
（云端要能独立上线）。

## 三道必须做对的约束

1. **路径必须落在 dist 之内**。`os.path.realpath` 归一后用 `commonpath`
   比对，而不是字符串 `startswith` —— 后者会把 `dist-evil/` 判成 `dist/`
   的子路径。`%2e%2e` 由框架解码后再挡一次显式 `..` 段。
2. **`.map` 不服务**。源码映射会暴露完整源码与注释，而这几百 KB 对
   手机没有任何用处。
3. **`index.html` 不缓存，带哈希的资源永久缓存**。外壳被缓存住会在
   升级后白屏（浏览器拿旧 index 去请求已删除的旧 `assets/index-*.js`），
   这一条与一期踩过的坑完全相同。
"""
from __future__ import annotations

import logging
import mimetypes
import os
from pathlib import Path
from typing import Any

from starlette.responses import FileResponse, Response
from starlette.types import Scope

from .reply import text_reply

__all__ = ["DEFAULT_DIST", "PAGE_CACHE", "ASSET_CACHE", "StaticPages"]

log = logging.getLogger(__name__)

#: 构建产物的默认位置。`cloud/relay/pages.py` → 仓库根 → `frontend/dist`。
DEFAULT_DIST: Path = Path(__file__).resolve().parents[2] / "frontend" / "dist"

#: 外壳的缓存策略。必须允许协商（`no-cache` 不是"不缓存"），否则每次
#: 打开都重下几十 KB HTML；但绝不能让浏览器**直接用**旧副本。
PAGE_CACHE = "no-cache"

#: 带内容哈希的资源。内容变了文件名就变了，不存在"看到旧代码"。
ASSET_CACHE = "public, max-age=31536000, immutable"

#: 其余（图标、manifest）：一小时。它们没有哈希，但也不该每次重下。
OTHER_CACHE = "public, max-age=3600"

#: 永不服务：源码映射会暴露完整源码与注释。
_BLOCKED_SUFFIXES = (".map",)

#: mimetypes 依赖注册表，不同机器结果不一致。这几个必须写死，否则
#: `index.html` 会以 `application/octet-stream` 发出，浏览器直接下载而不是
#: 渲染 —— 表现是"打开域名得到一个下载框"。与
#: `backend/app/static.py:EXTRA_TYPES` 同一份取值，但各写一份：云端与电脑端
#: 是两套独立部署。
_EXTRA_TYPES: dict[str, str] = {
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

#: 命中这几个前缀的资源名带内容哈希。
_HASHED_MARKERS = ("assets/",)


def _content_type(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in _EXTRA_TYPES:
        return _EXTRA_TYPES[ext]
    guess, _ = mimetypes.guess_type(str(path))
    return guess or "application/octet-stream"


def _cache_for(rel: str, *, is_index: bool) -> str:
    if is_index:
        return PAGE_CACHE
    if any(rel.startswith(m) for m in _HASHED_MARKERS):
        return ASSET_CACHE
    return OTHER_CACHE


def _looks_like_a_file(rel: str) -> bool:
    """这个路径看起来是在要一个**具体文件**吗（而不是页面路由）？

    ★ 不能用 `Path(rel).suffix` —— 它把 `.env`、`.git/config`、
    `.well-known/foo` 都算成"没有扩展名"（`Path("/.env").suffix == ""`，
    因为前导点被当成"隐藏文件名"而非"扩展名"）。而这些恰恰是扫描器
    最先打的东西，一旦被单页回退吃成 200 + HTML，`docs/11` 里"兜底不能
    返回 200"那条就形同废纸。

    判据：**任意一段**以点开头（`.env`、`.git/config`、`.well-known/foo`），
    或最后一段含点（`index-abc.js`、`foo.txt`）。

    ★ 2026-10-23 修：原先只看**最后一段**是否含点，于是 `/.git/config`
    的最后一段是 `config` —— 判成"页面路由"→ 返回 `index.html` 200。
    实测确认（`build/_sec_probe.py`）。这比它看起来更严重：扫描器把
    "200 且带 HTML"记成"这个路径存在"，而我们的本意是 404。

    ★ 这里刻意不用 `rel.replace("\\\\", "/")`：`backend/tests/test_cloud_deploy.py`
    用 AST 把 `cloud/**` 里所有 `.replace()` 调用当成写文件（`os.replace`）
    一并禁掉 —— 那条规则是"只读根"的前提条件，宁可误伤也不能放过一个。
    于是改用 `split("/")` + 单独判反斜杠：URL 里不该出现 `\\`，出现就是想穿越。
    """
    segments = rel.split("/")
    for seg in segments:
        if seg.startswith(".") and seg not in ("", ".", ".."):
            return True
    name = segments[-1]
    if "\\" in name:
        return True
    return "." in name


class StaticPages:
    """把 `frontend/dist` 挂到云端根路径的可调用对象。

    用类而不是闭包：`repr()` 在启动日志里能看出这是什么、在哪、有没有
    找到构建产物 —— 排障时比 `<function <locals>.<lambda> at 0x...>` 有用。

    `enabled=False` 时 `__call__` 返回 404（**不是**抛异常）：云端路由表把
    它注册成最后的兜底，抛异常会变成 500，而"这台云端没带前端产物"和
    "这个路径确实不存在"对用户是同一件事 —— 都是"打开域名什么都没有"。
    """

    __slots__ = ("root", "index", "enabled")

    def __init__(self, dist: str | Path | None = None) -> None:
        self.root: Path | None = None
        self.index: Path | None = None
        self.enabled = False

        candidate = Path(dist) if dist is not None else DEFAULT_DIST
        index = candidate / "index.html"
        if index.is_file():
            self.root = Path(os.path.realpath(str(candidate)))
            self.index = self.root / "index.html"
            self.enabled = True
            log.info("云端托管手机端页面：%s", self.root)
        else:
            # 不是错误：`cloud/` 可以在没有前端构建产物的机器上单独跑
            # （比如只做 relay 压测）。但生产镜像里必须有，见
            # `Dockerfile` 的 `COPY frontend/dist` 与
            # `backend/tests/test_cloud_deploy.py` 的断言。
            log.warning("未找到前端构建产物（%s），云端根路径不提供页面",
                        candidate)

    # ------------------------------------------------------------------

    def _resolve(self, rel: str) -> Path | None:
        """把 URL 路径解析成 dist 内的真实文件；越界或不存在返回 None。"""
        if self.root is None or self.index is None:
            return None
        rel = rel.lstrip("/")
        if not rel:
            return self.index
        # Starlette 给的 `path` 已解码；这里再挡一次显式 `..` 段，
        # 不依赖上游的解码行为（`%2e%2e` 这类二次编码在别的服务器上
        # 有过不同处理）。两种分隔符都切一遍，不能只按 `/` 切 ——
        # 反斜杠在 Windows 上同样是分隔符。
        if ".." in rel.split("/") or ".." in rel.split("\\"):
            return None
        cand = Path(os.path.realpath(str(self.root / rel)))
        try:
            if os.path.commonpath([str(self.root), str(cand)]) != str(self.root):
                return None
        except ValueError:
            # 不同盘符：commonpath 会抛 ValueError。明确的越界。
            return None
        return cand

    async def __call__(self, request: Any) -> Response:
        """根路径的兜底：`https://域名/...` 整体当 dist 内的相对路径。"""
        scope: Scope = request.scope
        rel = str(scope.get("path") or "/")
        return await self.serve(request, rel)

    async def serve(self, request: Any, rel: str) -> Response:
        """按**显式**相对路径服务一个请求。

        为什么不只留 `__call__`：手机端页面有两个入口，而它们的 URL 前缀
        不同 —— 根路径 `/`、以及设备作用域 `/d/{device_id}/`。后者是
        `frontend/vite.config.ts` 的 `base: "./"` 所要求的（`index.html`
        引用 `./assets/index-*.js`，在 `/d/abc/` 下必须解析成
        `/d/abc/assets/...`）。`main.py` 把 `/d/{id}/` 之后的部分剥出来
        交给这个方法，于是同一份产物在两个前缀下都能用，不需要构建两份。
        """
        if not self.enabled or self.index is None:
            return _not_found()

        # 只服务 GET/HEAD。静态资源没有"提交"语义，回 405 会让调试的人
        # 以为路径写错了。
        if request.method.upper() not in ("GET", "HEAD"):
            return _not_found()

        target = self._resolve(rel)
        if target is None or target.suffix.lower() in _BLOCKED_SUFFIXES:
            return _not_found()

        if target.is_dir():
            target = target / "index.html"

        if not target.is_file():
            # 单页应用回退：未知路径给 index.html，由前端自己决定显示什么。
            # 不做这件事的话，用户在界面里刷新会得到 404 —— 而这个 404
            # 来自云端，前端无法接管。
            #
            # ★ 但**带扩展名的不回退**。`/favicon.ico`、`/assets/index-old.js`
            #   这类请求本来是在找一个具体文件，给它一份 HTML 只会让浏览器
            #   报"资源被解释为 script 但传输的 MIME 是 text/html"，而真正
            #   有用的信息（文件不存在）被埋掉了。这也顺带避免把任意拼错的
            #   路径都答成 200 —— 扫描器会据此认为站点"什么都存在"。
            if _looks_like_a_file(rel):
                return _not_found()
            target = self.index

        return self._serve(request, target)

    def _serve(self, request: Any, path: Path) -> Response:
        assert self.root is not None and self.index is not None
        try:
            st = path.stat()
        except OSError:
            return _not_found()

        rel = path.relative_to(self.root).as_posix()
        is_index = path == self.index
        etag = f"{int(st.st_mtime)}-{st.st_size}"

        # 条件请求：外壳设了 no-cache，但仍然允许协商 —— 没变就回 304
        # 不带体。手机流量是真金白银，几十 KB HTML 也一样。
        inm = request.headers.get("if-none-match") or ""
        if _etag_matches(inm, etag):
            resp = Response(status_code=304)
            resp.headers["etag"] = f'"{etag}"'
            resp.headers["cache-control"] = _cache_for(rel, is_index=is_index)
            return resp

        # ★ 传 `stat_result`：FileResponse 会自己拿它算 `content-length` 与
        #   `last-modified`，同时省掉一次 `stat` 系统调用。自己算长度容易
        #   在"文件在这两步之间被替换"时写出一个比实际体更长的头。
        resp = FileResponse(
            str(path),
            media_type=_content_type(path),
            stat_result=st,
            headers={
                "cache-control": _cache_for(rel, is_index=is_index),
                # 外壳与静态资源都是自己构建的，不允许被嗅探成别的类型。
                # 与电脑端 `dispatch.py` 的默认头保持一致。
                "x-content-type-options": "nosniff",
            },
        )
        # ★ 这里加**一层**引号，正好一层。`_etag_matches` 收到的 `etag`
        # 是裸值、比对时自己拼引号；而响应头必须带引号（RFC 7232）。
        # 两边都多写一层会得到 `""1234-13""`，浏览器回传时匹配不上，
        # 304 永远不生效 —— 表现是每次刷新都重下整个 JS/CSS，而缓存头
        # 看起来完全正常。
        resp.headers["etag"] = f'"{etag}"'
        return resp

    def __repr__(self) -> str:
        return f"StaticPages(root={self.root!r}, enabled={self.enabled})"


def _etag_matches(header: str, etag: str) -> bool:
    """`If-None-Match` 是否命中了 `etag`（裸值，不带引号）。

    接受三种形式：`"abc"`、`W/"abc"`、逗号分隔的列表。`*` 一律命中。
    实现刻意短：这里只需要"变了没有"，不需要把 RFC 7232 的全部语法
    搬进来，而一个写错的宽松实现会让**永远 304**（用户升级后仍看到旧
    界面，且刷新无效）。
    """
    raw = (header or "").strip()
    if not raw:
        return False
    if raw == "*":
        return True
    quoted = f'"{etag}"'
    for part in raw.split(","):
        item = part.strip()
        if item == quoted:
            return True
        if item.startswith("W/"):
            item = item[2:].strip()
        if item == quoted:
            return True
    return False


def _not_found() -> Response:
    """云端根路径的 404。

    刻意回 **纯文本 404** 而不是 200：扫描器把 200 当成"路径存在"，一个
    兜底 200 会让 `/.env`、`/wp-login.php` 都看起来可访问。

    ★ P1 之后 `cloud/Caddyfile` 的兜底是 `reverse_proxy`（页面要能到达），
    所以"未匹配路径返回 404"这条保证完全落在这里 —— 它是唯一的把关点。
    改这里之前先读模块顶部的三道约束。
    """
    return text_reply("not found", status=404)
