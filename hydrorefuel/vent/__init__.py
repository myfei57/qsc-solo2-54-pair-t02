"""放散组件。

放散阀负责把软管与管路里的余压排到确认值以下；它有大、小两个流量位，段位
顺序固定为"先大后小"。:class:`~hydrorefuel.vent.staged.StagedVent` 把两段位、
切段核对、分段计量与对账做成状态机：切段没切成功按管路策略退回续放或关阀
报警，压力没放到位、分段账对不上时闩锁不释放、不允许拔管。
"""

from __future__ import annotations

from .latch import VentLatch
from .snapshot import VentSnapshot
from .staged import StagedVent
from .valve import VentValve

__all__ = ["VentValve", "VentLatch", "VentSnapshot", "StagedVent"]
