"""PhotoBridge 配置加载与校验。

分工（docs/05-数据库设计文档.md §4.2）：
  * `config.json` —— **启动前必须知道**的配置（roots、端口、缩略图质量）。改后需重启。
  * `settings` 表 —— **运行时可变**的内部状态（thumb_gen_version、index_last_scan_at）。
  * `data/secrets.json` —— 令牌。**绝不**写进 config.json，绝不进版本控制
    （docs/03-详细设计说明书.md §9.1.5 硬约束）。

为什么 config.json 不和数据库合并：数据库路径本身需要配置（鸡生蛋）；
且配置损坏时程序还能启动并给出可读的错误，数据库损坏则完全起不来。
"""
from __future__ import annotations

import copy
import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Final, Literal, Mapping

import logging

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# 路径常量
# --------------------------------------------------------------------------

#: 程序根目录。
#:
#: * 源码运行：本文件的上两级（backend/app/config.py → backend/app → backend → 仓库根）。
#: * **打包后（PyInstaller）**：必须是 **exe 所在目录**，不能是
#:   `__file__` 推导出来的路径 —— 冻结后 `__file__` 指向
#:   `.../_internal/backend/app/config.py`，于是 DATA_DIR 会变成
#:   `.../_internal/data`，把数据库与缩略图缓存写进**程序目录内部**。
#:   实测后果：整个 `_internal` 目录会被写脏，用户升级时删掉旧目录
#:   就等于删掉数据库（几千张照片的索引与所有缩略图重新生成）。
#:   正确的打包布局是 data/ 与 exe 平级。
def _resolve_app_root() -> Path:
    import sys

    if getattr(sys, "frozen", False):
        # onedir：exe 在 dist/PhotoBridge/PhotoBridge.exe，
        # 数据目录就是 dist/PhotoBridge/data/。
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


APP_ROOT: Final[Path] = _resolve_app_root()
DATA_DIR: Final[Path] = APP_ROOT / "data"
CONFIG_PATH: Final[Path] = DATA_DIR / "config.json"
SECRETS_PATH: Final[Path] = DATA_DIR / "secrets.json"

RootMode = Literal["readonly", "upload", "readwrite"]
ROOT_MODES: Final[frozenset[str]] = frozenset({"readonly", "upload", "readwrite"})

# --------------------------------------------------------------------------
# 白名单与排除表
# --------------------------------------------------------------------------

#: 一期支持的图片扩展名（docs/04 §5.2 E1017）。视频与 RAW 是二期。
#: 注意**不含** `jpg-lx2`：那是小米/红米相册回收站残留，不是真图片
#: （本机 D:\Photos 实测有 22 个），按扩展名直接不索引。
ALLOWED_IMAGE_EXTS: Final[frozenset[str]] = frozenset({
    "jpg", "jpeg", "png", "webp", "gif", "bmp", "tif", "tiff", "avif",
    "heic", "heif",
})

#: 二期启用（此处仅作声明，一期不索引）：视频与 RAW。
FUTURE_VIDEO_EXTS: Final[frozenset[str]] = frozenset({
    "mp4", "mov", "m4v", "avi", "mkv", "3gp", "webm", "wmv", "flv", "mts",
})
FUTURE_RAW_EXTS: Final[frozenset[str]] = frozenset({
    "dng", "cr2", "cr3", "nef", "arw", "raf", "rw2", "orf", "pef", "srw",
})

#: 扫描时跳过的目录名（大小写不敏感）。
#: `.photobridge_tmp` 必须在列：上传临时区就在库根下（docs/03 §4.3.2 的跨卷决策），
#: 不排除会把半截文件当照片索引进去。
DEFAULT_EXCLUDE_DIRS: Final[tuple[str, ...]] = (
    "@eaDir",                       # Synology 缩略图目录
    "System Volume Information",
    "$RECYCLE.BIN",
    ".git",
    "node_modules",
    ".thumbnails",
    "#recycle",                     # Synology 回收站
    ".Trash", ".Trashes",           # macOS / U 盘
    ".photobridge_tmp",             # ★ 本程序的上传临时区
    "data",                         # ★ 本程序的运行数据目录（规则 13 的第二道防线）
    "lost+found",
    ".Spotlight-V100", ".fseventsd",
)

#: 扫描时跳过的文件名前缀/精确名。
DEFAULT_EXCLUDE_FILES: Final[tuple[str, ...]] = (
    "Thumbs.db", "desktop.ini", ".DS_Store", "ehthumbs.db",
)

#: 上传临时区目录名（相对库根）。
TMP_DIRNAME: Final[str] = ".photobridge_tmp"

#: 本地时区偏移（分钟）。默认 +480 = 东八区。
#: 用于 taken_at → 文件名/目录名 的本地化，以及时间线按天分组。
DEFAULT_TZ_OFFSET_MIN: Final[int] = 480

# --------------------------------------------------------------------------
# 权威默认配置（docs/03-详细设计说明书.md §9.1.5）
# --------------------------------------------------------------------------

DEFAULT_CONFIG: Final[dict[str, Any]] = {
    "app": {
        "name": "PhotoBridge",
        "version": "0.1.0",
        # mode: readonly | upload | readwrite。作为未单独指定 root.mode 时的兜底。
        "mode": "readwrite",
        # 托盘默认开：双击 exe 的用户需要一个看得见的图标与退出入口，
        # 否则只能去任务管理器杀进程，而强杀会留下 WAL 日志。
        # 命令行前台运行时用 `--no-tray` 关掉。
        "tray": True,
        # 开机自启：**托盘模式下**启动成功就把注册表 Run 键写成当前该有的样子。
        #
        # 为什么默认开：这个程序没有主窗口。用户第二天开机发现手机连不上，
        # 而原因只是"电脑上那个程序没跑"——这是最容易发生、也最难自查的一类
        # 失败。托盘菜单里有「开机自启」开关，取消勾选即删除注册表项。
        "autostart": True,
    },
    "server": {
        # ★ 默认 127.0.0.1（只本机）。要让手机连上**必须**显式改成 0.0.0.0，
        # 这个改动本身就是一次有意识的安全决策，而不是被默认值悄悄做掉。
        #
        # 为什么不干脆默认 0.0.0.0：那会让"程序装好了但用户还没配对"的窗口期里，
        # 同网段任何人（含访客 Wi-Fi、公司网络）都能探测到 8787 并尝试访问照片库。
        # 首次运行向导会把这一项改成 0.0.0.0 并同时开启 require_pairing。
        "host": "127.0.0.1",
        "port": 8787,
        # 局域网直连开关。二期加 "relay" 时只需在 transports/ 注册新适配器。
        "transport": "local_http",
        # 是否允许绑到非回环地址。False 时 server 启动前会拒绝 0.0.0.0/::，
        # 防止"配置里写错了就整个库暴露在公网"。
        "allow_lan": True,
        # 允许的 Host 头（防 DNS rebinding，审计 F1-c）。
        # 留空 = 自动取「回环 + 本机所有真实网卡 + 主机名」；
        # 写 ["*"] = 完全不校验，**只在反向代理改写 Host 时才用**。
        "trusted_hosts": [],
        # 是否把 /panel/** 挂到 HTTP 上。
        #
        # ★ 默认 False，因为**没有任何调用方**：托盘直接用 handler 函数
        # （不走 HTTP），前端 `grep /panel` 零命中。开着它唯一的作用就是
        # 给"本机上打开的任何网页"留一条路 —— 而本机网页的来源 IP 恰好
        # 就是 127.0.0.1，与用户本人无从区分（审计 F1-b）。
        # 需要面板接口时（脚本化取配对码、排查设备列表）显式打开。
        "panel_http": False,
        # 允许跨源的 Origin 白名单（审计 F1-a）。
        # 空 = 不启用 CORS（一期手机端与 API 同源，不需要）；
        # 开发期 Vite 在 5173，加 "http://localhost:5173" 即可；
        # 写 ["*"] = 显式通配，仅本机调试用。
        "cors_origins": [],
    },
    "roots": [],                    # [{id, path, label, mode, enabled, ...}]
    "upload": {
        "subdir": "手机上传",        # 归档子目录；空字符串表示直接放库根
        "chunk_mb_lan": 8,          # 局域网分片 8 MB
        "chunk_mb_wan": 2,          # 云端分片 2 MB（二期）
        "max_file_mb": 2048,        # 单文件上限 2 GB（设计文档 §1.3）
        "session_ttl_hours": 24,
        "dedup": True,              # sha256 秒传与去重
        # 一期只收图片。**必须在这里显式列出**而不是让上传接口
        # 只看"扩展名在不在索引白名单里"：两者将来会分叉 ——
        # 二期索引要认识视频（能列出来、能播缩略图），
        # 但一期不接受视频上传。混用一个集合时，某天打开视频索引
        # 就顺带打开了视频上传，而存储与转码都还没准备好。
        "allowed_exts": sorted(ALLOWED_IMAGE_EXTS),
    },
    "thumbs": {
        "sizes": [320, 1600],
        "format": "webp",
        "quality": 78,
        "workers": 4,
        "gen_version": 1,           # 生成算法版本变化时递增，触发全量重生成
    },
    "auth": {
        "require_pairing": True,
        "allow_delete": True,       # 删除只进回收站
        "delete_to_recycle_bin": True,
        "allow_move": True,
        # client_token 的有效期（天）。AC-5.10 原文是 90 天，且明写
        # "过期策略可配置"。设为 0 表示永不过期（老库升级上来的行
        # expires_at 为 NULL，语义等同 0）。
        "token_ttl_days": 90,
    },
    "index": {
        "scan_on_start": True,
        "watch": True,              # 文件系统监听（watchdog）
        "hash_on_index": False,     # ★ 默认不算 sha256：4900 文件全量读盘太慢
        "batch_size": 500,
        "debounce_sec": 2.0,
        "exclude_dirs": list(DEFAULT_EXCLUDE_DIRS),
        "exclude_files": list(DEFAULT_EXCLUDE_FILES),
    },
    "locale": {
        "tz_offset_minutes": DEFAULT_TZ_OFFSET_MIN,
    },
    "cloud": {
        # 二期字段。一期留空即可，transport 不会读取。
        #
        # `device_id` 是**云端**分配的 32 位 hex（`scripts/cloud_init.py`
        # 打印的那一行），与本地 `state.device_id`（16 位、电脑自己生成）
        # 是两个不同的东西。云端 `/agent/ws` 的握手要求 HELLO 报的是
        # 前者，因为 `agent_token` 绑在云端那行 `devices.id` 上。
        "base_url": "",
        "enabled": False,
        "device_id": "",
        "agent_token": "",
    },
}


# --------------------------------------------------------------------------
# 配置访问
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RootConfig:
    """一个媒体根目录的配置。

    `mode` 决定沙箱放行哪些操作：
      readonly  → 只读浏览（最安全，推荐给"只读照片库"）
      upload    → 只读 + 可新增（不能删/不能改已有文件）
      readwrite → 只读 + 新增 + 删除（进回收站）+ 移动 + 改名
    """

    id: str
    path: Path
    label: str = ""
    mode: RootMode = "readonly"
    enabled: bool = True
    recursive: bool = True
    include_exts: frozenset[str] = field(default_factory=lambda: ALLOWED_IMAGE_EXTS)
    exclude_dirs: frozenset[str] = field(default_factory=lambda: frozenset(DEFAULT_EXCLUDE_DIRS))
    exclude_files: frozenset[str] = field(default_factory=lambda: frozenset(DEFAULT_EXCLUDE_FILES))

    @property
    def tmp_dir(self) -> Path:
        """上传临时区。**必须与库同卷**，否则 os.replace 会退化成跨卷复制（非原子）。"""
        return self.path / TMP_DIRNAME

    @property
    def can_write(self) -> bool:
        return self.mode in ("upload", "readwrite")

    @property
    def can_delete(self) -> bool:
        return self.mode == "readwrite"

    @property
    def can_modify(self) -> bool:
        """能不能改动**已存在**的条目（移动/改名）。

        比 `can_write` 严一档：`upload` 模式的含义是"只许新增、不许改已有
        文件"。移动和改名都直接改写用户原有的目录组织（审计 F01：这两个
        接口原来用的是 `can_write`，于是只允许上传的目录也能把整库照片
        搬走、改名）。
        """
        return self.mode == "readwrite"

    def display(self) -> str:
        return self.label or self.id


class ConfigError(Exception):
    """配置文件无法使用。这类错误必须让用户看到原文（含路径），
    因为它唯一的修复方式就是用户去改那个文件——与 API 响应的脱敏规则不同。"""


class UnknownRootError(ConfigError):
    """请求里指定的 `root` / `root_id` 不存在（审计 F7）。

    与 `ConfigError` 分开，是因为两者的**责任方不同**：

    - `ConfigError`：配置文件本身有问题 → 该由用户在电脑上改 config.json，
      必须连路径一起打印，且不该被 API 层改写成 4xx。
    - `UnknownRootError`：**请求**带了一个不存在的根 id → 客户端的错，
      应当 400，且不该把磁盘路径拼进响应体。

    早先两者混为一谈，后果是 `?root=nope` 走 `EXC_MAP` 未命中 → `Internal(None)`
    → `500 E3001 internal_error`。用户在手机上看到的提示是"服务器开小差了"，
    而真正的原因（根 id 写错）只有看电脑日志才知道。
    """


class RootDisabledError(ConfigError):
    """请求指向的根目录存在，但被管理员设成了 `enabled: false`（审计 F01）。

    为什么单独立一个类、而不是复用 `UnknownRootError`：两者的**修复方式
    不同** —— `UnknownRootError` 是"id 写错了"，用户改客户端就行；这里 id
    是对的，是管理员主动停了这块共享，客户端没有任何办法自救。

    但**对外的表现必须一样**：都是 404 `E1004`。若对"已停用"回 403，
    持有有效令牌的人就能借此枚举出"这台电脑上有哪些根目录"（403 与 404
    的差别本身就是信息泄漏）。所以 `errors.to_app_error()` 把两者都翻成
    404，只在电脑端日志里区分。

    ★ 这条闸门的默认方向是**拒绝**：`Config.root_by_id()` 默认不返回
    停用根。任何"确实需要读停用根"的内部用途（索引维护命令等）必须显式
    写 `allow_disabled=True` —— 默认安全，例外留痕。
    """

    def __init__(self, root_id: str) -> None:
        super().__init__(f"媒体根目录已停用：{root_id!r}")
        self.root_id = root_id


class Config:
    """配置对象。不可变语义：改配置请改 raw 后 save()。"""

    __slots__ = ("_raw", "_path", "_roots", "_secrets", "_data_dir")

    def __init__(self, raw: dict[str, Any], path: Path = CONFIG_PATH,
                 *, data_dir: Path | None = None) -> None:
        self._raw = _merge(DEFAULT_CONFIG, raw)
        self._path = path
        self._roots: list[RootConfig] | None = None
        self._secrets: dict[str, Any] | None = None
        # data_dir 的解析规则（顺序不能变）：
        #
        #   1. 显式传了 data_dir  → 用它。测试必须能把缩略图/审计日志/
        #      数据库写到 tmp_path，否则跑一次测试就会往真实 data/ 目录里
        #      留垃圾，更糟的是会**覆盖真实库里已有的缩略图缓存**。
        #   2. 否则 → 跟着**配置文件所在目录**走（`<config 的目录>/data`）。
        #   3. 都没有 → 安装目录下的 DATA_DIR。
        #
        # ★ 第 2 条是修一个真实事故：`main.py --config H:/tmp/pbtest/cfg.json`
        # 会从那个文件读 roots（指向测试照片库），但 data_dir 若固定成
        # 安装目录的 DATA_DIR，数据库仍然是**用户真实库的索引**。
        # 于是测试扫描完自己的 6 个文件后，`_mark_missing(root_id='library')`
        # 把真实库的 4915 行全部标成"文件已丢失" —— 用户打开 App 看到空相册。
        #
        # 默认情况（CONFIG_PATH == DATA_DIR/"config.json"）下第 2 条
        # 算出来的正是 DATA_DIR，行为与从前完全一致，不影响老用户。
        if data_dir is not None:
            self._data_dir = Path(data_dir)
        elif path is not None:
            parent = Path(path).resolve().parent
            # 默认布局下配置文件**就在**数据目录里（`DATA_DIR/config.json`），
            # 此时 data_dir 就是它所在的目录，绝不能再拼一层 "data"。
            # 判断用两条：等于默认 DATA_DIR，或目录本身叫 "data"
            # （用户把整个程序拷到别处时 __file__ 会跟着变，DATA_DIR 仍然对；
            #  而 "叫 data" 这条覆盖手工指定数据目录的约定用法）。
            if parent == Path(DATA_DIR).resolve() or parent.name.lower() == "data":
                self._data_dir = parent
            else:
                self._data_dir = parent / "data"
        else:                                       # pragma: no cover
            self._data_dir = DATA_DIR

    # --- 分节访问 ---------------------------------------------------------

    @property
    def raw(self) -> dict[str, Any]:
        return self._raw

    @property
    def app(self) -> dict[str, Any]:
        return self._raw["app"]

    @property
    def server(self) -> dict[str, Any]:
        return self._raw["server"]

    @property
    def upload(self) -> dict[str, Any]:
        return self._raw["upload"]

    @property
    def thumbs(self) -> dict[str, Any]:
        return self._raw["thumbs"]

    @property
    def auth(self) -> dict[str, Any]:
        return self._raw["auth"]

    @property
    def index(self) -> dict[str, Any]:
        return self._raw["index"]

    @property
    def cloud(self) -> dict[str, Any]:
        return self._raw["cloud"]

    @property
    def tz_offset_minutes(self) -> int:
        return int(self._raw["locale"]["tz_offset_minutes"])

    # --- 便捷取数 ---------------------------------------------------------

    def get(self, dotted: str, default: Any = None) -> Any:
        """`cfg.get("upload.max_file_mb")` 形式的点分路径读取。"""
        node: Any = self._raw
        for part in dotted.split("."):
            if not isinstance(node, Mapping) or part not in node:
                return default
            node = node[part]
        return node

    # --- 根目录 -----------------------------------------------------------

    @property
    def roots(self) -> list[RootConfig]:
        if self._roots is None:
            self._roots = [_root_from_dict(d, self._raw["app"]["mode"]) for d in self._raw["roots"]]
            # ★ 规则 13：在这里**硬拦截**，而不是启动时只打个警告。
            # 配置错到"根目录就是 data 目录"这一步时，程序继续跑等于
            # 主动把审计日志与数据库当照片提供服务；用户看到的是"照片
            # 能打开"，没有任何可感知的异常。宁可起不来，也不能悄悄泄漏。
            for r in self._roots:
                ok, why = check_root_safety(r, self)
                if not ok:
                    raise ConfigError(f"{why}。请修改 config.json 的 roots")
        return self._roots

    @property
    def enabled_roots(self) -> list[RootConfig]:
        return [r for r in self.roots if r.enabled]

    def root_by_id(self, root_id: str | None,
                   *, allow_disabled: bool = False) -> RootConfig:
        """按 id 取根。root_id 为空时返回第一个启用的根。

        未知 id 抛 `UnknownRootError`（ConfigError 的子类，见其 docstring）——
        这是请求参数错，要被 API 层翻成 400，而不是 500。

        ★ `allow_disabled=False`（默认）时，命中 `enabled: false` 的根抛
        `RootDisabledError` → 404 E1004（审计 F01）。这条默认值就是修复的
        核心：管理员停用某块共享之后，**所有**按 id 取根的地方（原图、缩略图、
        预览、上传、移动/改名）自动一起失效，不需要逐个 handler 去记得加
        检查。需要读停用根的内部用途必须写 `allow_disabled=True`，一眼可查。
        """
        if root_id is None:
            roots = self.enabled_roots
            if not roots:
                raise ConfigError("没有启用任何媒体根目录，请检查 config.json 的 roots")
            return roots[0]
        for r in self.roots:
            if r.id == root_id:
                if not r.enabled and not allow_disabled:
                    raise RootDisabledError(root_id)
                return r
        raise UnknownRootError(f"未知的根目录 id: {root_id!r}")

    @property
    def default_root_id(self) -> str:
        return self.root_by_id(None).id

    def has_root(self, root_id: str) -> bool:
        return any(r.id == root_id for r in self.roots)

    # --- data 目录 --------------------------------------------------------

    @property
    def data_dir(self) -> Path:
        return self._data_dir

    def data_path(self, *parts: str) -> Path:
        return self._data_dir.joinpath(*parts)

    def thumbs_dir(self, size: int) -> Path:
        return self._data_dir / "thumbs" / str(size)

    @property
    def db_path(self) -> Path:
        return self._data_dir / "photobridge.db"

    @property
    def audit_log_path(self) -> Path:
        return self._data_dir / "audit.log"

    @property
    def logs_dir(self) -> Path:
        return self._data_dir / "logs"

    # --- secrets（令牌） --------------------------------------------------

    @property
    def secrets(self) -> dict[str, Any]:
        """惰性读取 data/secrets.json。文件不存在时返回空 dict（首次运行）。"""
        if self._secrets is None:
            self._secrets = _read_json(self._data_dir / "secrets.json") or {}
        return self._secrets

    def secret(self, key: str, default: str | None = None) -> str | None:
        v = self.secrets.get(key, default)
        return v if isinstance(v, str) else default

    def set_secret(self, key: str, value: str) -> None:
        self.secrets[key] = value
        _write_json(self._data_dir / "secrets.json", self.secrets, private=True)

    # --- 持久化 -----------------------------------------------------------

    def save(self, path: Path | None = None) -> Path:
        """原子写回 config.json。

        先写同目录临时文件再 os.replace —— 否则写到一半断电会留下半个 JSON，
        下次启动直接起不来（docs/05 §4.2 提到的"写一半损坏"风险）。
        """
        target = path or self._path
        _write_json(target, self._raw, private=False)
        return target

    def __repr__(self) -> str:
        return f"<Config roots={[r.id for r in self.roots]} port={self.server['port']}>"


# --------------------------------------------------------------------------
# 内部工具
# --------------------------------------------------------------------------


def _root_from_dict(d: Mapping[str, Any], app_mode: str) -> RootConfig:
    rid = str(d.get("id") or "").strip()
    raw_path = str(d.get("path") or "").strip()
    if not rid:
        raise ConfigError(f"roots 条目缺少 id: {d!r}")
    if not raw_path:
        raise ConfigError(f"根目录 {rid!r} 缺少 path")
    mode = str(d.get("mode") or app_mode)
    if mode not in ROOT_MODES:
        raise ConfigError(f"根目录 {rid!r} 的 mode={mode!r} 非法，可选 {sorted(ROOT_MODES)}")

    include = d.get("include_exts")
    exts = (
        frozenset(str(e).lower().lstrip(".") for e in include)
        if include
        else ALLOWED_IMAGE_EXTS
    )
    # 白名单收窄是允许的，放宽到白名单之外是禁止的——二期加视频时改这里。
    unknown = exts - ALLOWED_IMAGE_EXTS - FUTURE_VIDEO_EXTS - FUTURE_RAW_EXTS
    if unknown:
        raise ConfigError(f"根目录 {rid!r} 的 include_exts 含未知扩展名: {sorted(unknown)}")

    return RootConfig(
        id=rid,
        # expanduser 支持 "~/photos"；不 resolve()，保留用户原样路径便于报错时显示
        path=Path(os.path.expandvars(os.path.expanduser(raw_path))),
        label=str(d.get("label") or ""),
        mode=mode,  # type: ignore[arg-type]
        enabled=bool(d.get("enabled", True)),
        recursive=bool(d.get("recursive", True)),
        include_exts=exts,
        exclude_dirs=frozenset(
            str(x).lower() for x in (d.get("exclude_dirs") or DEFAULT_EXCLUDE_DIRS)
        ),
        exclude_files=frozenset(
            str(x).lower() for x in (d.get("exclude_files") or DEFAULT_EXCLUDE_FILES)
        ),
    )


#: 允许作为媒体根的比较基准：**必须落在数据目录之外**。
#: 规则 13（`docs/06-安全设计文档.md` §10）要求「绝不通过 API 暴露审计日志与
#: 配置」。该规则原本只被理解为"不要加一条读 data/ 的路由"——那是**消极**
#: 的实现：真正的风险在于用户（或被改写的 config.json）把根目录设成
#: `<程序目录>\data`，从此时间线、搜索、缩略图原图下载**全部会把审计日志
#: 与 photobridge.db 当照片吐出去**（`/api/items/<id>/raw`、`/api/search`
#: 都会命中）。沙箱是按"根目录内"判定的，它无法区分"这是用户照片"还是
#: "这是我们自己的运行数据"。
#:
#: 因此这一层必须由配置侧拦住，而不是靠路由白名单。
#: 判据函数是 `check_root_safety()`。


def _is_inside(child: Path, parent: Path) -> bool:
    """`child` 是否等于 `parent` 或位于其下。两边都先 resolve（消 symlink/..）。"""
    try:
        c = child.resolve()
        p = parent.resolve()
    except (OSError, RuntimeError):
        return False
    return c == p or p in c.parents


def check_root_safety(root: RootConfig, cfg: Config) -> tuple[bool, str]:
    """判断某个媒体根是否安全。返回 `(ok, 说明)`，**只判断不抛异常**。

    规则 13（`docs/06-安全设计文档.md` §10）要求「绝不通过 API 暴露审计日志
    与配置」。只做到"别加一条读 data/ 的路由"是**消极**实现 —— 真正的风险
    在配置侧：一旦媒体根被设成数据目录（或它的父目录），全流程就都把运行
    数据当照片处理了，而沙箱只判断"在根目录内"，它无法区分用户照片与我们
    自己的运行文件：

      * `/api/timeline`、`/api/search` 会收录 `thumbs/**/*.webp`；
      * `/api/items/<id>/raw` 会把它们以照片身份吐出去；
      * `audit.log`、`photobridge.db`、`secrets.json` 同在一个目录里。

    ## 三种情形，两种处理

    1. **根目录就是数据目录，或在它内部** → 硬拒绝。
       此时数据目录里的每个文件都在根的覆盖范围内，没有任何缓冲。

    2. **数据目录在根目录内部，且数据目录的目录名不在扫描排除表里**
       → 硬拒绝。索引器会 `rglob("*")` 走进它。

    3. **数据目录在根目录内部，但目录名已被 `DEFAULT_EXCLUDE_DIRS` 排除**
       → 放行，但在说明里点出来。这是**常见且合理**的布局
       （用户把程序装在照片盘上，或测试用临时目录既当库又当数据目录），
       排除表已经保证了索引不越界。硬拒它会把正常用法判死，
       而用户没有任何自助修复手段。

    判定用**目录名**而不是全路径比较，因为 `data` 的位置随安装方式变化
    （开发期是 `仓库/data`，PyInstaller 下是 `dist/PhotoBridge/data`）。

    返回说明文本，供日志与托盘提示使用 —— 与 `check_bind_safety` 一致。
    """
    data_dir = cfg.data_dir
    excluded = {str(x).lower() for x in root.exclude_dirs}

    if _is_inside(root.path, data_dir):
        return False, (
            f"根目录 {root.id!r} 指向数据目录 {data_dir}（或其内部），"
            "会把审计日志与数据库当照片暴露出去"
        )

    if _is_inside(data_dir, root.path):
        if data_dir.name.lower() not in excluded:
            return False, (
                f"根目录 {root.id!r} 包含了数据目录 {data_dir}，"
                f"而 {data_dir.name!r} 不在扫描排除表里，"
                "索引会把缩略图缓存当照片收进来"
            )
        return True, f"ok（索引会跳过其中的 {data_dir.name}/）"

    return True, "ok"


def _merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """深合并：override 覆盖 base，嵌套 dict 递归合并，list/标量整体替换。

    为什么用深合并而不是整体替换：用户手写的 config.json 通常只写关心的几项
    （如只有 roots 和 port）。整体替换会让新增的默认键在升级后消失，
    导致 KeyError 或行为回退。

    ★ 为什么必须 deepcopy，而不是 `dict(base)`：`dict()` 只复制**第一层**，
    嵌套的 dict 仍然指向原对象。而 `base` 是模块级的 `DEFAULT_CONFIG`，
    于是任何一份「没写 server 节」的配置，它的 `cfg.raw["server"]` 就是
    `DEFAULT_CONFIG["server"]` 本身 —— 之后 `cfg.raw["server"]["host"] = x`
    改的不是这份配置，而是**全进程的默认值**，下一个新建的 Config 会带着
    上一次写进去的值出生。

    这不是理论问题：`main.py` 的 `--host` 参数与首启向导的 host 写入都走
    这条路，实测中表现为「先跑一条把 host 写成 0.0.0.0 的用例，后面每条
    用例都带着 0.0.0.0 出生」，连「关掉 require_pairing 就该拒绝监听」这条
    安全用例都被污染成通过。嵌套只有两三层的量级，拷贝成本可以忽略。
    """
    out: dict[str, Any] = copy.deepcopy(dict(base))
    for k, v in override.items():
        if isinstance(v, Mapping) and isinstance(out.get(k), Mapping):
            out[k] = _merge(out[k], v)  # type: ignore[arg-type]
        else:
            out[k] = v
    return out


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise ConfigError(
            f"配置文件不是合法 JSON：{path}\n第 {e.lineno} 行第 {e.colno} 列：{e.msg}"
        ) from e
    except OSError as e:
        raise ConfigError(f"无法读取配置文件 {path}：{e.strerror or e}") from e
    if not isinstance(data, dict):
        raise ConfigError(f"配置文件顶层必须是 JSON 对象，实际是 {type(data).__name__}：{path}")
    return data


def _write_json(path: Path, data: Mapping[str, Any], *, private: bool) -> None:
    """原子写 JSON。private=True 时把权限收到 0600（secrets.json 用）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())        # 断电安全：先落盘再改名
        if private:
            _restrict(tmp_name)
        os.replace(tmp_name, path)      # 同目录 → 同卷 → 真原子
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _restrict(path: str | Path) -> None:
    """把文件权限收到 0600。

    Windows 上 os.chmod 只影响只读位，真正的作用有限；这里仍调用是为了
    在 Linux/macOS 上运行云端或开发环境时行为一致（docs/03 §9.1.5 要求 0600）。
    """
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


# --------------------------------------------------------------------------
# 模块级单例
# --------------------------------------------------------------------------

_config: Config | None = None


def load(reload: bool = False) -> Config:
    """加载配置（带缓存）。首次运行无 config.json 时使用默认值，**不写盘**——
    写盘由首次运行向导触发，否则用户还没来得及改就被默认配置固化了。"""
    global _config
    if _config is None or reload:
        raw = _read_json(CONFIG_PATH) or {}
        _config = Config(raw, CONFIG_PATH)
    return _config


def get() -> Config:
    return _config if _config is not None else load()


def ensure_dirs(cfg: Config | None = None) -> None:
    """创建运行所需目录。幂等，启动时调用一次。"""
    c = cfg or load()
    c.data_dir.mkdir(parents=True, exist_ok=True)
    c.logs_dir.mkdir(parents=True, exist_ok=True)
    for size in c.thumbs["sizes"]:
        c.thumbs_dir(int(size)).mkdir(parents=True, exist_ok=True)


def is_first_run(cfg: Config | None = None) -> bool:
    """从未完成过配置（config.json 不存在或 roots 为空）。

    ★ 为什么要能接受一个 `cfg`：`serve(cfg)` 拿到的可能是 `--config`
    指定的**另一份**配置。若这里只看全局 `CONFIG_PATH`，那么用
    `--config H:/tmp/x/cfg.json` 指向一份已写好的配置时，本函数仍会去
    看默认的 `data/config.json`；默认配置不存在（或 roots 为空）时它
    返回 True，`serve()` 随即调 `bootstrap_default_root()` 再用
    `get_config()` **把调用方传进来的 cfg 整个丢掉** —— 用户以为在用
    A 配置，实际跑的是自动探测出来的 B。

    实测后果（本机）：`--config` 指向 `H:/tmp/freshclone/_lib`（1 张图），
    程序却去索引了自动探测到的 `D:\\Photos`，并在那份库上
    `marked 2000 rows missing`。这与"事故 2"（`--config` 不影响
    data_dir）是同一类错误的另一半：**传进来的配置必须全程有效**。
    """
    if cfg is not None:
        try:
            return not cfg.roots
        except ConfigError:
            return True
    if not CONFIG_PATH.exists():
        return True
    try:
        return not load().roots
    except ConfigError:
        return True


# --------------------------------------------------------------------------
# 绑定地址安全校验
# --------------------------------------------------------------------------

#: 回环地址。绑这些地址只有本机可访问，无需配对即可视为安全。
LOOPBACK_HOSTS: Final[frozenset[str]] = frozenset({"127.0.0.1", "::1", "localhost"})

#: 明确表示"监听所有网卡"的地址。绑定它们等于把服务暴露给整个可达网段。
WILDCARD_HOSTS: Final[frozenset[str]] = frozenset({"0.0.0.0", "::", "*"})


def is_loopback_host(host: str) -> bool:
    h = (host or "").strip().lower()
    if h in LOOPBACK_HOSTS:
        return True
    # 127.0.0.0/8 整段都是回环
    return h.startswith("127.")


def check_bind_safety(host: str, cfg: Config | None = None) -> tuple[bool, str]:
    """判断绑定该地址是否安全。返回 (ok, 说明)。

    三条规则：
      1. 回环地址永远允许。
      2. 通配地址（0.0.0.0/::）要求 `server.allow_lan` 为真**且**已配置媒体根
         **且** `auth.require_pairing` 为真 —— 三者缺一不可。任何一个缺失都意味着
         "任何人都能访问照片库"，这比服务起不来严重得多。
      3. 具体 IP 地址要求 `server.allow_lan` 为真；若是公网 IP 则一律拒绝。

    这个函数只做判断，不抛异常 —— 调用方（server 启动流程）决定是拒绝启动
    还是仅打警告。托盘与首启向导用同一函数给出提示文案。
    """
    c = cfg or get()
    h = (host or "").strip().lower()

    if is_loopback_host(h):
        return True, "仅本机可访问"

    if h in WILDCARD_HOSTS:
        if not c.server.get("allow_lan", True):
            return False, "server.allow_lan 为 false，不允许监听所有网卡"
        if not c.roots:
            return False, "尚未配置媒体根目录，拒绝监听所有网卡"
        if not c.auth.get("require_pairing", True):
            return False, "auth.require_pairing 为 false，监听所有网卡将无鉴权暴露照片库"
        if bool(c.get("server.panel_http", False)):
            # `/panel/**` 只有"来源 IP 是回环"一道门（审计 F1-b），而本机
            # 浏览器发出的请求来源 IP 就是 127.0.0.1 —— 与用户本人无从区分。
            # 局域网可达时把面板挂出去，等于让任何网页都能替用户生成配对码。
            return False, "server.panel_http 为 true 时不允许监听所有网卡"
        return True, "局域网可访问（需配对）"

    if not c.server.get("allow_lan", True):
        return False, f"server.allow_lan 为 false，不允许绑定 {host}"

    if _is_public_ip(h):
        return False, (
            f"{host} 是公网地址。本程序一期只做局域网访问，"
            "公网暴露请走二期云中继（不需要端口映射）。"
        )

    # ★ 审计 F04：原来这两个检查只写在**通配地址**分支里，于是
    #   `server.host=192.168.1.10` + `auth.require_pairing=false` 能起一个
    #   任何同网段设备都能读照片的无鉴权服务 —— 与通配地址一样危险，
    #   只是"看起来只绑了一个网卡"。任何非回环监听都必须过这两关。
    if not c.auth.get("require_pairing", True):
        return False, (
            "auth.require_pairing 为 false，绑定非回环地址将无鉴权暴露照片库"
        )
    if bool(c.get("server.panel_http", False)):
        return False, "server.panel_http 为 true 时不允许绑定非回环地址"

    return True, f"绑定到 {host}（需确认该地址属于可信网段）"


def _is_public_ip(host: str) -> bool:
    """判断是否公网 IP。非 IP 字面量（主机名）一律当作公网处理 —— 保守优先。"""
    import ipaddress

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True         # 域名 → 可能解析到公网，保守拒绝
    return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved)


#: 确定不属于「家庭局域网」的网段。手机不可能通过这些地址连上电脑。
#:
#: - `26.0.0.0/8`     Radmin VPN（实测本机 `26.26.192.40`）
#: - `25.0.0.0/8`     Hamachi
#: - `100.64.0.0/10`  运营商级 NAT（CGNAT），设备之间不互通
#: - `169.254.0.0/16` 链路本地，只在没有 DHCP 时自赋值
#: - `172.16.0.0/12`  Hyper-V / WSL2 默认段
#: - `127.0.0.0/8`    回环
#:
#: 这些是**启发式**，不是安全边界：过滤错了顶多是"少显示一个可用地址"
#: （用户仍可从 `server.host` 手填），而漏过滤会让手机去连一个永远连不上的
#: 地址，用户看到的是"应用坏了"。所以宁多滤不少滤。
_NON_LAN_CIDRS: Final[tuple[str, ...]] = (
    "26.0.0.0/8",
    "25.0.0.0/8",
    "100.64.0.0/10",
    "169.254.0.0/16",
    "172.16.0.0/12",
    "127.0.0.0/8",
)


def _in_non_lan_net(addr: str) -> bool:
    """`addr` 是否落在已知的非局域网网段内（解析失败也算）。"""
    import ipaddress

    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return True                    # 解析不了就当不可用，保守
    if ip.version != 4:
        return True
    return any(
        ip in ipaddress.ip_network(cidr) for cidr in _NON_LAN_CIDRS
    )


def _routes_off_link(addr: str) -> bool:
    """把 `addr` 作为源地址去连一个外网地址，判断它是否真的**能出网**。

    这是区分「真实局域网网卡」与「host-only 虚拟网卡」最可靠的手段：
    Hyper-V 内部交换机与 VMware host-only 网卡没有默认路由，绑定后
    connect() 会直接抛 `WSAENETUNREACH (10051)`；而真实网卡能拿到路由。

    实测（本机 5 个 IPv4）：

    - `192.0.2.10`  真实网卡        → OK
    - `26.26.192.40`   Radmin VPN     → OK（VPN 真的能路由，靠网段表过滤）
    - `192.168.226.1`  VMware VMnet8  → FAIL 10051
    - `192.168.64.1`   VMware VMnet1  → FAIL 10051
    - `192.168.32.1`   WSL vEthernet  → FAIL 10051

    注意 UDP connect 不发包，只做路由表查询，因此没有网络开销也不会被防火墙拦。
    """
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind((addr, 0))
        s.connect(("223.5.5.5", 80))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _usable_lan_ips(
    primary: str | None,
    others: list[str],
    *,
    routes_off_link: "Callable[[str], bool] | None" = None,
) -> list[str]:
    """从候选地址中挑出手机真能连上的那些。

    与系统调用分离，便于单测注入假的 `routes_off_link`。

    规则：
      1. 路由选出的 `primary` 永远保留（它就是默认网卡地址）。
      2. 其余地址必须**同时**满足：不在已知非局域网网段、且真的能出网。
    """
    route = routes_off_link or _routes_off_link
    out: list[str] = []

    def _push(a: str) -> None:
        if a and a not in out:
            out.append(a)

    if primary and not _in_non_lan_net(primary):
        _push(primary)

    for addr in others:
        if not addr or addr in out:
            continue
        if _in_non_lan_net(addr):
            continue
        if not route(addr):
            continue
        _push(addr)
    return out


def local_ipv4_addresses() -> list[str]:
    """列出本机**手机真能连上**的 IPv4 地址，默认网卡在前。

    为什么必须过滤：本机实测有 5 个 IPv4，其中 4 个来自虚拟网卡
    （Radmin VPN `26.26.192.40`、VMware VMnet1 `192.168.64.1`、
    VMware VMnet8 `192.168.226.1`、WSL vEthernet `192.168.32.1`），
    **只有 `192.0.2.10` 是真实局域网地址**。

    把它们显示给用户（托盘二维码、`/api/system/info` 的 `lan_ips`）会
    让手机去连一个永远连不上的地址，而用户的感受是"这个软件坏了" ——
    这类问题极难自助排查，所以在这里就滤掉。

    早期实现只用网段前缀过滤（只认识 172.16–172.31），实测漏掉 4 个中的 3 个；
    现在改为「网段表 + 源地址路由探测」双重判据，实测 5 个地址里正确留下 1 个。
    """
    import socket

    primary: str | None = None
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # 主路由探测：连一个外部地址（不实际发包，UDP connect 只做路由选择）
        probe.connect(("223.5.5.5", 80))
        got = probe.getsockname()[0]
        if got and not got.startswith("127."):
            primary = got
    except OSError:
        pass
    finally:
        probe.close()

    others: list[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addr = info[4][0]
            if addr and addr not in others:
                others.append(addr)
    except OSError:
        pass

    return _usable_lan_ips(primary, others)


def bootstrap_default_root(cfg: Config | None = None) -> Config:
    """首次运行时写出一份 config.json。

    ## 为什么不再硬编码 `D:\\Photos`

    这段代码原本直接写死开发机的照片库路径。后果：**任何别的用户**
    装好程序后，它会去索引一个他机器上不存在的 `D:\\Photos`
    （或更糟 —— 那个路径恰好存在但装的是别人的东西）。
    本机（开发机）当然有这个目录，所以这个 bug 在开发机上完全
    看不出来，只在分发后才暴露。

    现在的策略：**自动探测常见位置**，探测不到就写一个空的 roots
    并留下明确提示，由用户在设置页里选目录。宁可让用户点一次
    "选择照片库"，也不要让程序去翻一个猜出来的路径。

    ★ `cfg` 参数（与 `is_first_run` 同理）：调用方可能是拿
    `--config` 传进来的配置对象。若这里用全局 `load()`，写盘会写到
    默认的 `data/config.json`，而调用方手上那份配置对象根本没被更新 ——
    于是"探测结果"和"实际运行的配置"变成两份不同的东西。
    传进来时：就地改它的 `raw`、写到**它自己的路径**、并就地返回它。
    """
    if cfg is None:
        cfg = load()
    if cfg.roots:
        return cfg

    found = _guess_library_paths()
    # ★ 探测结果必须先过规则 13：自动探测是**无人复核**的路径，
    # 若它恰好选中了数据目录（或数据目录的父目录），后面所有
    # "根目录内即可访问"的判定都会把运行数据当照片放出去。
    if found:
        cand, label = found[0]
        probe = RootConfig(id="library", path=cand, label=label, mode="readwrite")
        safe, why = check_root_safety(probe, cfg)
        if not safe:
            log.warning("首次运行：自动探测到 %s 但不能用 —— %s", cand, why)
            found = []

    if found:
        path, label = found[0]
        log.info("首次运行：自动发现照片库 %s", path)
        cfg.raw["roots"] = [
            {
                "id": "library",
                "path": str(path),
                "label": label,
                "mode": "readwrite",
                "enabled": True,
                "recursive": True,
            }
        ]
    else:
        log.warning(
            "首次运行：未能自动发现照片库，请在设置页手动选择目录"
            "（config.json 里 roots 为空）"
        )
        cfg.raw["roots"] = []

    # 就地失效缓存：roots 是惰性解析并缓存的，改完 raw 必须让它重算，
    # 否则 `cfg.roots` 还是改之前的空列表，下面 check_bind_safety 会
    # 因为「没有媒体根目录」而拒绝 —— 明明刚写进去。
    cfg._roots = None       # noqa: SLF001

    # ★ 首次运行向导的**另一半**。
    #
    # DEFAULT_CONFIG 的注释里承诺过「首次运行向导会把这一项改成 0.0.0.0
    # 并同时开启 require_pairing」，但这段代码从来没兑现 —— 它只写 roots，
    # 从不碰 server.host。后果是**全新安装默认绑回环**：托盘已经打印出
    # `http://192.168.x.x:8787`，手机打开却是「目标计算机积极拒绝」，而
    # 电脑端一切正常，用户完全无从判断该改哪里（实测踩到）。
    #
    # 只在两个条件同时成立时改：
    #   ① 真的探测到了照片库 —— 没库就没必要开监听，开了也只能回 404；
    #   ② `check_bind_safety("0.0.0.0", cfg)` 放行 —— 复用同一套三道门
    #      （allow_lan / roots 非空 / require_pairing），保证**不会**出现
    #      「无鉴权暴露整个照片库」。把判断交给它而不是在这里重写一遍
    #      条件，是为了将来加门时两处不会走偏。
    #
    # 用户显式写过的 host 一律尊重：只在它还是默认值时才动。
    if found and cfg.server.get("host") == DEFAULT_CONFIG["server"]["host"]:
        ok, why = check_bind_safety("0.0.0.0", cfg)
        if ok:
            cfg.raw.setdefault("server", {})["host"] = "0.0.0.0"
            log.info("首次运行：已开启局域网直连（%s），手机访问上面的地址即可", why)
        else:
            log.warning(
                "首次运行：未开启局域网直连（%s），手机将连不上；可在设置里打开",
                why,
            )

    cfg.save()
    return cfg


#: 自动探测时的候选目录名。**精确匹配**（大小写不敏感），
#: 不做子串匹配 —— 本机实测 `Adobe_Photoshop_2020` 会被
#: "photos" 子串命中，把一个 Photoshop 安装目录当成照片库。
_LIBRARY_DIR_NAMES: Final[frozenset[str]] = frozenset({
    "相册", "照片", "图片", "我的照片", "照片库", "媒体库",
    "图片库", "相片", "我的相册", "手机照片", "手机相册",
    "pictures", "photos", "photolibrary", "photo library",
    "my pictures", "my photos", "dcim", "images",
})

#: 自动探测的起始位置。只在每个盘符下**有界地**看两层 ——
#: 无界递归会花几分钟，而用户会以为程序卡死了。
#: 为什么是两层：常见布局既有 `D:\Photos`（一层），也有
#: 本机这种 `D:\Photos`（两层）。三层以上就交给用户手选。
_PROBE_ROOTS: Final[tuple[str, ...]] = ("E:\\", "D:\\", "F:\\", "G:\\")

#: 探测时**不进入**的目录名（系统目录、程序目录、依赖目录）。
#: 不排除的话，`C:\Program Files` 下几万个目录会让探测慢得离谱。
_PROBE_SKIP: Final[frozenset[str]] = frozenset({
    "windows", "program files", "program files (x86)", "programdata",
    "$recycle.bin", "system volume information", "recovery",
    "appdata", "application data", "node_modules", ".git", ".cache",
    "intel", "amd", "nvidia", "drivers", "temp", "tmp", "msocache",
})


def _guess_library_paths() -> list[tuple[Path, str]]:
    """在常见位置找照片库，返回 [(路径, 显示名)]，最多两个。

    ## 为什么是"枚举 + 比对名字"而不是"遍历前 N 个目录再筛"

    第一版实现是"有界 BFS 遍历前 N 个目录，遇到名字匹配的就采纳"。
    实测在本机**完全失败**：`E:\\` 根下先被枚举的是
    `Chat software`、`Config.Msi`、`Galgame`、`software`… 等 120 个
    无关目录，预算耗尽时还没走到 `D:\\Photos`。
    调大预算不解决问题 —— 只是把"漏掉"换成"慢"。

    ## 为什么也不能"按候选名拼路径"

    第二版改成 `base / name` 直接拼（`name` 来自名字集合）。
    在 Windows 上拼出来的路径**大小写来自集合**：集合里存的是
    `"photos"`，`E:\\Photos` 也能 `is_dir()` 成功（NTFS 大小写不敏感），
    于是返回的 Path 显示成 `photos` —— 用户会看到一个自己从没创建过的
    目录名。实测确认：`Path('E:/software/相册')` 正常，但
    `Path(drive)/'photos'` 对真实存在的 `Photos` 也成立。

    现在的做法是**枚举目录、拿文件系统里的真名字去比对**：
    比对用 `.lower()`（大小写不敏感），但采纳的是 `entry` 本身，
    所以大小写永远是磁盘上的真实形态。枚举的层数有界（每个盘符
    两层），实测本机 0.01 秒。
    """
    hits: list[tuple[Path, str]] = []
    seen_paths: set[str] = set()

    def _consider(entry: Path) -> bool:
        """名字匹配且含图片 → 采纳。返回是否已凑够 2 个。"""
        if entry.name.lower() not in _LIBRARY_DIR_NAMES:
            return False
        key = str(entry).lower()
        if key in seen_paths:
            return False
        seen_paths.add(key)
        if _has_any_image(entry):
            hits.append((entry, entry.name))
        return len(hits) >= 2

    for drive in _PROBE_ROOTS:
        base = Path(drive)
        if not base.is_dir():
            continue
        try:
            level1 = list(base.iterdir())
        except OSError:
            continue

        # ① 一层布局：<盘符>\<候选名>（D:\Photos）
        for entry in level1:
            if _consider(entry):
                return hits

        # ② 两层布局：<盘符>\<一级目录>\<候选名>（D:\Photos）
        #    一级目录里跳过系统目录（Program Files / Windows / …），
        #    否则每个盘符都会白枚举几十个与照片无关的目录。
        for parent in level1:
            if not _is_plausible_parent(parent):
                continue
            try:
                level2 = list(parent.iterdir())
            except OSError:
                continue
            for entry in level2:
                if _consider(entry):
                    return hits

    return hits


def _is_plausible_parent(entry: Path) -> bool:
    """一级目录是否值得再往下探一层。"""
    try:
        if not entry.is_dir():
            return False
    except OSError:
        return False
    lname = entry.name.lower()
    if lname.startswith(".") or lname.startswith("$"):
        return False
    return lname not in _PROBE_SKIP


def _has_any_image(folder: Path, *, limit: int = 400) -> bool:
    """目录里（含子目录）是否有可索引的图片。

    最多看 `limit` 个条目就放弃 —— 一个放着几万张照片的库
    在第 3 个文件时就该判定成功，不需要遍历完。
    """
    seen = 0
    try:
        for p in folder.rglob("*"):
            seen += 1
            if seen > limit:
                return False
            if p.is_file() and p.suffix.lower().lstrip(".") in ALLOWED_IMAGE_EXTS:
                return True
    except OSError:
        return False
    return False


__all__ = [
    "ALLOWED_IMAGE_EXTS", "APP_ROOT", "CONFIG_PATH", "Config", "ConfigError",
    "DATA_DIR", "DEFAULT_CONFIG", "DEFAULT_EXCLUDE_DIRS", "DEFAULT_TZ_OFFSET_MIN",
    "FUTURE_RAW_EXTS", "FUTURE_VIDEO_EXTS", "LOOPBACK_HOSTS", "ROOT_MODES",
    "RootConfig", "RootDisabledError", "SECRETS_PATH", "TMP_DIRNAME",
    "WILDCARD_HOSTS", "UnknownRootError",
    "bootstrap_default_root", "check_bind_safety", "ensure_dirs", "get",
    "is_first_run", "is_loopback_host", "load", "local_ipv4_addresses",
]
