# 本轮测试证据说明

基线：e9805a76f3fe29766a2d7876fd1a99bf13106326。所有结果来自本轮实际执行。

- `pytest-final.xml` 为环境就绪后的最终全量结果；`pytest-initial.xml` 保留构建未完成时的首轮失败。
- `*.result.json` 的 exit_code 是被测进程退出码；不等同于所有产品行为通过。例如 `e2e-lan-viewer-evidence` 仅断言失败可复现，产品结果见 `lan-viewer-evidence.json` 的 passed=false。
- `e2e-browser`、`e2e-browser-fresh`、`e2e-browser-functional-subset` 均非全通过。补充导航与显式排除 4n 只用于缩小测试工具问题，不修改产品、不把未测项算通过。
- `focused-qr` 的退出码0表示证据收集和前置断言成功；二维码自身 decoded=false，属于产品失败。
- 辅助 `*.py` 位于原 build 目录，记录了隔离与补充步骤；需把其 repo/output 参数换成复测环境，勿直接重跑注册表/固定目录清理类旧脚本。
- 只保存日志、结果、截图、辅助脚本、构建元数据；没有复制账号数据库、secrets.json、真实照片或测试媒体。
- 日志中的临时令牌、配对码与查询票据已作规则脱敏（替换 150 处）。原始证据仍在本机 build/final-validation-e9805a7-20261103，勿将其直接作为公开附件。
- `manifest.json` 给出各附件 SHA-256，可检查报告交付后是否发生变动。
- XML 按文件汇总共 101 个测试文件；统计与收集项见 `pytest-files.json`、`pytest-summary.json`。

## 脱敏说明（2026-11-04）

- 文本类证据里的内网地址、本机路径、公网 IP 已换成占位值（`192.0.2.10` / `203.0.113.10` / `D:\Photos` / `C:\Users\alice`）。
- 5 张 `.png` 截图**未入库**：里面是渲染出来的地址与入口二维码，二进制层没法脱敏，只在本地留存（已在 `.gitignore` 排除）。
