"""放散状态快照。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class VentSnapshot:
    """两段放散某一时刻的状态。

    每段排量同时给出阀门压降账（``stage1/2_vented_mpa`` 与折算质量）和
    流量计账（``stage1/2_meter_kg``），控制台据此展示、审计据此核对。
    """

    open: bool
    position: str
    stage: int | None
    controller_state: str
    latch_engaged: bool
    latch_travel_mm: float
    latch_cycles: int
    latch_resets: int
    bursts: int
    switch_attempts: int
    vented_mpa: float
    stage1_vented_mpa: float
    stage2_vented_mpa: float
    stage1_meter_kg: float
    stage2_meter_kg: float
    meter_total_kg: float
    switch_ok: bool
    last_switch_reason: str
    reconciliation_ok: bool
    resumable: bool

    def as_dict(self) -> dict:
        return {
            "open": self.open,
            "position": self.position,
            "stage": self.stage,
            "controller_state": self.controller_state,
            "latch_engaged": self.latch_engaged,
            "latch_travel_mm": round(self.latch_travel_mm, 2),
            "latch_cycles": self.latch_cycles,
            "latch_resets": self.latch_resets,
            "bursts": self.bursts,
            "switch_attempts": self.switch_attempts,
            "vented_mpa": round(self.vented_mpa, 3),
            "stage1_vented_mpa": round(self.stage1_vented_mpa, 3),
            "stage2_vented_mpa": round(self.stage2_vented_mpa, 3),
            "stage1_meter_kg": round(self.stage1_meter_kg, 3),
            "stage2_meter_kg": round(self.stage2_meter_kg, 3),
            "meter_total_kg": round(self.meter_total_kg, 3),
            "switch_ok": self.switch_ok,
            "last_switch_reason": self.last_switch_reason,
            "reconciliation_ok": self.reconciliation_ok,
            "resumable": self.resumable,
        }
