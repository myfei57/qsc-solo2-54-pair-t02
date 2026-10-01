"""卸氢顺序：软管连接 → 卸氢 → 两段泄压放散 → 对账 → 拔管。

卸氢先按卸氢速率把槽车压力降下来，再开放散阀两段泄压：大流量段快速排到
切段压力，核对切段成功后切小流量段慢慢收到拔管确认值。顺序写死、不能跳段，
每一段的起止压力与排量都单独记账，收尾时把"分段排量之和 == 总压降"连同
阀表底数差一起对一遍。对账不过、或压力没放到位，闩锁不释放、状态机拒绝
拔管，编排把这些情况转成违约项。

切段失败按管路配置处置：``revert`` 退回大段继续放散并重试；``hold`` 关阀并
置 ``vent_paused`` 部分失败，运行报告带 ``resumable``，排障后由
:func:`resume_unload_truck` 接着放——两段排量接着记，审计事件共用同一个
``run_id``，事后能逐段核对。
"""

from __future__ import annotations

from ..errors import HydroError, InterlockError
from ..truck.machine import TruckMachine
from ..vent.latch import VentLatch
from ..vent.staged import DONE, FAILED, IDLE, PAUSED, READY, RUNNING, StagedVent
from ..vent.valve import VentValve


def unload_truck(
    truck: TruckMachine,
    vent: VentValve,
    latch: VentLatch,
    staged: StagedVent,
    station: str,
    hose_pressure_mpa: float = 20.0,
    target_kg: float = 20.0,
    dt: float = 1.0,
    max_steps: int = 4000,
    run_id: str = "",
) -> dict:
    """执行一次卸氢，返回带步骤、分段放散账与违约项的报告。"""

    steps: list[dict] = []
    violations: list[str] = []
    try:
        truck.reset()
        pressure = truck.connect(hose_pressure_mpa)
        steps.append({"component": "truck", "action": "connect", "detail": {"pressure_mpa": round(pressure, 3)}})

        latch.engage()
        steps.append({"component": "vent", "action": "latch_engage", "detail": {"travel_mm": latch.travel()}})

        truck.open_unload()
        steps.append({"component": "truck", "action": "open_unload", "detail": {"state": truck.state}})

        for _ in range(max_steps):
            if truck.delivered_kg >= target_kg:
                break
            truck.transfer(dt)
        steps.append(
            {
                "component": "truck",
                "action": "transfer",
                "detail": {
                    "delivered_kg": round(truck.delivered_kg, 3),
                    "pressure_mpa": round(truck.pressure_mpa, 3),
                },
            }
        )
        if truck.delivered_kg + 1e-9 < target_kg:
            violations.append("unload_incomplete")

        truck.finish_unload()
        steps.append({"component": "truck", "action": "finish_unload", "detail": {"state": truck.state}})

        vent_run = _vent_to_confirm(truck, vent, staged, steps, dt, max_steps, run_id)
        violations.extend(vent_run["violations"])

        if staged.is_paused():
            # 部分失败：阀已关、闩锁保持，等排障后续放，不允许走到拔管。
            return {
                "station": station,
                "ok": False,
                "steps": steps,
                "violations": violations,
                "resumable": True,
                "run_id": staged.run_id,
                "vent": staged.snapshot(),
            }

        if staged.state == FAILED:
            return {
                "station": station,
                "ok": False,
                "steps": steps,
                "violations": violations,
                "resumable": False,
                "run_id": staged.run_id,
                "vent": staged.snapshot(),
            }

        # 小流量收到确认压力时阀已在对账前关闭，这里登记闭阀并释放闩锁。
        steps.append(
            {
                "component": "vent",
                "action": "close",
                "detail": {"open": vent.is_open(), "latch_engaged": latch.is_engaged()},
            }
        )
        latch.release()

        truck.disconnect()
        steps.append({"component": "truck", "action": "disconnect", "detail": {"state": truck.state}})
    except HydroError as error:
        payload = error.as_record()
        violations.append(f"{payload['component']}:{payload['type']}")
        steps.append({"component": payload["component"], "action": "rejected", "detail": {"message": payload["message"]}})

    return {
        "station": station,
        "ok": not violations,
        "steps": steps,
        "violations": violations,
        "resumable": False,
        "run_id": staged.run_id,
        "vent": staged.snapshot(),
    }


def resume_unload_truck(
    truck: TruckMachine,
    vent: VentValve,
    latch: VentLatch,
    staged: StagedVent,
    station: str,
    dt: float = 1.0,
    max_steps: int = 4000,
) -> dict:
    """从切段中断的部分失败态续放，接着把余压放到位后拔管。"""

    steps: list[dict] = []
    violations: list[str] = []
    try:
        if not staged.is_paused():
            raise InterlockError(
                f"放散当前处于 {staged.state} 态，没有可续放的中断",
                component="vent",
            )
        steps.append(
            {
                "component": "vent",
                "action": "vent_resume",
                "detail": {"run_id": staged.run_id, "stage": staged.stage, "pressure_mpa": round(truck.pressure_mpa, 3)},
            }
        )
        staged.resume(truck.pressure_mpa)
        vent_run = _vent_to_confirm(truck, vent, staged, steps, dt, max_steps, str(staged.run_id))
        violations.extend(vent_run["violations"])

        if staged.state in (PAUSED, FAILED):
            return {
                "station": station,
                "ok": False,
                "steps": steps,
                "violations": violations,
                "resumable": staged.state == PAUSED,
                "run_id": staged.run_id,
                "vent": staged.snapshot(),
            }

        # 小流量收到确认压力时阀已在对账前关闭，这里登记闭阀并释放闩锁。
        steps.append(
            {
                "component": "vent",
                "action": "close",
                "detail": {"open": vent.is_open(), "latch_engaged": latch.is_engaged()},
            }
        )
        latch.release()

        truck.disconnect()
        steps.append({"component": "truck", "action": "disconnect", "detail": {"state": truck.state}})
    except HydroError as error:
        payload = error.as_record()
        violations.append(f"{payload['component']}:{payload['type']}")
        steps.append({"component": payload["component"], "action": "rejected", "detail": {"message": payload["message"]}})

    return {
        "station": station,
        "ok": not violations,
        "steps": steps,
        "violations": violations,
        "resumable": staged.is_paused(),
        "run_id": staged.run_id,
        "vent": staged.snapshot(),
    }


def _vent_to_confirm(
    truck: TruckMachine,
    vent: VentValve,
    staged: StagedVent,
    steps: list[dict],
    dt: float,
    max_steps: int,
    run_id: str,
) -> dict:
    """驱动两段放散直到到位、暂停或失败，把分段事件追加进 steps。"""

    violations: list[str] = []
    if staged.state in (IDLE, DONE, FAILED):
        staged.begin(truck.pressure_mpa, run_id, confirm_mpa=truck.limits.vent_confirm_mpa)
        steps.append(
            {
                "component": "vent",
                "action": "vent_begin",
                "detail": {
                    "run_id": staged.run_id,
                    "stage": staged.stage,
                    "stage_order": "high>low",
                    "pressure_mpa": round(truck.pressure_mpa, 3),
                    "switch_pressure_mpa": staged.snapshot()["switch_pressure_mpa"],
                    "failure_policy": staged.snapshot()["failure_policy"],
                },
            }
        )

    steps_count = 0
    while staged.state == RUNNING:
        steps_count += 1
        outcome = staged.step(truck.pressure_mpa, dt)
        drop = truck.pressure_mpa - outcome["pressure_mpa"]
        if drop > 0.0:
            truck.bleed(drop)
        attempt = outcome["switch_attempt"]
        if attempt is not None:
            steps.append(
                {
                    "component": "vent",
                    "action": "vent_switch",
                    "severity": "notice" if attempt["ok"] else "warning",
                    "detail": {
                        "run_id": staged.run_id,
                        "requested": attempt["requested"],
                        "actual": attempt["actual"],
                        "pressure_mpa": attempt["pressure_mpa"],
                        "expected_rate_mpa_per_s": attempt["expected_rate_mpa_per_s"],
                        "actual_rate_mpa_per_s": attempt["actual_rate_mpa_per_s"],
                        "ok": attempt["ok"],
                        "policy": attempt["policy"],
                    },
                }
            )
        if outcome["state"] != RUNNING:
            break
        if truck.discharge_ready():
            break
        if steps_count >= max_steps:
            violations.append("vent_timeout")
            break

    if staged.state == RUNNING and not truck.discharge_ready():
        violations.append("vent_timeout")

    if staged.is_paused():
        violations.append("vent_switch_stuck_hold")
        steps.append(
            {
                "component": "vent",
                "action": "vent_paused",
                "severity": "alarm",
                "detail": {"run_id": staged.run_id, "stage": staged.stage, "alarm": staged.alarm},
            }
        )

    if staged.state == FAILED:
        violations.append(staged.alarm or "vent_failed")
        steps.append(
            {
                "component": "vent",
                "action": "vent_failed",
                "severity": "alarm",
                "detail": {"run_id": staged.run_id, "stage": staged.stage, "alarm": staged.alarm},
            }
        )

    if staged.state == READY and truck.discharge_ready():
        report = staged.complete(truck.pressure_mpa, truck.limits.vent_confirm_mpa)
        steps.append(
            {
                "component": "vent",
                "action": "vent_reconcile",
                "severity": "notice" if report["ok"] else "alarm",
                "detail": {
                    "run_id": staged.run_id,
                    "ok": report["ok"],
                    "problems": ",".join(report["problems"]),
                    "initial_pressure_mpa": report["initial_pressure_mpa"],
                    "final_pressure_mpa": report["final_pressure_mpa"],
                    "switch_pressure_mpa": report["switch_pressure_mpa"],
                    "high_vented_mpa": report["stages"][0]["vented_mpa"] if report["stages"] else 0.0,
                    "low_vented_mpa": report["stages"][1]["vented_mpa"] if len(report["stages"]) > 1 else 0.0,
                    "meter_high_vented_mpa": report["meter_vented_mpa"].get("high", 0.0),
                    "meter_low_vented_mpa": report["meter_vented_mpa"].get("low", 0.0),
                },
            }
        )
        steps.append(
            {
                "component": "vent",
                "action": "vent_hold",
                "detail": {
                    "run_id": staged.run_id,
                    "pressure_mpa": round(truck.pressure_mpa, 3),
                    "high_vented_mpa": report["stages"][0]["vented_mpa"] if report["stages"] else 0.0,
                    "low_vented_mpa": report["stages"][1]["vented_mpa"] if len(report["stages"]) > 1 else 0.0,
                    "ready": truck.discharge_ready(),
                },
            }
        )

    return {"violations": violations}
