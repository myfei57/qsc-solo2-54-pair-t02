"""放散组件。

放散分两段，顺序定死为先大流量后小流量：:class:`VentValve` 提供大/小两个
开位与切段反馈，:class:`VentFlowMeter` 按段累计放散质量，
:class:`VentStaging` 编排"开阀 → 大流量放到切段压力 → 切段双重确认 →
小流量收尾 → 核对关阀"的状态机；机械闩锁保证放散过程中阀位不被压差顶回。
切段失败按管路策略退回大流量段或就地报警，任何部分失败都留检查点可续放。
"""

from __future__ import annotations

from .latch import VentLatch
from .meter import VentFlowMeter
from .snapshot import VentSnapshot
from .staging import (
    COMPLETE,
    HOLD,
    IDLE,
    POLICY_HOLD,
    POLICY_REVERT,
    STAGE1,
    STAGE2,
    SWITCH_STATE,
    StageAmount,
    VentCheckpoint,
    VentStaging,
)
from .valve import LARGE, SMALL, SWITCHING, SWITCH_FAULTS, VentValve

__all__ = [
    "VentValve",
    "VentLatch",
    "VentFlowMeter",
    "VentStaging",
    "VentSnapshot",
    "VentCheckpoint",
    "StageAmount",
    "LARGE",
    "SMALL",
    "SWITCHING",
    "SWITCH_FAULTS",
    "IDLE",
    "STAGE1",
    "STAGE2",
    "SWITCH_STATE",
    "HOLD",
    "COMPLETE",
    "POLICY_HOLD",
    "POLICY_REVERT",
]
