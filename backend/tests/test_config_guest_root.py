"""游客根的配置契约（ISSUE-063 G4 / G6 / G8 / G9）。

## 这批用例在守什么

游客隔离**不是**前端不显示，而是"服务端只认一块目录"。落地方式是给根加一个
`visibility` 字段：

- 主人请求 → `ctx.allowed_root_ids = cfg.owner_root_ids`（不含游客根）；
- 游客请求 → `ctx.allowed_root_ids = cfg.guest_root_ids`（只有游客根）。

于是 `visibility` 一旦漏解析或配错，隔离就整个失效，而**功能看起来是好的**
（能看图、能上传）。这类"没有可见症状的失效"必须用配置层用例钉住：

1. 缺省必须是 `owner` —— 老配置文件里没有这个键，默认值错了等于把所有根
   都当成游客根，或者反过来把游客根暴露给主人。
2. 至多一个游客根 —— 两个游客根会让白名单同时放行两块目录。
3. 游客根不许 `readwrite` —— `can_modify` 只认 readwrite，配错就等于给了
   游客删除权（G6），而"能不能删"不会有人主动去试。
4. 停用的游客根必须当场报错 —— 它的症状是"游客点进来一片空白"，
   配置在启动时拦下来比事后排查便宜。
"""
from __future__ import annotations

import pytest

from backend.app.config import Config, ConfigError


def _cfg(tmp_path, roots: list[dict], *, app_mode: str = "readonly") -> Config:
    return Config(
        {"app": {"mode": app_mode}, "roots": roots},
        path=tmp_path / "config.json",
        data_dir=tmp_path / "data",
    )


def _root(tmp_path, rid: str, **extra) -> dict:
    d = tmp_path / rid
    d.mkdir(parents=True, exist_ok=True)
    base = {"id": rid, "path": str(d), "label": rid, "mode": "readonly"}
    base.update(extra)
    return base


# ---------------------------------------------------------------- 解析

def test_visibility_defaults_to_owner(tmp_path) -> None:
    cfg = _cfg(tmp_path, [_root(tmp_path, "library")])
    assert cfg.roots[0].visibility == "owner"


def test_guest_visibility_is_parsed(tmp_path) -> None:
    cfg = _cfg(tmp_path, [_root(tmp_path, "guest", visibility="guest",
                               mode="upload")])
    assert cfg.roots[0].visibility == "guest"


def test_visibility_is_case_insensitive_and_stripped(tmp_path) -> None:
    cfg = _cfg(tmp_path, [_root(tmp_path, "guest", visibility=" GUEST ",
                               mode="upload")])
    assert cfg.roots[0].visibility == "guest"


def test_unknown_visibility_is_rejected(tmp_path) -> None:
    cfg = _cfg(tmp_path, [_root(tmp_path, "g", visibility="visitor")])
    with pytest.raises(ConfigError, match="visibility"):
        _ = cfg.roots


# ---------------------------------------------------------------- 不变量

def test_only_one_guest_root_is_allowed(tmp_path) -> None:
    cfg = _cfg(tmp_path, [
        _root(tmp_path, "g1", visibility="guest", mode="upload"),
        _root(tmp_path, "g2", visibility="guest", mode="upload"),
    ])
    with pytest.raises(ConfigError, match="只允许配置一个"):
        _ = cfg.roots


def test_guest_root_may_not_be_readwrite(tmp_path) -> None:
    cfg = _cfg(tmp_path, [_root(tmp_path, "guest", visibility="guest",
                               mode="readwrite")])
    with pytest.raises(ConfigError, match="readwrite"):
        _ = cfg.roots


@pytest.mark.parametrize("mode", ["readonly", "upload"])
def test_guest_root_accepts_safe_modes(tmp_path, mode: str) -> None:
    cfg = _cfg(tmp_path, [_root(tmp_path, "guest", visibility="guest", mode=mode)])
    assert cfg.guest_root is not None
    assert cfg.guest_root.mode == mode


def test_disabled_guest_root_is_rejected(tmp_path) -> None:
    cfg = _cfg(tmp_path, [_root(tmp_path, "guest", visibility="guest",
                               mode="upload", enabled=False)])
    with pytest.raises(ConfigError, match="停用"):
        _ = cfg.roots


# ---------------------------------------------------------------- 访问器

def test_owner_root_ids_exclude_the_guest_root(tmp_path) -> None:
    cfg = _cfg(tmp_path, [
        _root(tmp_path, "library"),
        _root(tmp_path, "archive"),
        _root(tmp_path, "guest", visibility="guest", mode="upload"),
    ])
    assert cfg.owner_root_ids == ["library", "archive"]
    assert cfg.guest_root_ids == ["guest"]
    assert cfg.guest_root.id == "guest"


def test_owner_root_ids_keep_disabled_roots(tmp_path) -> None:
    """停用根仍在白名单里：停用是"取根 404"，不是"被过滤掉"。"""
    cfg = _cfg(tmp_path, [
        _root(tmp_path, "library"),
        _root(tmp_path, "old", enabled=False),
    ])
    assert cfg.owner_root_ids == ["library", "old"]
    assert [r.id for r in cfg.enabled_roots] == ["library"]


def test_without_guest_root_the_list_is_empty(tmp_path) -> None:
    cfg = _cfg(tmp_path, [_root(tmp_path, "library")])
    assert cfg.guest_root is None
    assert cfg.guest_root_ids == []
