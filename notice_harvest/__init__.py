"""消防科普公告的轮次式增量采集模块。

对外的入口是 :func:`collect_round`，它从本地来源站读取列表与详情，把
证据写入状态目录，并以来源编号为身份计算本轮的新增、修订、撤下和无法
确认变化。模块只依赖 Python 标准库。
"""
from .collector import HarvestError, collect_round

__all__ = ["collect_round", "HarvestError"]
