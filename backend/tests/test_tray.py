"""托盘二维码（AC-5.2）。

## 这个测试防的是什么

AC-5.2 要求"托盘显示的二维码含 `device_id`"。原实现只生成
`/?code=123456`，于是**静态二维码 + 动态 IP** 会错配：

二维码是用户截图存着、或者打印出来贴在机箱上的；而局域网 IP 由
DHCP 分配，租期一到就可能变。隔几天再扫，同一个 IP 可能已经分给了
**另一台也跑着 PhotoBridge 的电脑** —— 手机会绑到别人的电脑上，
还会把对方的照片显示出来。这属于"看起来一切正常"的错误，
用户根本不会怀疑。

所以二维码必须带上这台电脑的 `device_id`，由前端在配对前核对。
"""
from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from backend.app.tray import pair_qr_url


def _qs(url: str) -> dict[str, list[str]]:
    return parse_qs(urlparse(url).query)


class TestPairQrUrl:
    """URL 形状。"""

    def test_includes_code(self) -> None:
        url = pair_qr_url("http://192.0.2.10:8787", "123456")
        assert _qs(url)["code"] == ["123456"]

    def test_includes_device_id(self) -> None:
        url = pair_qr_url("http://192.0.2.10:8787", "123456", "dev-abc")
        assert _qs(url)["did"] == ["dev-abc"]

    def test_without_device_id_is_still_valid(self) -> None:
        # 参数可选：老调用点、或设备表还没初始化时不该崩
        url = pair_qr_url("http://192.0.2.10:8787", "123456")
        assert "did" not in _qs(url)

    def test_empty_device_id_is_omitted(self) -> None:
        # 空串必须等价于"不传"。拼出 `&did=` 会让前端以为
        # 这是一台 device_id 为空的电脑，反而触发校验失败。
        url = pair_qr_url("http://192.0.2.10:8787", "123456", "")
        assert "did" not in _qs(url)

    def test_trailing_slash_does_not_double(self) -> None:
        # 托盘传进来的 base 有时带尾斜杠（`_lan_urls()` 的形态不固定）
        url = pair_qr_url("http://192.0.2.10:8787/", "123456", "dev-abc")
        assert url.startswith("http://192.0.2.10:8787/?code=")
        assert "//?" not in url

    def test_params_are_url_encoded(self) -> None:
        # device_id 是 `secrets.token_hex` 产出的十六进制，但配对码
        # 未来可能换成含特殊字符的形态；不编码的话 `&` 会把参数切断。
        url = pair_qr_url("http://192.0.2.10:8787", "12&34", "d&e=f")
        q = _qs(url)
        assert q["code"] == ["12&34"]
        assert q["did"] == ["d&e=f"]

    def test_url_stays_parseable(self) -> None:
        url = pair_qr_url("http://192.0.2.10:8787", "123456", "dev-abc")
        u = urlparse(url)
        assert u.scheme == "http"
        assert u.netloc == "192.0.2.10:8787"
        assert u.path == "/"
        assert u.fragment == ""      # 二维码 URL 不能带 fragment，扫码器常截断


class TestTrayWiring:
    """托盘真的把 `device_id` 传下去了吗。

    `pair_qr_url` 支持 `device_id` 不等于托盘**调用**时传了它 ——
    这正是这个仓库反复踩的坑：能力已实现、调用点没接上，
    验收标准却打着勾。所以这里直接对着源码断言，而不是只测纯函数。
    """

    def test_new_pair_code_passes_device_id(self) -> None:
        import inspect

        from backend.app import tray as tray_mod

        src = inspect.getsource(tray_mod.TrayController._new_pair_code)
        assert "pair_qr_url(base, code, st.device_id)" in src, (
            "托盘生成二维码时必须把 device_id 传进去，否则 AC-5.2 是空头的"
        )


class TestPairCodeIsDiscoverable:
    """首启时配对码必须自己出现，不能只藏在托盘右键菜单里。

    ## 这个测试防的是什么（用户实际反馈："配对码是隐藏的"）

    原实现里配对窗口**唯一**入口是托盘右键菜单「显示配对二维码」。
    而本程序没有主窗口（托盘常驻），Windows 又把新出现的托盘图标默认
    收进折叠区 —— 用户双击 exe 后看到的是"什么都没发生"，根本不知道
    要去哪里找那 6 位数字。首次使用就此卡死在第一步。

    所以这里断言的不是"窗口长什么样"，而是**启动流程里有没有一个
    主动弹出配对码的动作**，以及它是否只在首次（无已配对设备）时触发。
    """

    def test_on_ready_actually_calls_the_auto_show(self) -> None:
        import inspect

        from backend.app import tray as tray_mod

        src = inspect.getsource(tray_mod.TrayController.on_ready)
        assert "self._maybe_show_qr_on_start()" in src, (
            "on_ready 必须主动调用 _maybe_show_qr_on_start()，否则配对码"
            "只能从托盘右键菜单里找 —— 而用户根本看不见那个图标"
        )

    def test_auto_show_is_gated_on_having_no_device(self) -> None:
        import inspect

        from backend.app import tray as tray_mod

        src = inspect.getsource(tray_mod.TrayController._maybe_show_qr_on_start)
        assert "_has_paired_device()" in src, (
            "自动弹窗必须跳过已配对过的用户，否则每次重启都被窗口挡住"
        )

    def test_has_paired_device_ignores_revoked(self) -> None:
        import inspect

        from backend.app import tray as tray_mod

        src = inspect.getsource(tray_mod.TrayController._has_paired_device)
        # 被撤销的设备不算"配对过"：用户可能正是撤销了旧手机、要重新配。
        assert "revoked = 0" in src, (
            "查询已配对设备时必须过滤 revoked = 0，否则撤销后无法重新配对"
        )

    def test_auto_show_never_raises_into_on_ready(self) -> None:
        """弹窗失败绝不能把服务带崩。"""
        import inspect

        from backend.app import tray as tray_mod

        src = inspect.getsource(tray_mod.TrayController._maybe_show_qr_on_start)
        assert "except Exception:" in src
        assert "daemon=True" in src, (
            "必须用 daemon 线程 —— 否则关程序时会被这个线程挂住"
        )
