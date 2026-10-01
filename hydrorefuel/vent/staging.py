"""两段放散状态机：先大流量、后小流量，顺序与时序写死。

状态只沿 ``IDLE → STAGE1 → SWITCHING → STAGE2 → COMPLETE`` 单向推进，
任何跳段、倒序都被拒绝；切段结果由"阀位反馈 + 压降速率带"双重确认，
失败必须看得出来。切段失败怎么收场按管路配置（``switch_policy``）：

* ``revert``：退回大流量段继续放，攒够余量再切一次，重试次数用尽才报警
  冻结——属于部分失败，可续放；
* ``hold``：停在当前段报警冻结，等人处理后续放。

冻结（``HOLD``）时阀门关到不漏（仿真里不再放散），检查点落盘，
:meth:`resume` 从断点继续；已经放掉的段量不重算、不漏记。每段结束都做
"流量计账 vs 压力折算账"核对，段量对不上同样进 ``HOLD`` 报警，排查后用
:meth:`recheck` 重新核对，账平了才允许续放。

段量口径：流量计内部按"真值"记账，现场偏差与重标定只是显示系数，因此
检查点只带真值与系数，恢复时 ``真值 × 系数`` 重建现场账，标定修正永远
只作用一次，不会因为反复恢复被重复缩放。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import TruckLimits, VentLimits
from ..errors import InterlockError
from .meter import VentFlowMeter
from .valve import VentValve

IDLE = "idle"
STAGE1 = "stage1_large"
SWITCH_STATE = "switching"
STAGE2 = "stage2_small"
HOLD = "hold"
COMPLETE = "complete"

POLICY_REVERT = "revert"
POLICY_HOLD = "hold"

# 切段失败原因码（同时写进审计，事后逐条可追）。
REASON_NONE = ""
REASON_POSITION = "position_feedback_mismatch"
REASON_RATE_HIGH = "rate_still_large"
REASON_RATE_NONE = "no_flow_after_switch"
REASON_RECONCILE = "stage_reconcile_mismatch"
REASON_VENT_TIMEOUT = "vent_timeout"


@dataclass(frozen=True)
class StageAmount:
    """一段放散的两套账：流量计实测（显示读数）与压力折算。"""

    stage: int
    meter_kg: float
    pressure_kg: float
    vented_mpa: float
    matched: bool
    discrepancy_ratio: float

    def as_dict(self) -> dict:
        return {
            "stage": self.stage,
            "meter_kg": round(self.meter_kg, 3),
            "pressure_kg": round(self.pressure_kg, 3),
            "vented_mpa": round(self.vented_mpa, 3),
            "matched": self.matched,
            "discrepancy_ratio": round(self.discrepancy_ratio, 4),
        }


@dataclass(frozen=True)
class VentCheckpoint:
    """可续放检查点：段状态、真值段量、失败原因、现场系数都要带得走。"""

    state: str
    pressure_mpa: float
    stage1: StageAmount | None
    stage2: StageAmount | None
    stage1_open_true_kg: float
    stage1_open_pressure_mpa: float
    stage2_open_true_kg: float
    stage2_open_pressure_mpa: float
    attempts: int
    stage1_elapsed_s: float
    stage2_elapsed_s: float
    switch_elapsed_s: float
    hold_reason: str
    policy: str
    meter_bias: float
    meter_cal_factor: float

    def as_dict(self) -> dict:
        return {
            "state": self.state,
            "pressure_mpa": round(self.pressure_mpa, 3),
            "stage1": None if self.stage1 is None else self.stage1.as_dict(),
            "stage2": None if self.stage2 is None else self.stage2.as_dict(),
            "stage1_open_true_kg": round(self.stage1_open_true_kg, 6),
            "stage1_open_pressure_mpa": round(self.stage1_open_pressure_mpa, 6),
            "stage2_open_true_kg": round(self.stage2_open_true_kg, 6),
            "stage2_open_pressure_mpa": round(self.stage2_open_pressure_mpa, 6),
            "attempts": self.attempts,
            "stage1_elapsed_s": round(self.stage1_elapsed_s, 3),
            "stage2_elapsed_s": round(self.stage2_elapsed_s, 3),
            "switch_elapsed_s": round(self.switch_elapsed_s, 3),
            "hold_reason": self.hold_reason,
            "policy": self.policy,
            "meter_bias": self.meter_bias,
            "meter_cal_factor": self.meter_cal_factor,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "VentCheckpoint":
        """从落盘字段还原检查点。"""

        def amount(key: str) -> StageAmount | None:
            data = raw.get(key)
            if not isinstance(data, dict):
                return None
            return StageAmount(
                stage=int(data["stage"]),
                meter_kg=float(data["meter_kg"]),
                pressure_kg=float(data["pressure_kg"]),
                vented_mpa=float(data["vented_mpa"]),
                matched=bool(data["matched"]),
                discrepancy_ratio=float(data["discrepancy_ratio"]),
            )

        return cls(
            state=str(raw["state"]),
            pressure_mpa=float(raw["pressure_mpa"]),
            stage1=amount("stage1"),
            stage2=amount("stage2"),
            stage1_open_true_kg=float(raw.get("stage1_open_true_kg", 0.0)),
            stage1_open_pressure_mpa=float(raw.get("stage1_open_pressure_mpa", 0.0)),
            stage2_open_true_kg=float(raw.get("stage2_open_true_kg", 0.0)),
            stage2_open_pressure_mpa=float(raw.get("stage2_open_pressure_mpa", 0.0)),
            attempts=int(raw.get("attempts", 0)),
            stage1_elapsed_s=float(raw.get("stage1_elapsed_s", 0.0)),
            stage2_elapsed_s=float(raw.get("stage2_elapsed_s", 0.0)),
            switch_elapsed_s=float(raw.get("switch_elapsed_s", 0.0)),
            hold_reason=str(raw.get("hold_reason", REASON_NONE)),
            policy=str(raw.get("policy", POLICY_REVERT)),
            meter_bias=float(raw.get("meter_bias", 0.0)),
            meter_cal_factor=float(raw.get("meter_cal_factor", 1.0)),
        )


class VentStaging:
    """两段放散编排与核对。"""

    def __init__(
        self,
        valve: VentValve,
        meter: VentFlowMeter,
        vent_limits: VentLimits,
        truck_limits: TruckLimits,
    ) -> None:
        self._valve = valve
        self._meter = meter
        self._limits = vent_limits
        self._truck = truck_limits
        self._state = IDLE
        self._pressure_mpa = 0.0
        self._stage1: StageAmount | None = None
        self._stage2: StageAmount | None = None
        # 各段"段内已放散"真值进度（相对段起点），续放据此累加。
        self._stage1_open_true_kg = 0.0
        self._stage1_open_pressure_mpa = 0.0
        self._stage2_open_true_kg = 0.0
        self._stage2_open_pressure_mpa = 0.0
        self._attempts = 0
        self._stage1_elapsed_s = 0.0
        self._stage2_elapsed_s = 0.0
        self._switch_elapsed_s = 0.0
        self._hold_reason = REASON_NONE
        self._last_switch_ok = False
        self._last_switch_reason = REASON_NONE

    # ---- 基本观测 -------------------------------------------------------

    @property
    def state(self) -> str:
        return self._state

    @property
    def pressure_mpa(self) -> float:
        return self._pressure_mpa

    @property
    def hold_reason(self) -> str:
        return self._hold_reason

    @property
    def last_switch_ok(self) -> bool:
        return self._last_switch_ok

    @property
    def last_switch_reason(self) -> str:
        return self._last_switch_reason

    @property
    def switch_attempts(self) -> int:
        return self._attempts

    def stage_amount(self, stage: int) -> StageAmount | None:
        """返回已结清段的段量（未结清为 None）。"""

        return self._stage1 if stage == 1 else self._stage2

    def is_hold(self) -> bool:
        """是否冻结等待续放。"""

        return self._state == HOLD

    def is_complete(self) -> bool:
        """是否两段都放完且核对通过。"""

        return self._state == COMPLETE

    def resumable(self) -> bool:
        """冻结态是否可续放（只有带着部分进度的冻结才可续）。"""

        return self._state == HOLD

    def reconciliation_ok(self) -> bool:
        """已结清段的账是否全部对得上。"""

        return all(amount.matched for amount in (self._stage1, self._stage2) if amount is not None)

    def reconcile_report(self) -> dict:
        """段量核对汇总，供控制台展示与审计落盘。"""

        total_meter = self._meter.total_kg()
        total_pressure = self._truck.vent_mass_kg(self._valve.vented_mpa())
        if total_pressure > 0.0:
            total_ratio = abs(total_meter - total_pressure) / total_pressure
        else:
            total_ratio = 0.0 if total_meter == 0.0 else 1.0
        return {
            "stage1": None if self._stage1 is None else self._stage1.as_dict(),
            "stage2": None if self._stage2 is None else self._stage2.as_dict(),
            "total_meter_kg": round(total_meter, 3),
            "total_pressure_kg": round(total_pressure, 3),
            "total_matched": total_ratio <= self._limits.reconcile_tolerance,
            "total_discrepancy_ratio": round(total_ratio, 4),
        }

    # ---- 运行 -----------------------------------------------------------

    def start(self, pressure_mpa: float) -> None:
        """开阀进入大流量段；只允许从空闲态发起，不能跳过任何一段。"""

        if self._state != IDLE:
            raise InterlockError("放散已经开始，不能重复发起", component="vent")
        pressure = float(pressure_mpa)
        if pressure <= 0.0:
            raise InterlockError("放散起始压力必须为正", component="vent")
        self._valve.open()
        self._pressure_mpa = pressure
        self._state = STAGE1
        self._attempts = 0
        self._stage1_elapsed_s = 0.0
        self._stage2_elapsed_s = 0.0
        self._last_switch_ok = False
        self._last_switch_reason = REASON_NONE

    def tick(self, dt: float) -> None:
        """推进一个步长。

        大流量段放到切段压力就发起切段；切段确认通过才进小流量收尾段，
        小流量段一直放到拔管确认压力。任一段超时或核对失败进冻结。
        """

        if dt <= 0:
            raise ValueError("步长必须为正")
        if self._state in (IDLE, COMPLETE, HOLD):
            return
        # 每段单独计时：大流量段快、小流量段慢，不能共用一个超时。
        if self._state in (STAGE1, SWITCH_STATE):
            self._stage1_elapsed_s += dt
            if self._stage1_elapsed_s > self._limits.stage1_timeout_s:
                self._freeze(REASON_VENT_TIMEOUT)
                return
        if self._state == STAGE2:
            self._stage2_elapsed_s += dt
            if self._stage2_elapsed_s > self._limits.stage2_timeout_s:
                self._freeze(REASON_VENT_TIMEOUT)
                return
        if self._state == STAGE1:
            self._tick_stage1(dt)
        elif self._state == SWITCH_STATE:
            self._tick_switching(dt)
        elif self._state == STAGE2:
            self._tick_stage2(dt)

    def _tick_stage1(self, dt: float) -> None:
        # 已经低到确认压力以下：直接按当前段结清收尾，不切空段。
        if self._pressure_mpa <= self._truck.vent_confirm_mpa:
            self._close_stage(1)
            if self._state != HOLD:
                self._state = COMPLETE
            return
        if self._pressure_mpa <= self._limits.switch_mpa:
            self._begin_switch()
            return
        self._bleed(1, dt)

    def _tick_stage2(self, dt: float) -> None:
        if self._pressure_mpa <= self._truck.vent_confirm_mpa:
            self._close_stage(2)
            if self._state != HOLD:
                self._state = COMPLETE
            return
        self._bleed(2, dt)

    def _bleed(self, stage: int, dt: float) -> None:
        before = self._pressure_mpa
        after = self._valve.bleed(before, dt)
        drop = before - after
        true_mass = self._truck.vent_mass_kg(drop)
        self._meter.commit(true_mass, stage)
        self._pressure_mpa = after
        # 段内真值进度按"该段真值累计 − 段起点"记，切段退回不影响二段。
        if stage == 1:
            self._stage1_open_true_kg = self._meter.stage_true_kg(1)
            self._stage1_open_pressure_mpa = self._valve.vented_mpa(1)
        else:
            self._stage2_open_true_kg = self._meter.stage_true_kg(2)
            self._stage2_open_pressure_mpa = self._valve.vented_mpa(2)

    # ---- 切段 -----------------------------------------------------------

    def _begin_switch(self) -> None:
        self._valve.switch_to_small()
        self._state = SWITCH_STATE
        self._switch_elapsed_s = 0.0
        self._attempts += 1
        self._last_switch_ok = False
        self._last_switch_reason = REASON_NONE

    def _tick_switching(self, dt: float) -> None:
        # 切段窗口内阀位走完并稳定，窗口结束一次性判定反馈。
        self._switch_elapsed_s += dt
        if self._switch_elapsed_s < self._limits.switch_timeout_s:
            return
        self._valve.settle_switch()
        reason = self._check_switch_feedback()
        if reason == REASON_NONE:
            # 切段确认成功：冻结一段真值边界，二段从零起算。
            self._meter.close_stage1()
            self._valve.close_stage1()
            self._close_stage(1)
            if self._state == HOLD:
                return
            self._last_switch_ok = True
            self._stage2_open_true_kg = 0.0
            self._stage2_open_pressure_mpa = 0.0
            self._state = STAGE2
            return
        self._last_switch_ok = False
        self._last_switch_reason = reason
        self._handle_switch_failure(reason)

    def _check_switch_feedback(self) -> str:
        """阀位反馈 + 速率带双重确认切段是否真的成功。

        ``settle_switch`` 后阀门位置已经按反馈定档，这里再用"该档位实际
        压降速率是否落在应有速率带内"做二次确认：小阀开了但速率仍是大
        流量档（大阀没关死）、或位置到位却没有流量（小阀其实没开），都
        算切段没成功。
        """

        position = self._valve.position()
        observed = self._valve.bleed_rate_mpa_per_s(self._pressure_mpa)
        # settle 成功才会落到 small；仍在 switching 说明位置反馈不到位。
        if position != "small":
            if self._valve.is_open():
                return REASON_RATE_HIGH
            return REASON_POSITION
        expected_small = self._valve.expected_rate_mpa_per_s(self._pressure_mpa, small=True)
        expected_large = self._valve.expected_rate_mpa_per_s(self._pressure_mpa, small=False)
        # 小阀位反馈到位、流量却仍是大流量档：大阀没关死。
        if abs(observed - expected_large) <= self._limits.rate_band * max(expected_large, 1e-9):
            return REASON_RATE_HIGH
        # 小阀位反馈到位却没有小流量：切段后根本不放散。
        if abs(observed - expected_small) > self._limits.rate_band * max(expected_small, 1e-9):
            return REASON_RATE_NONE
        return REASON_NONE

    def _handle_switch_failure(self, reason: str) -> None:
        policy = self._limits.switch_policy
        can_retry = self._attempts < self._limits.switch_max_attempts
        if policy == POLICY_REVERT:
            # 退回大流量段：settle 后阀可能仍在切换中（小阀没开）也可能已经
            # 被反馈定在大流量位（大阀没关死），两种情况都归位到大流量位。
            if self._valve.is_open():
                if self._valve.position() == "switching":
                    self._valve.reopen_large()
            else:
                self._valve.open()
            if can_retry:
                self._state = STAGE1
                return
        self._freeze(reason)

    # ---- 段结清与核对 ----------------------------------------------------

    def _build_amount(self, stage: int) -> StageAmount:
        vented_mpa = self._valve.vented_mpa(stage)
        meter_kg = self._meter.stage_kg(stage)
        pressure_kg = self._truck.vent_mass_kg(vented_mpa)
        if pressure_kg > 0.0:
            ratio = abs(meter_kg - pressure_kg) / pressure_kg
        else:
            ratio = 0.0 if meter_kg == 0.0 else 1.0
        return StageAmount(
            stage=stage,
            meter_kg=meter_kg,
            pressure_kg=pressure_kg,
            vented_mpa=vented_mpa,
            matched=ratio <= self._limits.reconcile_tolerance,
            discrepancy_ratio=ratio,
        )

    def _close_stage(self, stage: int) -> StageAmount:
        """结清一段：按阀门/流量计当前段累计取账，对不上则冻结。"""

        amount = self._build_amount(stage)
        if stage == 1:
            self._stage1 = amount
        else:
            self._stage2 = amount
        if not amount.matched:
            self._freeze(REASON_RECONCILE)
        return amount

    def apply_meter_calibration(self, factor: float) -> None:
        """冻结排查时按系数重新标定放散流量计（真值不动，只改显示系数）。"""

        if factor <= 0.0:
            raise ValueError("标定系数必须为正")
        self._meter.calibrate(factor)

    def recheck(self) -> bool:
        """冻结排查后按当前两套账重新核对各已结清段，返回是否全部对上。

        流量计复位、偏差消除后段量会重新对上；账没平之前 :meth:`resume`
        仍然拒绝，防止"对不上也接着放"。
        """

        if self._state != HOLD:
            raise InterlockError("只有冻结态需要重新核对", component="vent")
        # 按当前两套账重算所有已结清/已判定的段；现场标定修好后自然对上。
        if self._stage1 is not None:
            self._stage1 = self._build_amount(1)
        if self._stage2 is not None:
            self._stage2 = self._build_amount(2)
        return self.reconciliation_ok()

    # ---- 冻结与续放 ------------------------------------------------------

    def _freeze(self, reason: str) -> None:
        self._state = HOLD
        self._hold_reason = reason
        # 冻结即关阀，绝不允许"报警了还在大流量往外排"。
        if self._valve.is_open():
            self._valve.close()

    def resume(self) -> str:
        """从冻结点续放，返回续放后的状态；段量不重算、不跳段。"""

        if self._state != HOLD:
            raise InterlockError("只有冻结的放散才能续放", component="vent")
        if self._hold_reason == REASON_RECONCILE and not self.reconciliation_ok():
            raise InterlockError(
                "放散段量仍对不上，先重新核对（recheck）排查流量计/阀门",
                component="vent",
            )
        self._hold_reason = REASON_NONE
        self._switch_elapsed_s = 0.0
        # 续放不能跳段：一段未结清开在大流量位；一段边界已冻结则直接开在
        # 小流量位把收尾段接着放完（不是跳段，是二段本身的续放）。
        in_stage2 = self._meter.stage1_closed_kg() is not None
        self._valve.open(small=in_stage2)
        self._state = STAGE2 if in_stage2 else STAGE1
        return self._state

    def finish(self) -> None:
        """两段放散完成后关阀收尾；未完成直接拒绝。"""

        if self._state != COMPLETE:
            raise InterlockError("放散未完成，不能收尾关阀", component="vent")
        if self._valve.is_open():
            self._valve.close()
        self._state = IDLE

    # ---- 检查点 ----------------------------------------------------------

    def checkpoint(self) -> VentCheckpoint:
        """导出可落盘的续放检查点（带真值账与现场系数）。"""

        return VentCheckpoint(
            state=self._state,
            pressure_mpa=self._pressure_mpa,
            stage1=self._stage1,
            stage2=self._stage2,
            stage1_open_true_kg=self._stage1_open_true_kg,
            stage1_open_pressure_mpa=self._stage1_open_pressure_mpa,
            stage2_open_true_kg=self._stage2_open_true_kg,
            stage2_open_pressure_mpa=self._stage2_open_pressure_mpa,
            attempts=self._attempts,
            stage1_elapsed_s=self._stage1_elapsed_s,
            stage2_elapsed_s=self._stage2_elapsed_s,
            switch_elapsed_s=self._switch_elapsed_s,
            hold_reason=self._hold_reason,
            policy=self._limits.switch_policy,
            meter_bias=self._meter.bias(),
            meter_cal_factor=self._meter.cal_factor(),
        )

    def restore(self, checkpoint: VentCheckpoint) -> None:
        """按检查点重建状态机与各组件账册（跨重启续放用）。"""

        self._state = checkpoint.state
        self._pressure_mpa = checkpoint.pressure_mpa
        self._stage1 = checkpoint.stage1
        self._stage2 = checkpoint.stage2
        self._stage1_open_true_kg = checkpoint.stage1_open_true_kg
        self._stage1_open_pressure_mpa = checkpoint.stage1_open_pressure_mpa
        self._stage2_open_true_kg = checkpoint.stage2_open_true_kg
        self._stage2_open_pressure_mpa = checkpoint.stage2_open_pressure_mpa
        self._attempts = checkpoint.attempts
        self._stage1_elapsed_s = checkpoint.stage1_elapsed_s
        self._stage2_elapsed_s = checkpoint.stage2_elapsed_s
        self._switch_elapsed_s = checkpoint.switch_elapsed_s
        self._hold_reason = checkpoint.hold_reason
        stage1_closed = checkpoint.stage1 is not None
        stage1_mpa = checkpoint.stage1.vented_mpa if stage1_closed else checkpoint.stage1_open_pressure_mpa
        stage1_true = checkpoint.stage1_open_true_kg
        stage2_mpa = checkpoint.stage2_open_pressure_mpa
        stage2_true = checkpoint.stage2_open_true_kg
        # 冻结时阀一定是关的，恢复出来同样保持关阀，由 resume() 决定开在哪个位。
        self._valve.restore(
            open_large=False,
            stage1_mpa=stage1_mpa,
            stage2_mpa=stage2_mpa,
            stage1_closed=stage1_closed,
        )
        # 流量计按"真值 + 现场系数"重建：标定系数只在这里套一次。
        self._meter.restore(
            stage1_true,
            stage2_true,
            stage1_closed=stage1_closed,
            bias=checkpoint.meter_bias,
            cal_factor=checkpoint.meter_cal_factor,
        )
