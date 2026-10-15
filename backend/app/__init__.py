"""PhotoBridge 电脑端（Agent）包。

一期为**局域网直连**：手机与电脑在同一 Wi-Fi 下，手机直接访问
`http://<电脑局域网IP>:8787`。远程（云端中继）通过 `core/` 与
`transports/` 的抽象留好接口，二期接入时业务代码零改动。

分层：
  config / errors / models / db / sandbox      —— 地基，无网络依赖
  exif / thumbs / indexer / archive            —— 媒体处理
  core/                                        —— 协议无关的请求中间表示与路由
  transports/                                  —— 传输适配器（local_http 一期 / relay 二期）
  handlers/                                    —— 路由处理函数，只依赖 core
  server / main                                —— 组装与进程入口
"""

__version__ = "0.1.0"
