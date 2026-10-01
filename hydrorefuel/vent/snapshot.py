"""放散状态快照。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class VentSnapshot:
    """放散阀与两段放散过程某一时刻的状态。"""

    open: bool
    stage: str | None
    stage_order: tuple[str, ...]
    latch_engaged: bool
    latch_travel_mm: float
    latch_cycles: int
    latch_resets: int
    bursts: int
    switches: int
    switch_failures: int
    high_vented_mpa: float
    low_vented_mpa: float
    vented_mpa: float
    run_state: str
    switch_pressure_mpa: float
    failure_policy: str
    alarm: str | None

    def as_dict(self) -> dict:
        return {
            "open": self.open,
            "stage": self.stage,
            "stage_order": list(self.stage_order),
            "latch_engaged": self.latch_engaged,
            "latch_travel_mm": round(self.latch_travel_mm, 2),
            "latch_cycles": self.latch_cycles,
            "latch_resets": self.latch_resets,
            "bursts": self.bursts,
            "switches": self.switches,
            "switch_failures": self.switch_failures,
            "high_vented_mpa": round(self.high_vented_mpa, 3),
            "low_vented_mpa": round(self.low_vented_mpa, 3),
            "vented_mpa": round(self.vented_mpa, 3),
            "run_state": self.run_state,
            "switch_pressure_mpa": self.switch_pressure_mpa,
            "failure_policy": self.failure_policy,
            "alarm": self.alarm,
        }
