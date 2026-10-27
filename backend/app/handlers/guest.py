"""游客访问的接口（ISSUE-063 S3）。

## 六个端点，两种身份

主人（`role=owner`，必须有配对令牌）：
  * `POST   /api/guest/invites`      生成邀请链接（**唯一一次**能看到明文）
  * `GET    /api/guest/invites`      列出邀请及其状态
  * `DELETE /api/guest/invites/{id}` 撤销邀请（连带终止它的会话）
  * `GET    /api/guest/sessions`     现在谁在访问
  * `DELETE /api/guest/sessions/{client_id}` 结束某一位游客

游客（凭邀请码换令牌，**只能从云端入口**）：
  * `POST   /api/guest/redeem`       `{token}` → 会话令牌 + cookie

## 为什么 `redeem` 要单开一个公开端点

游客手里还没有任何凭证，而中间件对 `/api/**` 一律要求令牌
（`PUBLIC_PATHS` 是白名单）。把邀请码塞进 `Authorization` 也不行 ——
邀请码不是令牌，`looks_like_token()` 会先把它判成垃圾（那是审计 F03 的
改动，正确且必须保留）。所以：一个公开端点、一次兑换、拿到真令牌。
兑换成功后前端把令牌存起来（和主人令牌同样的用法），之后所有请求走
正常鉴权路径。

## 为什么游客上传不在这里

上传是"写"，权限面比读大得多，单独放在 S4（`handlers/uploads.py` 的
游客分支）：那里已经有分片、秒传、归档的完整实现，抄一份新的只会多
一处会漂移的逻辑。
"""
from __future__ import annotations

import logging
from typing import Any

from .. import audit, guest as guestmod
from ..core import Ctx, Reply, empty_reply, json_reply
from ..core.ctx import CLOUD
from ..errors import (E1020, E1026, E2011, AppError, BadRequest,
                      Unauthorized)
from ..auth import require_owner, token_device_claim

log = logging.getLogger(__name__)

__all__ = [
    "create_invite",
    "list_invites",
    "revoke_invite",
    "list_sessions",
    "kick_session",
    "redeem",
]


def _st(ctx: Ctx) -> Any:
    st = getattr(ctx, "app", None)
    if st is None:
        raise RuntimeError("Ctx.app 未装配：无法访问 AppState")
    return st


def _device_id(st: Any) -> str:
    """邀请绑定的是**云端**设备 id（链接里的 `/d/<id>/`）。

    与 `auth._verify_cloud_token` 用同一个函数取：两处口径不同会让
    "生成的链接指向 A、令牌却绑在 B"，症状是游客一进来就 401。
    """
    from ..tunnel import cloud_device_id

    return cloud_device_id(st.cfg, st.device_id)


def _public_base(cfg: Any, device_id: str) -> str:
    """`https://域名/d/<id>`，没配云端时返回空串。"""
    try:
        from ..tunnel import public_base_for

        return public_base_for(str(cfg.get("cloud.base_url", "") or ""), device_id)
    except Exception:  # noqa: BLE001 —— 生成链接失败不该 500
        log.debug("计算游客链接基址失败", exc_info=True)
        return ""


async def create_invite(ctx: Ctx) -> Reply:
    """`POST /api/guest/invites` —— 生成一条一次性邀请链接。

    请求体：`{"label": "老王", "minutes": 1440}`（分钟可省，缺省一天）。
    响应：`{"token", "link", "invite_id", "expires_at", "minutes"}`。

    ★ `link` 里的邀请码放在 **fragment**（`#guest=…`）：fragment 不会
      发给服务器、不会进反代日志、也不会出现在 `Referer` 里。查询串不行 ——
      那是我们自己在 ISSUE-062 里专门改掉的形态（配对码进 Caddy 日志）。
    """
    st = _st(ctx)
    me = require_owner(ctx)

    # ★ BUG-1：这里以前按"通路"一刀切 —— `ctx.via == CLOUD` 直接 403，
    #   而且用的是 `E2003 token_resource_mismatch`（"账号/设备/角色"三种
    #   不匹配共用一个码，用户看不出该找谁）。主人的实际路径是**扫云端
    #   二维码进"我的"页面**，于是这条规则把正常用法也挡了。
    #
    #   真正的边界分两层，都在这里和闸门里落实：
    #     1) 角色：`require_owner` 现在要求 `role == "owner"`（E2007）；
    #     2) 设备：凭证里的 `dev` 必须就是这台电脑（下面 E2008）。
    #   通路不再是判据 —— 云端与组网走的是同一套身份，拒绝谁只取决于
    #   他是谁、持有哪台电脑的凭证。
    body = await ctx.json(limit=4 * 1024)
    label = str(body.get("label") or "")[:64]
    minutes = body.get("minutes", 1440)

    device_id = _device_id(st)
    # ★ BUG-1：身份与设备必须同源。凭证自报的 `dev` 与这台电脑不一致时，
    #   拼出来的链接会指向**别人的**设备（扫码的人进不去，或者进到别人家）。
    #   凭证缺失时 `token_dev` 为空串，交给下面的 `device_id` 判据处理，
    #   不在这里凭空报错。
    token_dev = token_device_claim(ctx, st.db, st.cfg)
    if token_dev and device_id and token_dev != device_id:
        # 不把凭证里的设备 id 写进日志（`check_lint` 规则 4）：它由令牌
        # 派生，属于凭据取值。够用的事实是"两者不一致"。
        log.warning("生成邀请的设备不符：凭证声明的设备不是这台电脑")
        raise AppError(E2011, "这个凭证不属于这台电脑，请重新配对")
    if not device_id:
        # 没有云端设备 id 就没有"游客从哪儿进来"的地址。这不是用法问题，
        # 是配置问题：直接说清楚，别让用户对着"链接无效"猜。
        raise BadRequest(
            "还没有连上云端，无法生成游客链接：请先在电脑上完成云端配对")

    inv = guestmod.create_invite(
        st.db, device_id=device_id, label=label,
        minutes=guestmod.clamp_minutes(minutes),
        via="cloud" if ctx.via == CLOUD else "lan",
        # 未使用邀请的条数与同时在线人数共用同一个上限（B1/B3）。
        max_active=st.cfg.guest_max_online,
    )
    base = _public_base(st.cfg, device_id)
    if not base:
        # 没有云端基址就拼不出链接：这时发出去的邀请是**废纸** ——
        # 之前这里静默给 `link=""`，主人在面板上只看到一个空框，
        # 会以为功能坏了。当场说清楚该先做什么。
        raise BadRequest(
            "还没有连上云端，无法生成游客链接：请先在电脑上完成云端配对"
        )
    link = f"{base}/#guest={inv['token']}"
    audit.record(
        st.db, st.cfg, audit.ACTION_GUEST_INVITE,
        device=st.device_name, client_id=str(me.get("id") or ""),
        detail={"invite_id": inv["invite_id"]},
    )
    return json_reply({
        "invite_id": inv["invite_id"],
        "token": inv["token"],
        "link": link,
        "label": inv["label"],
        "minutes": inv["minutes"],
        "expires_at": inv["expires_at"],
    })


async def list_invites(ctx: Ctx) -> Reply:
    """`GET /api/guest/invites` —— 主人的邀请列表（**不含明文码**）。

    ★ 默认**不返回已撤销的邀请**（A 项）：撤销是终态、不可恢复，留着只会让
      页面越用越长。记录仍在库里与审计日志里（`guest.list_invites(...,
      include_revoked=True)` 可取）。
    """
    st = _st(ctx)
    require_owner(ctx)
    return json_reply({"items": guestmod.list_invites(st.db)})


async def revoke_invite(ctx: Ctx) -> Reply:
    """`DELETE /api/guest/invites/{invite_id}` —— 撤销一条邀请。

    ★ 连带终止它名下的会话（`guest.revoke_invite`）：「撤销」在用户心里
      就是"这个人不能再看了"，只让链接失效而会话继续跑到 2 小时结束，
      等于撤销按钮只关了一半。
    """
    st = _st(ctx)
    me = require_owner(ctx)
    invite_id = str(ctx.params.get("invite_id") or "")
    if not invite_id:
        raise BadRequest("缺少 invite_id")
    if not guestmod.revoke_invite(st.db, invite_id):
        raise NotFound(invite_id)
    audit.record(
        st.db, st.cfg, audit.ACTION_GUEST_REVOKE,
        device=st.device_name, client_id=str(me.get("id") or ""),
        detail={"invite_id": invite_id},
    )
    return empty_reply(204)


async def list_sessions(ctx: Ctx) -> Reply:
    """`GET /api/guest/sessions` —— 谁在访问（G13 的可见性一半）。"""
    st = _st(ctx)
    require_owner(ctx)
    return json_reply({"items": guestmod.list_sessions(st.db)})


async def kick_session(ctx: Ctx) -> Reply:
    """`DELETE /api/guest/sessions/{client_id}` —— 立刻结束某位游客。

    ★ 会话状态在库里，`auth._admit_guest` 每次请求都查一次
      （`guest.session_active`），所以这条 DELETE 是**即时生效**的，
      不依赖任何缓存失效。游客下一刻的请求就是 403 E1023。
    """
    st = _st(ctx)
    me = require_owner(ctx)
    client_id = str(ctx.params.get("client_id") or "")
    if not client_id:
        raise BadRequest("缺少 client_id")
    if not guestmod.kick_session(st.db, client_id):
        # ★ BUG-3：会话已撤销/已过期时原来回 404 not_found —— 用户点的是
        #   列表里的一行，看起来就是"按钮坏了"。改成 409 + 明确文案，
        #   前端据此刷新列表把它挪到"已退出"。
        raise AppError(E1026, "这位游客已经不在线了")
    audit.record(
        st.db, st.cfg, audit.ACTION_GUEST_REVOKE,
        device=st.device_name, client_id=str(me.get("id") or ""),
        detail={"guest_client_id": client_id},
    )
    return empty_reply(204)


async def redeem(ctx: Ctx) -> Reply:
    """`POST /api/guest/redeem` —— 邀请码换会话令牌（公开端点）。

    请求体 `{"token": "pbg1.…"}`；响应 `{"token", "expires_at",
    "invite_id", "label"}`，并下发 `pb_guest` cookie（`<img>`/`<video>`
    发不了自定义头，需要它）。

    ★ G2：只接受云端通路的请求。局域网通路也能到这里（同一个中间件、
      同一套路由），但邀请链接的用途就是"人不在家"，允许局域网兑换
      等于给"同一个 Wi-Fi 下的陌生人"开一条绕过配对码的路。
    """
    st = _st(ctx)
    if ctx.via != CLOUD:
        log.warning("游客兑换来自非云端通路 via=%s（已拒绝）", ctx.via)
        audit.record(
            st.db, st.cfg, audit.ACTION_GUEST_DENIED,
            device=st.device_name, detail={"via": str(ctx.via)})
        raise Unauthorized("游客只能从云端入口进入")

    body = await ctx.json(limit=guestmod.GUEST_BODY_LIMIT)
    token = str(body.get("token") or "").strip()
    if not token:
        raise BadRequest("缺少邀请码")
    if not token.startswith("pbg1."):
        # 形状不对直接拒，不进数据库（与审计 F03 同一个理由：垃圾输入
        # 不该产生一次查询，也不该在日志里留下 500）。
        raise AppError(E1020, "邀请链接无效，请向主人索取新的链接")

    sess = guestmod.redeem(
        st.db, token=token, device_id=_device_id(st), via="cloud",
        # 同时可访问的游客上限：判定在 guest.redeem 的 BEGIN IMMEDIATE 事务里。
        max_online=st.cfg.guest_max_online)
    audit.record(
        st.db, st.cfg, audit.ACTION_GUEST_ENTER,
        device=st.device_name, client_id=sess["client_id"],
        detail={"invite_id": sess["invite_id"]},
    )
    r = json_reply({
        "token": sess["token"],
        "client_id": sess["client_id"],
        "invite_id": sess["invite_id"],
        "label": sess["label"],
        "expires_at": sess["expires_at"],
        # ★ 前端要靠它写 `pb.device_id`（"连到了另一台电脑"的判定依据）。
        #   以前只回 token/label，前端就自己去猜，猜错就落一个空 id ——
        #   症状是游客每次刷新都被判成"连到了另一台电脑"。
        # ★ 必须是**本地** device_id（与 /api/system/ping 同一个值），
        #   不是云端那串 32 位。前端用它跟自己存的 pb.device_id 比对，
        #   填错了第一次探活就会判成“连到了另一台电脑”，把人踢回配对页。
        "device_id": str(getattr(st, "device_id", "") or ""),
        "device_name": str(getattr(st, "device_name", "") or ""),
    })
    ttl = max(int(sess["expires_at"] - ctx.started_at), 60)
    r.set_cookie(guestmod.GUEST_COOKIE, sess["token"], max_age=ttl)
    # 兑换响应**绝不能**被缓存：里面是刚签发的会话令牌。
    r.set_cache(0, private=True)
    return r
