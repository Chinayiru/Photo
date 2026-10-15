"""共享包：云端与电脑端共用的协议定义。

为什么单独成包而不是两边各放一份：
设计文档 §2.3 要求 protocol.py「由云端与电脑端共享同一份文件」。
两份手工同步的文件必然漂移，而协议不一致会产生极难调试的"半通"故障
（docs/02 附录 A 的 Q-29）。因此这里采用**单一物理文件 + 双方 sys.path 引入**，
从根本上消除漂移可能，而不是靠哈希校验事后发现。
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

__all__ = ["REPO_ROOT"]
