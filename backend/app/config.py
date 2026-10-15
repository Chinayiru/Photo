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

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal, Mapping

# --------------------------------------------------------------------------
# 路径常量
# --------------------------------------------------------------------------

#: 仓库根 = 本文件的上两级（backend/app/config.py → backend/app → backend → 根）
APP_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
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
        "base_url": "",
        "enabled": False,
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

    def display(self) -> str:
        return self.label or self.id


class ConfigError(Exception):
    """配置文件无法使用。这类错误必须让用户看到原文（含路径），
    因为它唯一的修复方式就是用户去改那个文件——与 API 响应的脱敏规则不同。"""


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
        return self._roots

    @property
    def enabled_roots(self) -> list[RootConfig]:
        return [r for r in self.roots if r.enabled]

    def root_by_id(self, root_id: str | None) -> RootConfig:
        """按 id 取根。root_id 为空时返回第一个启用的根。"""
        if root_id is None:
            roots = self.enabled_roots
            if not roots:
                raise ConfigError("没有启用任何媒体根目录，请检查 config.json 的 roots")
            return roots[0]
        for r in self.roots:
            if r.id == root_id:
                return r
        raise ConfigError(f"未知的根目录 id: {root_id!r}")

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


def _merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """深合并：override 覆盖 base，嵌套 dict 递归合并，list/标量整体替换。

    为什么用深合并而不是整体替换：用户手写的 config.json 通常只写关心的几项
    （如只有 roots 和 port）。整体替换会让新增的默认键在升级后消失，
    导致 KeyError 或行为回退。
    """
    out: dict[str, Any] = dict(base)
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


def is_first_run() -> bool:
    """从未完成过配置（config.json 不存在或 roots 为空）。"""
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
        return True, "局域网可访问（需配对）"

    if not c.server.get("allow_lan", True):
        return False, f"server.allow_lan 为 false，不允许绑定 {host}"

    if _is_public_ip(h):
        return False, (
            f"{host} 是公网地址。本程序一期只做局域网访问，"
            "公网暴露请走二期云中继（不需要端口映射）。"
        )

    return True, f"绑定到 {host}（需确认该地址属于可信网段）"


def _is_public_ip(host: str) -> bool:
    """判断是否公网 IP。非 IP 字面量（主机名）一律当作公网处理 —— 保守优先。"""
    import ipaddress

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True         # 域名 → 可能解析到公网，保守拒绝
    return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved)


def local_ipv4_addresses() -> list[str]:
    """列出本机可用于局域网访问的 IPv4 地址（已过滤虚拟网卡网段）。

    为什么要过滤：本机实测有 5 个 IPv4，其中 26.26.192.40、192.168.64.1、
    192.168.226.1、192.168.32.1 来自 Hyper-V / WSL / VMware 虚拟网卡。
    把这些地址显示给用户会让手机连一个永远连不上的地址，
    而真正的局域网地址只有 192.0.2.10。过滤依据是这些虚拟网段的
    固定前缀（WSL 用 172.x，Hyper-V 默认用 172.x，VMware 用 192.168.x.1 的
    特定网关形态），无法穷举，因此采用"优先返回默认网关所在网段的地址"策略。
    """
    import socket

    found: list[str] = []
    # 主路由探测：连一个外部地址（不实际发包，UDP connect 只做路由选择）
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("223.5.5.5", 80))
        primary = probe.getsockname()[0]
        if primary and not primary.startswith("127."):
            found.append(primary)
    except OSError:
        pass
    finally:
        probe.close()

    # 补充：主机名解析出的其他地址（去重）
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addr = info[4][0]
            if addr.startswith("127.") or addr in found:
                continue
            if _looks_virtual(addr):
                continue
            found.append(addr)
    except OSError:
        pass
    return found


def _looks_virtual(addr: str) -> bool:
    """已知虚拟网卡网段的启发式过滤。

    Hyper-V 默认给虚拟交换机分配 172.16–172.31 段，WSL2 也用 172.x；
    VMware 的 host-only 网卡习惯用 x.x.x.1 且属于 192.168.0.0/16 的
    非默认网关段。这里只过滤确定性最高的两类，其余交给调用方展示给用户确认。
    """
    if addr.startswith("172."):
        try:
            second = int(addr.split(".")[1])
        except (ValueError, IndexError):
            return False
        return 16 <= second <= 31          # Hyper-V / WSL2 默认段
    return False


def bootstrap_default_root() -> Config:
    """首次运行时，用本机实际的照片库写出一份可用的 config.json。

    本机实测：D:\\Photos 含 3 个子目录（全部备份\\、相册1\\、青甘环线\\），
    4952 个文件，其中 4789 jpg / 68 png / 49 jpeg / 22 .jpg-lx2 / 12 mov / 7 webp
    / 3 mp4 / 2 gif。用户已确认此目录为媒体库，且允许程序写入
    （上传直接落库，归档为 年\\年-月\\）。
    """
    cfg = load()
    if cfg.roots:
        return cfg
    cfg.raw["roots"] = [
        {
            "id": "library",
            "path": r"D:\Photos",
            "label": "我的照片库",
            "mode": "readwrite",
            "enabled": True,
            "recursive": True,
        }
    ]
    cfg.save()
    load(reload=True)
    return get()


__all__ = [
    "ALLOWED_IMAGE_EXTS", "APP_ROOT", "CONFIG_PATH", "Config", "ConfigError",
    "DATA_DIR", "DEFAULT_CONFIG", "DEFAULT_EXCLUDE_DIRS", "DEFAULT_TZ_OFFSET_MIN",
    "FUTURE_RAW_EXTS", "FUTURE_VIDEO_EXTS", "LOOPBACK_HOSTS", "ROOT_MODES",
    "RootConfig", "SECRETS_PATH", "TMP_DIRNAME", "WILDCARD_HOSTS",
    "bootstrap_default_root", "check_bind_safety", "ensure_dirs", "get",
    "is_first_run", "is_loopback_host", "load", "local_ipv4_addresses",
]
