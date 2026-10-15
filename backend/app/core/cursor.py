"""游标分页的编解码。

## 为什么必须有 id 参与游标

`taken_at` **不唯一**：EXIF 只精确到秒，同一秒连拍两张很常见；更极端的是
`taken_src='mtime'` 时，从 SD 卡一次拷 200 张会让所有文件的 mtime 相同。
若游标只带 `taken_at`：

    页 1 返回 taken_at = T 的 100 条（共 150 条 taken_at = T）
    next_cursor = encode(T)
    页 2 的 SQL: WHERE taken_at < T     ← 剩下 50 条 taken_at = T 全部丢失！

加上 id 后 `(taken_at, id)` 构成**严格全序**，配合 `ORDER BY taken_at DESC, id DESC`
与复合索引 `idx_media_timeline(taken_at DESC, id DESC)` 完全对齐，既不漏也不重。

## 为什么不用 OFFSET

1. **分页漂移**：索引器在后台持续写入（首次索引 4915 个文件要 19 秒，期间用户
   已经在浏览）。用 OFFSET 的话新插入的行会把后面的行往后推，第 2 页会重复
   出现第 1 页的尾巴。
2. **删除是常态**：删一张照片会让下一页跳过一条。
3. **深分页代价**：`LIMIT 100 OFFSET 4800` 要让 B-tree 游标遍历并丢弃 4800 个
   索引项（约 144 KB 页面），而游标法是直接定位。

## 游标是"不透明"的

`next_cursor` 对客户端不可解析（接口文档 §4.2.1 ⑥）。这样将来若要改成
`f"{taken_at}|{id}|{index_generation}"` 做快照隔离，老客户端无需改动。
"""
from __future__ import annotations

import base64
import binascii

from ..errors import BadRequest

__all__ = ["encode_cursor", "decode_cursor", "MAX_CURSOR_LEN"]

#: 游标长度上限。合法游标约 40 字符；给足余量，但绝不能让客户端
#: 塞一个 10 MB 的字符串进来（解码要在请求路径上跑）。
MAX_CURSOR_LEN = 256

_SEP = "|"


def encode_cursor(taken_at: float, media_id: int) -> str:
    """把 (taken_at, id) 编码成不透明游标。

    用 `:.6f` 而不是 `repr()`：微秒精度足够区分任何真实照片，
    且定长格式让游标长度可预测（日志里好看，也便于测试断言）。
    """
    raw = f"{taken_at:.6f}{_SEP}{media_id}"
    return base64.urlsafe_b64encode(raw.encode("ascii")).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> tuple[float, int]:
    """解码游标。任何非法输入都抛 `BadRequest`（400 E1001）。

    ⚠️ 这里**必须**把所有异常都转成 400，不能让 ValueError 逃出去变成 500。
    游标来自客户端，它完全可以是任意字符串；500 会让前端以为"服务器坏了"
    而触发重试风暴，而正确行为是"刷新页面重新拿游标"。
    """
    if not cursor:
        raise BadRequest("cursor 不能为空")
    if len(cursor) > MAX_CURSOR_LEN:
        raise BadRequest(f"cursor 过长（上限 {MAX_CURSOR_LEN} 字符）")

    s = cursor.strip()
    # 还原被去掉的 base64 padding（urlsafe 变体同样用 '='）。
    pad = (-len(s)) % 4
    if pad:
        s += "=" * pad

    try:
        raw = base64.urlsafe_b64decode(s.encode("ascii")).decode("ascii")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise BadRequest("cursor 不是合法的 base64url") from None

    ts_s, sep, id_s = raw.partition(_SEP)
    if not sep:
        raise BadRequest("cursor 缺少分隔符")
    try:
        taken_at = float(ts_s)
        media_id = int(id_s)
    except ValueError:
        raise BadRequest("cursor 内容不是合法的数字") from None

    # 负 id 与 NaN/inf 会让 SQL 比较出现意外结果（NaN 与任何值比较都是 False，
    # 于是 WHERE 恒不成立，用户看到空白页却没有任何错误提示）。
    if media_id <= 0:
        raise BadRequest("cursor 中的 id 非法")
    if taken_at != taken_at or taken_at in (float("inf"), float("-inf")):
        raise BadRequest("cursor 中的时间戳非法")

    return taken_at, media_id
