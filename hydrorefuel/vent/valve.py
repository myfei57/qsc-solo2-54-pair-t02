"""两段放散阀。

放散阀有关闭、大流量位（``high``）与小流量位（``low``）三种物理位，段位顺序
写死：先大流量排到切段压力，再切小流量收到拔管确认压力，不允许跳段或倒序。
压降不是瞬时的：:meth:`bleed` 按当前压力与所在段位的孔口系数算出这一步的压降，
压力越接近常压放散越慢。未开阀就调用放散会直接报错，避免审计里出现"没开阀却
压降了"的假记录。

切阀位由 :meth:`switch_to` 驱动，并返回阀位与实测流量速率，供编排核对"切段
到底有没有成功"。测试可用 :meth:`force_switch_jam` 模拟阀芯卡滞，此时指令让
切小流量，物理阀位仍停在大流量位——这正是现场必须能看出来的切段失败。
"""

from __future__ import annotations

import math

from ..config import VENT_STAGE_HIGH, VENT_STAGE_LOW, VENT_STAGE_ORDER, VentLimits
from ..errors import InterlockError


class VentValve:
    """两段放散阀的阀位切换与压降模型。"""

    def __init__(self, limits: VentLimits) -> None:
        self._limits = limits
        self._open = False
        self._stage: str | None = None
        self._bursts = 0
        self._switches = 0
        self._switch_failures = 0
        # 每个物理段位各记一个累计排量，保证两段排量事后能分别对得上。
        self._vented_mpa = {VENT_STAGE_HIGH: 0.0, VENT_STAGE_LOW: 0.0}
        self._switch_jammed = False

    @property
    def limits(self) -> VentLimits:
        return self._limits

    def open(self, stage: str = VENT_STAGE_HIGH) -> None:
        """把阀开到指定段位（默认大流量位，顺序固定）。"""

        if self._open:
            raise InterlockError("放散阀已经打开", component="vent")
        if stage not in VENT_STAGE_ORDER:
            raise InterlockError(f"未知放散段位 {stage!r}", component="vent")
        self._open = True
        self._stage = stage
        self._bursts += 1

    def close(self) -> None:
        """关闭放散阀。"""

        if not self._open:
            raise InterlockError("放散阀未打开，无需关闭", component="vent")
        self._open = False
        self._stage = None

    def is_open(self) -> bool:
        """放散阀是否处于打开状态。"""

        return self._open

    def stage(self) -> str | None:
        """返回当前物理阀位（关闭时为 None）。"""

        return self._stage

    def switch_to(self, stage: str) -> dict:
        """指令切到指定段位，返回阀位与实测速率核对结果。

        卡滞时物理阀位不动，但仍然如实返回实际阀位与实测速率，由编排判定
        切段失败；失败次数照样累计，供审计核对。
        """

        if not self._open:
            raise InterlockError("放散阀未打开，不能切段", component="vent")
        if stage not in VENT_STAGE_ORDER:
            raise InterlockError(f"未知放散段位 {stage!r}", component="vent")
        commanded = self._stage
        self._switches += 1
        switched = stage
        if self._switch_jammed:
            switched = commanded
            self._switch_failures += 1
        self._stage = switched
        return {
            "commanded": commanded,
            "requested": stage,
            "actual": switched,
            "switched": switched == stage,
        }

    def force_switch_jam(self, jammed: bool = True) -> None:
        """模拟阀芯卡滞（或清除卡滞），用于切段失败演练。"""

        self._switch_jammed = bool(jammed)

    def switch_jammed(self) -> bool:
        """当前是否处于切段卡滞状态。"""

        return self._switch_jammed

    def bleed_rate_mpa_per_s(self, pressure_mpa: float, stage: str | None = None) -> float:
        """返回指定段位在当前压力下的压降速率；缺省取当前物理阀位。"""

        actual_stage = self._stage if stage is None else stage
        if actual_stage is None:
            raise InterlockError("放散阀已关闭，没有阀位流量", component="vent")
        return self._limits.vent_rate_mpa_per_s(pressure_mpa, actual_stage)

    def bleed(self, pressure_mpa: float, dt: float) -> float:
        """按当前物理阀位推进一个步长并记账，返回放散后的压力。

        压降服从"速率与超压成正比"的一阶衰减，这里直接取指数闭式解，保证
        任意步长下阀表排量都等于真实压降（大步长不会因欧拉积分越过目标）。
        """

        if not self._open or self._stage is None:
            raise InterlockError("放散阀未打开，不能放散", component="vent")
        if dt <= 0:
            raise ValueError("步长必须为正")
        k = self._limits.rate_coeff * self._limits.orifice_mm(self._stage) / 100.0
        ambient = self._limits.ambient_mpa
        remaining = ambient + max(0.0, float(pressure_mpa) - ambient) * math.exp(-k * dt)
        self._vented_mpa[self._stage] += float(pressure_mpa) - remaining
        return remaining

    def stage_vented_mpa(self, stage: str) -> float:
        """返回指定段位累计放散掉的压力。"""

        return self._vented_mpa[stage]

    def vented_mpa(self) -> float:
        """返回两段合计累计放散掉的压力。"""

        return sum(self._vented_mpa.values())

    def bursts(self) -> int:
        """返回累计开阀次数。"""

        return self._bursts

    def switches(self) -> int:
        """返回累计切段指令次数。"""

        return self._switches

    def switch_failures(self) -> int:
        """返回累计切段失败（卡滞）次数。"""

        return self._switch_failures

    def reset(self) -> None:
        """复位阀位、分段排量与卡滞（交付前重新标定用）。"""

        self._open = False
        self._stage = None
        self._bursts = 0
        self._switches = 0
        self._switch_failures = 0
        self._vented_mpa = {VENT_STAGE_HIGH: 0.0, VENT_STAGE_LOW: 0.0}
        self._switch_jammed = False
