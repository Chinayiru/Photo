"""配置管理：data/config.json 的读写与默认值。"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

# backend/app/config.py -> D:\Photos
APP_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = APP_ROOT / "data"
CONFIG_PATH = DATA_DIR / "config.json"

IMAGE_EXTS = ["jpg", "jpeg", "png", "heic", "heif", "webp", "gif", "bmp", "tif", "tiff", "avif"]
RAW_EXTS = ["dng", "cr2", "cr3", "nef", "arw", "raf", "orf", "rw2", "pef", "srw"]
VIDEO_EXTS = ["mp4", "mov", "m4v", "avi", "mkv", "3gp", "wmv", "flv", "ts", "mts", "m2ts", "webm"]
ALL_EXTS = IMAGE_EXTS + RAW_EXTS + VIDEO_EXTS

MODE_READONLY = "readonly"
MODE_UPLOAD = "upload"
MODE_READWRITE = "readwrite"
VALID_MODES = (MODE_READONLY, MODE_UPLOAD, MODE_READWRITE)

DEFAULT_CONFIG: dict[str, Any] = {
    "app": {"name": "PhotoBridge", "version": "0.1.0", "first_run": True},
    "server": {
        "host": "0.0.0.0",
        "port": 8787,
        "https": {"enabled": False, "cert": "", "key": ""},
    },
    "roots": [
        {
            "id": "library",
            "label": "相册",
            "path": "D:\\Photos",
            "mode": MODE_READWRITE,
            "recursive": True,
            "include": list(ALL_EXTS),
            "exclude_dirs": [
                "@eaDir", "System Volume Information", "$RECYCLE.BIN",
                ".git", "node_modules", ".thumbnails", "#recycle", ".Trash",
            ],
        }
    ],
    "upload": {
        "root_id": "library",
        "subdir": "手机上传",
        "naming": "{YYYY}/{YYYY-MM}/{YYYYMMDD_HHMMSS}_{seq}{ext}",
        "dedup": "sha256",
        "max_file_gb": 32,
        "chunk_mb": 8,
        "session_ttl_hours": 24,
    },
    "thumbs": {"sizes": [320, 1600], "quality": 78, "workers": 4, "format": "WEBP"},
    "video": {
        "ffmpeg": "tools\\ffmpeg\\ffmpeg.exe",
        "native_exts": ["mp4", "m4v", "webm"],
        "transcode_on_demand": True,
        "crf": 23,
        "preset": "veryfast",
    },
    "auth": {
        "require_pairing": True,
        "token_ttl_days": 90,
        "allow_delete": True,
        "delete_to_recycle_bin": True,
        "allow_move": True,
        "allow_upload_to_library": True,
    },
    "index": {"scan_on_start": True, "watch": True, "hash_on_index": False, "batch_size": 500},
}

_lock = threading.RLock()
_cache: dict[str, Any] | None = None


def default_config() -> dict[str, Any]:
    return json.loads(json.dumps(DEFAULT_CONFIG))


def _merge_defaults(cfg: dict[str, Any]) -> dict[str, Any]:
    """把缺失的键用默认值补齐（不覆盖已有值），便于版本升级。"""
    base = default_config()

    def walk(dst: dict, src: dict) -> None:
        for k, v in src.items():
            if k not in dst:
                dst[k] = v
            elif isinstance(v, dict) and isinstance(dst.get(k), dict):
                walk(dst[k], v)

    walk(cfg, base)
    return cfg


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    thumbs_dir().mkdir(parents=True, exist_ok=True)
    tmp_dir().mkdir(parents=True, exist_ok=True)
    transcode_dir().mkdir(parents=True, exist_ok=True)


def load(force: bool = False) -> dict[str, Any]:
    global _cache
    with _lock:
        if _cache is not None and not force:
            return _cache
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if CONFIG_PATH.exists():
            try:
                cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            except Exception:
                cfg = default_config()
        else:
            cfg = default_config()
            CONFIG_PATH.write_text(
                json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        _cache = _merge_defaults(cfg)
        return _cache


def save(cfg: dict[str, Any]) -> None:
    global _cache
    with _lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(CONFIG_PATH)
        _cache = cfg


def get() -> dict[str, Any]:
    return load()


def update(patch: dict[str, Any]) -> dict[str, Any]:
    with _lock:
        cfg = load()

        def deep(dst: dict, src: dict) -> None:
            for k, v in src.items():
                if isinstance(v, dict) and isinstance(dst.get(k), dict):
                    deep(dst[k], v)
                else:
                    dst[k] = v

        deep(cfg, patch)
        save(cfg)
        return cfg


def roots() -> list[dict[str, Any]]:
    return load().get("roots", [])


def root_by_id(rid: str) -> dict[str, Any] | None:
    for r in roots():
        if r.get("id") == rid:
            return r
    return None


def default_root_id() -> str | None:
    up = load().get("upload", {})
    rid = up.get("root_id")
    if rid and root_by_id(rid):
        return rid
    rs = roots()
    return rs[0]["id"] if rs else None


def data_path(*parts: str) -> Path:
    p = DATA_DIR
    for x in parts:
        p = p / x
    return p


def thumbs_dir() -> Path:
    return DATA_DIR / "thumbs"


def tmp_dir() -> Path:
    return DATA_DIR / "uploads_tmp"


def transcode_dir() -> Path:
    return DATA_DIR / "transcode"


def is_first_run() -> bool:
    return bool(load().get("app", {}).get("first_run", True))


def mark_configured() -> None:
    update({"app": {"first_run": False}})
