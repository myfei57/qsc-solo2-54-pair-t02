"""两段放散编排内核。

一个放散过程的段位顺序写死为 ``high → low``：大流量段把压力快速排到切段压力，
切段核对成功后才允许进入小流量段，把余压慢慢收到拔管确认压力。任何一段都
不能跳过：没切成功就直接进小段、或在大段直接放到确认压力，都判失败并报警。

切段失败按管路配置两种处置：

* ``revert``：退回大段继续放散，下一拍再试切段，直到切成功；连续重试仍失败
  时转为 :data:`FAILED` 终态，宁可不拔管也不跳段。
* ``hold``：停在当前段、关阀、置 :data:`PAUSED` 并报警；排障清卡后由
  :meth:`resume` 续放。``PAUSED`` 属于部分失败，各段排量保留，续放接着记。

每个 :class:`StagedVent` 进程维护一份分段台账（起止压力、本段排量、切段尝试
结果）。放散到位后由 :meth:`reconcile` 对总账：两段都真实放散过、各段排量
之和等于总压降、切段发生在切段压力上——三者都对得上才算一致性通过，之后
编排才允许释放闩锁并拔管。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import (
    VENT_POLICY_HOLD,
    VENT_POLICY_REVERT,
    VENT_STAGE_HIGH,
    VENT_STAGE_LOW,
    VENT_STAGE_ORDER,
    VentLimits,
)
from ..errors import InterlockError
from .valve import VentValve

IDLE = "idle"
RUNNING = "running"
PAUSED = "paused"
READY = "ready"
DONE = "done"
FAILED = "failed"

# revert 策略下同一切段点连续失败这么多次就不再自愈，转终态报警。
MAX_SWITCH_ATTEMPTS = 3
# 步长缩算落在阈值上的浮点容差。
THRESHOLD_EPS = 1e-9


@dataclass
class StageLedger:
    """一个放散段位的计量台账。"""

    stage: str
    start_pressure_mpa: float
    end_pressure_mpa: float | None = None
    vented_mpa: float = 0.0
    switch_attempts: int = 0
    switch_failures: int = 0

    def as_dict(self) -> dict:
        return {
            "stage": self.stage,
            "start_pressure_mpa": round(self.start_pressure_mpa, 3),
            "end_pressure_mpa": round(self.end_pressure_mpa, 3) if self.end_pressure_mpa is not None else None,
            "vented_mpa": round(self.vented_mpa, 3),
            "switch_attempts": self.switch_attempts,
            "switch_failures": self.switch_failures,
        }


@dataclass
class SwitchAttempt:
    """一次切段指令的核对结果。"""

    requested: str
    actual: str
    pressure_mpa: float
    expected_rate: float
    actual_rate: float
    ok: bool
    policy: str

    def as_dict(self) -> dict:
        return {
            "requested": self.requested,
            "actual": self.actual,
            "pressure_mpa": round(self.pressure_mpa, 3),
            "expected_rate_mpa_per_s": round(self.expected_rate, 5),
            "actual_rate_mpa_per_s": round(self.actual_rate, 5),
            "ok": self.ok,
            "policy": self.policy,
        }


class StagedVent:
    """两段放散状态机：顺序、切段核对、分段计量与对账。"""

    def __init__(self, valve: VentValve, limits: VentLimits) -> None:
        self._valve = valve
        self._limits = limits
        self._state = IDLE
        self._stage: str | None = None
        self._run_id: str | None = None
        self._initial_pressure_mpa = 0.0
        self._last_pressure_mpa = 0.0
        self._ledgers: dict[str, StageLedger] = {}
        self._attempts: list[SwitchAttempt] = []
        self._meter_base: dict[str, float] = {}
        self._confirm_pressure = 0.0
        self._alarm: str | None = None

    @property
    def state(self) -> str:
        return self._state

    @property
    def stage(self) -> str | None:
        return self._stage

    @property
    def alarm(self) -> str | None:
        return self._alarm

    @property
    def run_id(self) -> str | None:
        return self._run_id

    def ledgers(self) -> list[StageLedger]:
        """按固定段位顺序返回分段台账。"""

        return [self._ledgers[stage] for stage in VENT_STAGE_ORDER if stage in self._ledgers]

    def switch_attempts(self) -> list[SwitchAttempt]:
        """返回本次放散全部切段尝试记录。"""

        return list(self._attempts)

    def is_paused(self) -> bool:
        """是否停在可续放的部分失败态。"""

        return self._state == PAUSED

    def begin(self, pressure_mpa: float, run_id: str, confirm_mpa: float | None = None) -> None:
        """开阀进入大流量段。顺序固定，起点只能是大段。"""

        if self._state not in (IDLE, DONE, FAILED):
            raise InterlockError("已有放散过程在进行，不能重新开段", component="vent")
        if confirm_mpa is None:
            raise InterlockError("必须给出放散确认压力，小流量段才有收尾目标", component="vent")
        if pressure_mpa <= self._limits.switch_pressure_mpa:
            raise InterlockError(
                f"起点压力 {pressure_mpa} 已不高于切段压力 {self._limits.switch_pressure_mpa}，"
                "两段放散无从起段",
                component="vent",
            )
        if confirm_mpa >= self._limits.switch_pressure_mpa:
            raise InterlockError("放散确认压力必须低于切段压力，小流量段才存在", component="vent")
        if confirm_mpa <= self._limits.ambient_mpa:
            raise InterlockError(
                f"放散确认压力 {confirm_mpa} 不高于常压 {self._limits.ambient_mpa}，物理上放不到",
                component="vent",
            )
        self._reset_run()
        self._run_id = str(run_id)
        self._confirm_pressure = float(confirm_mpa)
        self._initial_pressure_mpa = float(pressure_mpa)
        self._last_pressure_mpa = float(pressure_mpa)
        # 记录开段前的阀表底数，收尾时用"表底数差"与分段台账对一遍。
        self._meter_base = {
            stage: self._valve.stage_vented_mpa(stage) for stage in VENT_STAGE_ORDER
        }
        self._valve.open(VENT_STAGE_HIGH)
        self._stage = VENT_STAGE_HIGH
        self._ledgers[VENT_STAGE_HIGH] = StageLedger(VENT_STAGE_HIGH, float(pressure_mpa))
        self._state = RUNNING

    def step(self, pressure_mpa: float, dt: float) -> dict:
        """推进一个放散步长，返回本段排量、当前压力与本拍发生的切段尝试。

        到达切段点的那一拍会自动把步长缩到恰好落在切段压力上，因此大段台账
        的终点压力与阀表、管路实测压降三者天然一致，不靠事后钳位凑数。
        """

        if self._state != RUNNING or self._stage is None:
            raise InterlockError(f"当前放散状态 {self._state} 不接受续放步长", component="vent")
        if dt <= 0:
            raise ValueError("步长必须为正")
        pressure = float(pressure_mpa)
        attempt: SwitchAttempt | None = None
        threshold: float | None = None

        if self._stage == VENT_STAGE_HIGH:
            threshold = self._limits.switch_pressure_mpa
        elif pressure > self._confirm_pressure:
            # 小流量段最后一拍同样缩步长，恰好落在确认压力上收尾。
            threshold = self._confirm_pressure
        if self._stage == VENT_STAGE_HIGH and pressure <= self._limits.switch_pressure_mpa + THRESHOLD_EPS:
            # 已到切段点：每拍只做"切段指令 + 核对"，压力停在切段点不动，
            # 成功才进小流量；失败按管路策略退回续放或停段报警，绝不带着
            # 大流量位越过切段点继续放（那等于跳段）。
            attempt = self._try_switch(pressure)
            if attempt.ok:
                self._enter_low(pressure)
                return self._step_result(pressure, attempt)
            self._handle_switch_failure()
            return self._step_result(pressure, attempt)
        if threshold is not None:
            dt = self._shorten_dt_to_threshold(pressure, dt, threshold, str(self._stage))

        before = pressure
        pressure = self._valve.bleed(pressure, dt)
        self._account(before, pressure)
        if self._stage == VENT_STAGE_LOW and pressure <= self._confirm_pressure + THRESHOLD_EPS:
            # 余压已收到确认压力：小流量段停在收尾态，关阀与对账交给 complete()。
            pressure = self._confirm_pressure
            self._ledgers[VENT_STAGE_LOW].end_pressure_mpa = pressure
            self._last_pressure_mpa = pressure
            self._valve.close()
            self._state = READY
        return self._step_result(pressure, attempt)

    def resume(self, pressure_mpa: float) -> None:
        """清除卡滞后从部分失败态续放：重新开回大段并允许再试切段。"""

        if self._state != PAUSED or self._stage != VENT_STAGE_HIGH:
            raise InterlockError("只有停在大段的部分失败态才能续放", component="vent")
        if self._valve.switch_jammed():
            raise InterlockError("切段卡滞尚未排除，不能续放", component="vent")
        self._valve.open(VENT_STAGE_HIGH)
        self._last_pressure_mpa = float(pressure_mpa)
        self._state = RUNNING
        self._alarm = None

    def complete(self, pressure_mpa: float, confirm_mpa: float) -> dict:
        """放散到位后对账并置完成，返回对账结果；对不上就拒绝收尾。"""

        if self._state != READY or self._stage != VENT_STAGE_LOW:
            raise InterlockError(
                f"放散停在 {self._state}/{self._stage}，未把余压收到确认值，不能结束放散",
                component="vent",
            )
        if pressure_mpa > confirm_mpa + THRESHOLD_EPS:
            raise InterlockError(
                f"压力 {pressure_mpa:.3f} 尚未放到确认值 {confirm_mpa}，不能结束放散",
                component="vent",
            )
        report = self.reconcile(pressure_mpa, confirm_mpa)
        if not report["ok"]:
            self._fail("vent_reconcile_mismatch")
            raise InterlockError(
                f"放散对账未通过：{report['problems']}，保持闩锁禁止拔管",
                component="vent",
            )
        self._state = DONE
        return report

    def reconcile(self, final_pressure_mpa: float, confirm_mpa: float | None = None) -> dict:
        """对总账：两段都放过、各段排量之和等于总压降、切段边界对得上。"""

        problems: list[str] = []
        high = self._ledgers.get(VENT_STAGE_HIGH)
        low = self._ledgers.get(VENT_STAGE_LOW)
        if high is None or low is None:
            problems.append("missing_stage")
        if high is not None and high.vented_mpa <= 0.0:
            problems.append("high_stage_no_flow")
        if low is not None and low.vented_mpa <= 0.0:
            problems.append("low_stage_no_flow")
        # 切段只能在压力降到切段点（或更低）之后发生，且不能偷跑到确认压力以下。
        if high is not None and high.end_pressure_mpa is not None:
            if high.end_pressure_mpa > self._limits.switch_pressure_mpa + THRESHOLD_EPS:
                problems.append("switch_above_threshold")
            if confirm_mpa is not None and high.end_pressure_mpa <= confirm_mpa:
                problems.append("high_stage_reached_confirm")
        if low is not None and high is not None and high.end_pressure_mpa is not None:
            if abs(low.start_pressure_mpa - high.end_pressure_mpa) > THRESHOLD_EPS:
                problems.append("stage_boundary_mismatch")
            staged_total = high.vented_mpa + low.vented_mpa
            observed_total = self._initial_pressure_mpa - float(final_pressure_mpa)
            tolerance = max(1e-6, abs(observed_total) * 1e-6)
            if abs(staged_total - observed_total) > tolerance:
                problems.append("stage_total_mismatch")
        # 阀表分段底数差必须与分段台账一致，防止"账上有、表上没有"。
        meter_deltas = {
            stage: self._valve.stage_vented_mpa(stage) - self._meter_base.get(stage, 0.0)
            for stage in VENT_STAGE_ORDER
        }
        for stage in VENT_STAGE_ORDER:
            ledger_vented = (
                self._ledgers[stage].vented_mpa if stage in self._ledgers else 0.0
            )
            if abs(meter_deltas[stage] - ledger_vented) > 1e-6:
                problems.append(f"meter_mismatch_{stage}")
        return {
            "ok": not problems,
            "problems": problems,
            "initial_pressure_mpa": round(self._initial_pressure_mpa, 3),
            "final_pressure_mpa": round(float(final_pressure_mpa), 3),
            "switch_pressure_mpa": self._limits.switch_pressure_mpa,
            "confirm_mpa": confirm_mpa,
            "stage_order": list(VENT_STAGE_ORDER),
            "stages": [ledger.as_dict() for ledger in self.ledgers()],
            "meter_vented_mpa": {stage: round(value, 3) for stage, value in meter_deltas.items()},
            "switch_attempts": [attempt.as_dict() for attempt in self._attempts],
        }

    def snapshot(self) -> dict:
        """返回两段放散当前状态，供控制台展示与审计核对。"""

        return {
            "state": self._state,
            "stage": self._stage,
            "stage_order": list(VENT_STAGE_ORDER),
            "run_id": self._run_id,
            "initial_pressure_mpa": round(self._initial_pressure_mpa, 3),
            "last_pressure_mpa": round(self._last_pressure_mpa, 3),
            "switch_pressure_mpa": self._limits.switch_pressure_mpa,
            "confirm_required": True,
            "failure_policy": self._limits.switch_failure_policy,
            "alarm": self._alarm,
            "stages": [ledger.as_dict() for ledger in self.ledgers()],
            "switch_attempts": [attempt.as_dict() for attempt in self._attempts],
        }

    def reset(self) -> None:
        """复位放散过程（阀位由编排另行关闭）。"""

        self._reset_run()
        self._state = IDLE

    def _reset_run(self) -> None:
        self._stage = None
        self._run_id = None
        self._initial_pressure_mpa = 0.0
        self._last_pressure_mpa = 0.0
        self._ledgers = {}
        self._attempts = []
        self._meter_base = {}
        self._confirm_pressure = 0.0
        self._alarm = None

    def _account(self, before_mpa: float, after_mpa: float) -> None:
        """把一拍压降记入当前段位台账。"""

        ledger = self._ledgers[self._stage]
        ledger.vented_mpa += float(before_mpa) - float(after_mpa)
        ledger.end_pressure_mpa = float(after_mpa)
        self._last_pressure_mpa = float(after_mpa)

    def _shorten_dt_to_threshold(self, pressure: float, dt: float, threshold_mpa: float, stage: str) -> float:
        """若本拍会跨过阈值，把步长缩到恰好落在阈值上（指数闭式解）。"""

        if pressure <= threshold_mpa:
            return dt
        dt_to_threshold = self._limits.vent_time_s(pressure, threshold_mpa, stage)
        if 0.0 < dt_to_threshold < dt:
            return dt_to_threshold
        return dt

    def _try_switch(self, pressure: float) -> SwitchAttempt:
        """下切段指令并核对物理阀位与实测流量是否真的变成小流量。"""

        ledger = self._ledgers[VENT_STAGE_HIGH]
        ledger.switch_attempts += 1
        result = self._valve.switch_to(VENT_STAGE_LOW)
        expected_rate = self._limits.vent_rate_mpa_per_s(pressure, VENT_STAGE_LOW)
        actual_rate = self._valve.bleed_rate_mpa_per_s(pressure)
        # 阀位没动，或实测速率没按孔径比降下来，都算切段没成功。
        rate_ok = actual_rate <= expected_rate * (1.0 + self._limits.switch_rate_tolerance)
        ok = bool(result["switched"]) and rate_ok
        attempt = SwitchAttempt(
            requested=VENT_STAGE_LOW,
            actual=str(result["actual"]),
            pressure_mpa=pressure,
            expected_rate=expected_rate,
            actual_rate=actual_rate,
            ok=ok,
            policy=self._limits.switch_failure_policy,
        )
        self._attempts.append(attempt)
        if not ok:
            ledger.switch_failures += 1
        return attempt

    def _handle_switch_failure(self) -> None:
        """按管路策略处置切段失败。"""

        attempts = self._ledgers[VENT_STAGE_HIGH].switch_attempts
        if self._limits.switch_failure_policy == VENT_POLICY_HOLD:
            # 停在当前段（仍是大流量位），关阀报警，保留台账等待续放。
            self._valve.close()
            self._state = PAUSED
            self._alarm = "vent_switch_stuck_hold"
            return
        if attempts >= MAX_SWITCH_ATTEMPTS:
            self._valve.close()
            self._state = FAILED
            self._alarm = "vent_switch_stuck"
            return
        # revert：退回大段继续放散（物理阀位本来就没离开大段）。
        self._stage = VENT_STAGE_HIGH

    def _enter_low(self, pressure: float) -> None:
        self._stage = VENT_STAGE_LOW
        self._ledgers[VENT_STAGE_HIGH].end_pressure_mpa = pressure
        self._ledgers[VENT_STAGE_LOW] = StageLedger(VENT_STAGE_LOW, pressure)
        self._last_pressure_mpa = pressure

    def _step_result(self, pressure: float, attempt: SwitchAttempt | None) -> dict:
        return {
            "state": self._state,
            "stage": self._stage,
            "pressure_mpa": pressure,
            "vented_mpa": sum(ledger.vented_mpa for ledger in self._ledgers.values()),
            "switch_attempt": None if attempt is None else attempt.as_dict(),
            "alarm": self._alarm,
        }

    def _fail(self, alarm: str) -> None:
        if self._valve.is_open():
            self._valve.close()
        self._state = FAILED
        self._alarm = alarm
