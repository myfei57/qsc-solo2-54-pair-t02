"""卸氢顺序：软管连接 → 卸氢 → 两段泄压放散 → 拔管。

卸氢先按卸氢速率把槽车压力降到卸氢结束阈值，再按"先大流量后小流量"两段
放散：大流量段放到切段压力，切段经阀位与速率带双重确认后切小流量收尾，
两段各自核对流量计账与压力账。压力没降到位、切段失败被冻结或段量对不上
时都不允许断开软管——编排把这些情况转成违约项，绝对不允许带压拔管。

切段中断属于部分失败：阀门关断、检查点落盘，:func:`resume_unload_truck`
从断点续放，已放掉的段量保留不重算。
"""

from __future__ import annotations

from ..errors import HydroError, InterlockError
from ..truck.machine import TruckMachine
from ..vent.latch import VentLatch
from ..vent.staging import REASON_RECONCILE, VentCheckpoint, VentStaging
from ..vent.valve import VentValve


def _violation_for_hold(reason: str) -> str:
    """冻结原因到违约项名的映射，审计里用同一套口径。"""

    return "vent_reconcile_mismatch" if reason == REASON_RECONCILE else "vent_switch_failed"


def _run_vent(staging: VentStaging, dt: float, max_steps: int) -> tuple[list[dict], list[str]]:
    """把两段放散跑到完成或冻结，返回步骤记录与段量违约项。"""

    log: list[dict] = []
    violations: list[str] = []
    for _ in range(max_steps):
        staging.tick(dt)
        if staging.state == "hold":
            log.append(
                {
                    "component": "vent",
                    "action": "vent_switch_failed"
                    if staging.hold_reason != REASON_RECONCILE
                    else "vent_reconcile_failed",
                    "severity": "alarm",
                    "detail": {
                        "reason": staging.hold_reason,
                        "attempts": staging.switch_attempts,
                        "pressure_mpa": round(staging.pressure_mpa, 3),
                    },
                }
            )
            break
        if staging.is_complete():
            break
    else:
        violations.append("vent_timeout")

    report = staging.reconcile_report()
    for stage_key in ("stage1", "stage2"):
        amount = report[stage_key]
        if amount is None:
            continue
        log.append(
            {
                "component": "vent",
                "action": f"vent_stage_{amount['stage']}_amount",
                "severity": "notice" if amount["matched"] else "alarm",
                "detail": amount,
            }
        )
    log.append(
        {
            "component": "vent",
            "action": "vent_reconcile",
            "severity": "notice" if report["total_matched"] else "alarm",
            "detail": {
                "total_meter_kg": report["total_meter_kg"],
                "total_pressure_kg": report["total_pressure_kg"],
                "total_matched": report["total_matched"],
                "total_discrepancy_ratio": report["total_discrepancy_ratio"],
            },
        }
    )
    if not report["total_matched"] and "vent_reconcile_mismatch" not in violations:
        violations.append("vent_reconcile_mismatch")
    return log, violations


def unload_truck(
    truck: TruckMachine,
    vent: VentValve,
    latch: VentLatch,
    staging: VentStaging,
    station: str,
    hose_pressure_mpa: float = 20.0,
    target_kg: float = 20.0,
    dt: float = 1.0,
    max_steps: int = 4000,
) -> dict:
    """执行一次卸氢与两段放散，返回带步骤与违约项的报告。"""

    steps: list[dict] = []
    violations: list[str] = []
    checkpoint: dict | None = None
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

        # ---- 两段放散：先大后小，顺序由状态机写死 -----------------------
        staging.start(truck.pressure_mpa)
        steps.append(
            {
                "component": "vent",
                "action": "vent_stage_start",
                "detail": {"stage": 1, "position": vent.position(), "pressure_mpa": round(truck.pressure_mpa, 3)},
            }
        )
        vent_steps, vent_violations = _run_vent(staging, dt, max_steps)
        steps.extend(vent_steps)
        # 状态机里的压力才是权威账，回写到槽车状态机供拔管联锁判断。
        truck.bleed(truck.pressure_mpa - staging.pressure_mpa)
        violations.extend(vent_violations)

        if staging.is_hold():
            violation = _violation_for_hold(staging.hold_reason)
            if violation not in violations:
                violations.append(violation)
            checkpoint = staging.checkpoint().as_dict()
            steps.append(
                {
                    "component": "vent",
                    "action": "vent_checkpoint",
                    "severity": "alarm",
                    "detail": {"resumable": True, "state": staging.state, "reason": staging.hold_reason},
                }
            )
        elif staging.is_complete() and not violations:
            staging.finish()
            latch.release()
            steps.append(
                {
                    "component": "vent",
                    "action": "vent_hold",
                    "detail": {
                        "pressure_mpa": round(truck.pressure_mpa, 3),
                        "vented_mpa": round(vent.vented_mpa(), 3),
                        "ready": truck.discharge_ready(),
                    },
                }
            )
            steps.append({"component": "vent", "action": "close", "detail": {"latch_engaged": latch.is_engaged()}})
            truck.disconnect()
            steps.append({"component": "truck", "action": "disconnect", "detail": {"state": truck.state}})
        elif not staging.is_complete():
            if "vent_timeout" not in violations:
                violations.append("vent_timeout")
    except HydroError as error:
        payload = error.as_record()
        marker = f"{payload['component']}:{payload['type']}"
        if marker not in violations:
            violations.append(marker)
        steps.append({"component": payload["component"], "action": "rejected", "detail": {"message": payload["message"]}})

    return {
        "station": station,
        "ok": not violations,
        "steps": steps,
        "violations": violations,
        "vent_reconciliation": staging.reconcile_report(),
        "checkpoint": checkpoint,
        "resumable": checkpoint is not None,
    }


def resume_unload_truck(
    truck: TruckMachine,
    vent: VentValve,
    latch: VentLatch,
    staging: VentStaging,
    station: str,
    checkpoint: dict,
    dt: float = 1.0,
    max_steps: int = 4000,
) -> dict:
    """从放散中断检查点续放并收尾拔管。"""

    steps: list[dict] = []
    violations: list[str] = []
    new_checkpoint: dict | None = None
    try:
        cp = VentCheckpoint.from_dict(checkpoint)
        # 先在检查点快照上判定能不能续，拒绝时绝不动内存账册，现场标定与
        # 已重写的检查点都保持原样，允许排查后再次发起续放。
        if cp.hold_reason == "stage_reconcile_mismatch":
            stage1_ok = cp.stage1 is None or cp.stage1.matched
            stage2_ok = cp.stage2 is None or cp.stage2.matched
            if not (stage1_ok and stage2_ok):
                raise InterlockError(
                    "放散段量仍对不上，先标定流量计并重新核对再续放",
                    component="vent",
                )
        staging.restore(cp)
        truck.restore_pressure(cp.pressure_mpa)
        # 跨重启续放时闩锁是松开的，先重新咬合保持阀位，再恢复放散。
        if not latch.is_engaged():
            latch.engage()
        steps.append(
            {
                "component": "vent",
                "action": "vent_resume",
                "severity": "warning",
                "detail": {"from_state": cp.state, "reason": cp.hold_reason, "pressure_mpa": round(cp.pressure_mpa, 3)},
            }
        )
        staging.resume()
        vent_steps, vent_violations = _run_vent(staging, dt, max_steps)
        steps.extend(vent_steps)
        truck.bleed(truck.pressure_mpa - staging.pressure_mpa)
        violations.extend(vent_violations)

        if staging.is_hold():
            violation = _violation_for_hold(staging.hold_reason)
            if violation not in violations:
                violations.append(violation)
            new_checkpoint = staging.checkpoint().as_dict()
            steps.append(
                {
                    "component": "vent",
                    "action": "vent_checkpoint",
                    "severity": "alarm",
                    "detail": {"resumable": True, "state": staging.state, "reason": staging.hold_reason},
                }
            )
        elif staging.is_complete() and not violations:
            staging.finish()
            latch.release()
            steps.append(
                {
                    "component": "vent",
                    "action": "vent_hold",
                    "detail": {
                        "pressure_mpa": round(truck.pressure_mpa, 3),
                        "vented_mpa": round(vent.vented_mpa(), 3),
                        "ready": truck.discharge_ready(),
                    },
                }
            )
            truck.disconnect()
            steps.append({"component": "truck", "action": "disconnect", "detail": {"state": truck.state}})
        elif not staging.is_complete():
            if "vent_timeout" not in violations:
                violations.append("vent_timeout")
    except HydroError as error:
        payload = error.as_record()
        marker = f"{payload['component']}:{payload['type']}"
        if marker not in violations:
            violations.append(marker)
        steps.append({"component": payload["component"], "action": "rejected", "detail": {"message": payload["message"]}})

    return {
        "station": station,
        "ok": not violations,
        "steps": steps,
        "violations": violations,
        "vent_reconciliation": staging.reconcile_report(),
        "checkpoint": new_checkpoint,
        "resumable": new_checkpoint is not None,
    }
