"""云端安全原语：口令哈希、令牌生成、常量时间比较、配对码。

## 这里的每一条都对应一次真实事故

* **口令用 scrypt**，不是 sha256。加盐哈希的必要性不用解释，但"迭代
  次数取多少"是有答案的：`docs/06` §3.2 给的参数是 `n=2^14, r=8, p=1`，
  约 16 MB 内存 / 单次 ~50 ms。参数写死在代码里而不是配置里，是因为
  "允许配置降低强度"最终一定会被用来降低强度。
* **令牌只存哈希**。云端库被拖走时，攻击者拿到的是一堆 sha256，
  而不是能直接登录的字符串。
* **比较必须常量时间**。`==` 在第一个不同的字节就返回，理论上能按耗时
  猜出前缀。`hmac.compare_digest` 是标准做法。
* **配对码用 `secrets`，绝不用 `random`**。`random` 是梅森旋转，观察到
  624 个连续输出就能预测下一个——而配对码只需要 5 次尝试就能被锁定，
  但如果它能被预测，攻击者一次就够。`scripts/check_lint.py` 的规则 19
  在电脑端禁 `random`，云端这份同理（见 `cloud/tests/test_cloud_security.py`）。

## 为什么不引第三方库

`passlib`/`bcrypt`/`argon2-cffi` 都要编译扩展，会让 VPS 上的镜像多出
几百 MB 与一个必须匹配 glibc 的轮子。`hashlib.scrypt` 是标准库，
OpenSSL 提供实现，正是 `docs/06` 指定的算法。
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import string

__all__ = [
    "SCRYPT_N",
    "SCRYPT_R",
    "SCRYPT_P",
    "TOKEN_BYTES",
    "PAIR_CODE_DIGITS",
    "PAIR_CODE_TTL",
    "PAIR_MAX_TRIES",
    "PAIR_LOCK_SECONDS",
    "CLIENT_TOKEN_TTL_DAYS",
    "hash_password",
    "verify_password",
    "new_token",
    "hash_token",
    "tokens_equal",
    "new_device_id",
    "new_pair_code",
    "is_valid_pair_code",
    "new_session_id",
    "EMAIL_RE",
]

#: scrypt 参数（`docs/06` §3.2）。改大要同步改测试里的耗时预算。
SCRYPT_N: int = 2 ** 14
SCRYPT_R: int = 8
SCRYPT_P: int = 1
SCRYPT_DKLEN: int = 32
_SCRYPT_SALT_BYTES: int = 16

#: 令牌 32 字节 → 43 字符的 urlsafe base64（无填充）。
TOKEN_BYTES: int = 32

#: 配对码：6 位十进制。`docs/04` §3.3 规定 5 次尝试 / 10 分钟锁 / 300 秒有效。
PAIR_CODE_DIGITS: int = 6
PAIR_CODE_TTL: float = 300.0
PAIR_MAX_TRIES: int = 5
PAIR_LOCK_SECONDS: float = 600.0

#: 客户端令牌有效期（`docs/04` §1.4：90 天）。
CLIENT_TOKEN_TTL_DAYS: int = 90

_ALPHABET = frozenset(string.ascii_lowercase + string.digits + "._%+-")

#: 邮箱格式。刻意宽松：真正的校验是"发一封信"，不是正则。
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9.\-]{1,190}\.[A-Za-z]{2,24}$")


# ---------------------------------------------------------------------------
# 口令
# ---------------------------------------------------------------------------


def hash_password(password: str) -> str:
    """返回 `scrypt$n$r$p$<salt_b64>$<hash_b64>`。

    参数写进字符串里：以后调高 `SCRYPT_N` 时，老口令仍能按它自己那组
    参数验证，不需要一次全量重算。
    """
    if not isinstance(password, str) or not password:
        raise ValueError("口令不能为空")
    salt = secrets.token_bytes(_SCRYPT_SALT_BYTES)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                        n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
                        dklen=SCRYPT_DKLEN)
    return "scrypt${}${}${}${}${}".format(
        SCRYPT_N, SCRYPT_R, SCRYPT_P,
        _b64(salt), _b64(dk))


def verify_password(password: str, stored: str) -> bool:
    """常量时间校验。任何解析失败都返回 False（不抛，避免枚举 oracle）。"""
    if not isinstance(password, str) or not password:
        return False
    if not isinstance(stored, str) or not stored.startswith("scrypt$"):
        return False
    parts = stored.split("$")
    if len(parts) != 6:
        return False
    try:
        n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        salt = _unb64(parts[4])
        want = _unb64(parts[5])
    except (ValueError, TypeError):
        return False
    # 参数来自库，若被改成荒谬的值会造成 DoS（n=2^30 会吃光内存）。
    if not (2 ** 12 <= n <= 2 ** 18) or not (1 <= r <= 32) or not (1 <= p <= 8):
        return False
    try:
        got = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                             n=n, r=r, p=p, dklen=len(want) or SCRYPT_DKLEN)
    except (ValueError, MemoryError):
        return False
    return hmac.compare_digest(got, want)


# ---------------------------------------------------------------------------
# 令牌
# ---------------------------------------------------------------------------


def new_token() -> str:
    """新令牌（明文）。只在这一刻存在，之后库里只有 sha256。"""
    return secrets.token_urlsafe(TOKEN_BYTES)


def new_session_id() -> str:
    """隧道会话 id（只用于日志与 `/api/system/info`，不是凭据）。"""
    return secrets.token_hex(8)


def hash_token(token: str) -> str:
    """令牌的 sha256。

    用 sha256 而不是 scrypt：令牌是 32 字节的**高熵随机值**，没有"字典
    攻击"这回事，慢哈希只会让每个请求多花 50 ms（隧道每秒几十个请求）。
    口令不同——口令是人选的，熵低，必须慢哈希。
    """
    if not isinstance(token, str) or not token:
        return ""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def tokens_equal(a: str, b: str) -> bool:
    """常量时间比较两个字符串（用于与库里哈希比对前的短路场景）。"""
    return hmac.compare_digest((a or "").encode("utf-8"),
                               (b or "").encode("utf-8"))


# ---------------------------------------------------------------------------
# 标识与配对码
# ---------------------------------------------------------------------------


def new_device_id() -> str:
    """32 位 hex（`docs/05` §2.2 要求 `TEXT(32)`）。

    不用 uuid4().hex：那也是 32 hex 且随机性相同，但 `secrets` 让"这是
    凭据级随机"这件事在代码里显式可见——uuid 常被当成"只是个 id"。
    """
    return secrets.token_hex(16)


def new_pair_code() -> str:
    """6 位十进制配对码，**允许前导零**。

    `secrets.randbelow(10**6)` 而不是 `randint(100000, 999999)`：后者的
    空间只有 90 万且首位永远非零，等于白送攻击者一位信息。
    """
    return f"{secrets.randbelow(10 ** PAIR_CODE_DIGITS):0{PAIR_CODE_DIGITS}d}"


def is_valid_pair_code(code: object) -> bool:
    """格式检查：恰好 6 位 ASCII 数字。

    必须在查库**之前**判：`length(code)=6` 的 CHECK 约束会放过
    `"aaaaaa"`，而按字符串查库会让"格式错"和"码不存在"返回同一个错误，
    客户端无法区分"输错了"与"输入框里有空格"。
    """
    return (isinstance(code, str) and len(code) == PAIR_CODE_DIGITS
            and code.isascii() and code.isdigit())


def _b64(raw: bytes) -> str:
    import base64

    return base64.b64encode(raw).decode("ascii")


def _unb64(text: str) -> bytes:
    import base64

    return base64.b64decode(text.encode("ascii"), validate=True)
