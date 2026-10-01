"""两段放散：先大流量后小流量、切段核对、失败策略与续放。

覆盖的硬约束：

* 顺序定死为先大后小，不能跳段/倒序（阀门层与状态机层都拒绝）；
* 大流量段放到切段压力才切小流量收尾，两段各自的流量计账与压力账都要
  对得上，总量也要对得上；
* 切段失败必须看得出来，并按管路策略处理：``hold`` 停在当前段报警，
  ``revert`` 退回大流量段重试，重试耗尽才报警；
* 任何中断都是部分失败：阀关断、检查点落盘，可续放，已放掉的段量保留；
* 放不到拔管确认压力绝不允许拔管。
"""

from __future__ import annotations

import dataclasses

import pytest

from hydrorefuel.config import ConfigError, default_config
from hydrorefuel.errors import InterlockError
from hydrorefuel.vent.staging import (
    COMPLETE,
    HOLD,
    STAGE1,
    STAGE2,
    VentStaging,
)
from hydrorefuel.vent.valve import SMALL, SWITCH_FAULTS, VentValve
from hydrorefuel.vent.meter import VentFlowMeter


def _cfg(**vent_overrides):
    cfg = default_config()
    if vent_overrides:
        cfg = dataclasses.replace(cfg, vent=dataclasses.replace(cfg.vent, **vent_overrides))
    return cfg


def _controller(cfg):
    valve = VentValve(cfg.vent)
    meter = VentFlowMeter()
    return valve, meter, VentStaging(valve, meter, cfg.vent, cfg.truck)


def _run(staging, dt=1.0, limit=4000):
    for _ in range(limit):
        if staging.state in (COMPLETE, HOLD):
            break
        staging.tick(dt)
    return staging.state


# ---- 顺序：先大后小，写死，不能跳段 --------------------------------------


def test_open_valve_always_starts_at_large_stage():
    cfg = _cfg()
    valve = VentValve(cfg.vent)
    valve.open()
    assert valve.position() == "large"
    assert valve.stage() == 1
    valve.close()


def test_cannot_switch_before_open():
    cfg = _cfg()
    valve = VentValve(cfg.vent)
    with pytest.raises(InterlockError):
        valve.switch_to_small()


def test_cannot_bleed_while_closed():
    cfg = _cfg()
    valve = VentValve(cfg.vent)
    with pytest.raises(InterlockError):
        valve.bleed(10.0, 1.0)


def test_small_stage_rate_is_lower_than_large():
    cfg = _cfg()
    valve = VentValve(cfg.vent)
    valve.open()
    large_rate = valve.bleed_rate_mpa_per_s(10.0)
    valve.switch_to_small()
    valve.settle_switch()
    assert valve.position() == SMALL
    small_rate = valve.bleed_rate_mpa_per_s(10.0)
    assert small_rate < large_rate


def test_cannot_start_vent_twice():
    cfg = _cfg()
    _, _, staging = _controller(cfg)
    staging.start(20.0)
    with pytest.raises(InterlockError):
        staging.start(20.0)


def test_stage_order_is_large_then_small():
    cfg = _cfg()
    valve, meter, staging = _controller(cfg)
    staging.start(20.0)
    seen = []
    previous = STAGE1
    for _ in range(4000):
        if staging.state != previous:
            seen.append(staging.state)
            previous = staging.state
        if staging.state in (COMPLETE, HOLD):
            break
        staging.tick(1.0)
    assert staging.state == COMPLETE
    # 只允许沿 大流量 → 切换中 → 小流量 → 完成 单向走。
    assert "stage2_small" in seen
    assert seen.index("stage2_small") > seen.index("switching")
    # 一段已结清（切段成功时冻结）。
    assert staging.stage_amount(1) is not None and staging.stage_amount(1).matched


# ---- 正常两段放散：段量两套账都对得上 ------------------------------------


def test_normal_two_stage_vent_reconciles_each_stage(platform):
    report = platform.unload(hose_pressure_mpa=20.0, target_kg=20.0)
    assert report["ok"], report["violations"]
    assert platform.truck.state == "drained"
    reconciliation = report["vent_reconciliation"]
    stage1 = reconciliation["stage1"]
    stage2 = reconciliation["stage2"]
    assert stage1["stage"] == 1 and stage1["matched"]
    assert stage2["stage"] == 2 and stage2["matched"]
    assert reconciliation["total_matched"]
    # 两段都排掉了正排量，且大流量段排量大于小流量收尾段。
    assert stage1["vented_mpa"] > stage2["vented_mpa"] > 0
    assert abs(stage1["meter_kg"] - stage1["pressure_kg"]) / stage1["pressure_kg"] <= 0.1


def test_unload_actions_record_stage_amounts_in_order(platform):
    report = platform.unload(hose_pressure_mpa=20.0, target_kg=20.0)
    actions = [step["action"] for step in report["steps"]]
    assert actions.index("vent_stage_1_amount") < actions.index("vent_stage_2_amount")
    assert actions.index("vent_stage_2_amount") < actions.index("disconnect")
    # 必须先放散后拔管。
    assert "vent_hold" in actions
    assert actions.index("vent_hold") < actions.index("disconnect")


def test_vent_snapshot_exposes_per_stage_amounts(platform):
    platform.unload(hose_pressure_mpa=20.0, target_kg=20.0)
    snap = platform.vent_snapshot()
    assert snap["stage1_meter_kg"] > 0
    assert snap["stage2_meter_kg"] > 0
    assert snap["stage1_closed"]["matched"] is True
    assert snap["stage2_closed"]["matched"] is True


def test_stage_amounts_agree_across_report_audit_console_journal(platform):
    report = platform.unload(hose_pressure_mpa=20.0, target_kg=20.0)
    op_id = report["op_id"]

    def _audit_amount(stage):
        for entry in platform.audit.entries(limit=300):
            payload = entry["payload"]
            if (
                payload.get("op_id") == op_id
                and entry["action"] == f"vent_stage_{stage}_amount"
            ):
                return payload
        raise AssertionError(f"审计缺少 {stage} 段排量记录")

    for stage in (1, 2):
        expected = report["vent_reconciliation"][f"stage{stage}"]
        audited = _audit_amount(stage)
        assert abs(audited["meter_kg"] - expected["meter_kg"]) < 1e-6
        assert abs(audited["pressure_kg"] - expected["pressure_kg"]) < 1e-6
        assert audited["matched"] == expected["matched"]

    # 控制台快照与落盘回放必须给出同一份段量。
    snap = platform.vent_snapshot()
    assert abs(snap["stage1_closed"]["meter_kg"] - _audit_amount(1)["meter_kg"]) < 1e-6
    replayed = [
        entry
        for entry in platform.audit.replay_from_journal()
        if entry["payload"].get("op_id") == op_id and entry["action"] == "vent_stage_1_amount"
    ]
    assert replayed
    assert abs(replayed[0]["payload"]["meter_kg"] - _audit_amount(1)["meter_kg"]) < 1e-6


# ---- 切段失败：必须看得出来 + 按管路策略 ---------------------------------


@pytest.mark.parametrize("fault", list(SWITCH_FAULTS))
def test_switch_failure_is_visible_and_holds(platform, fault):
    platform.config = dataclasses.replace(
        platform.config,
        vent=dataclasses.replace(platform.config.vent, switch_policy="hold", switch_max_attempts=1),
    )
    # 平台已按默认配置装配，这里直接对放散阀挂故障（演练注入口径）。
    platform.inject_vent_fault(fault)
    report = platform.unload(hose_pressure_mpa=20.0, target_kg=20.0)
    assert not report["ok"]
    assert "vent_switch_failed" in report["violations"]
    assert report["resumable"] is True
    assert report["checkpoint"]["state"] == "hold"
    assert report["checkpoint"]["hold_reason"]
    # 报警期间阀门必须关着、绝不能拔管。
    assert platform.vent.is_open() is False
    assert platform.truck.state == "connected"


def test_hold_policy_freezes_at_current_stage(platform):
    platform.config = dataclasses.replace(
        platform.config,
        vent=dataclasses.replace(platform.config.vent, switch_policy="hold", switch_max_attempts=1),
    )
    platform.inject_vent_fault("large_not_close")
    report = platform.unload(hose_pressure_mpa=20.0, target_kg=20.0)
    # 切段从未成功：一段只记了段内进度，没有结清段量。
    assert report["checkpoint"]["stage1"] is None
    assert report["checkpoint"]["stage1_open_pressure_mpa"] > 0
    # 控制台快照标记可续放。
    assert platform.vent_snapshot()["resumable"] is True


def test_revert_policy_retries_then_holds_after_exhaustion(platform):
    platform.config = dataclasses.replace(
        platform.config,
        vent=dataclasses.replace(platform.config.vent, switch_policy="revert", switch_max_attempts=2),
    )
    platform.inject_vent_fault("large_not_close")
    report = platform.unload(hose_pressure_mpa=20.0, target_kg=20.0)
    assert not report["ok"]
    # 重试两次都失败才冻结，切段次数如实落检查点。
    assert report["checkpoint"]["attempts"] == 2
    assert report["checkpoint"]["state"] == "hold"


def test_switch_failure_audit_is_alarm_severity(platform):
    platform.config = dataclasses.replace(
        platform.config,
        vent=dataclasses.replace(platform.config.vent, switch_policy="hold", switch_max_attempts=1),
    )
    platform.inject_vent_fault("small_not_open")
    platform.unload(hose_pressure_mpa=20.0, target_kg=20.0)
    failed = [
        entry
        for entry in platform.audit.entries(limit=200)
        if entry["action"] == "vent_switch_failed"
    ]
    assert failed and failed[0]["severity"] == "alarm"
    assert failed[0]["payload"]["reason"] == "position_feedback_mismatch"


# ---- 部分失败可续放：续到到位才拔管 --------------------------------------


def test_resume_after_switch_failure_completes(platform):
    platform.config = dataclasses.replace(
        platform.config,
        vent=dataclasses.replace(platform.config.vent, switch_policy="hold", switch_max_attempts=1),
    )
    platform.inject_vent_fault("large_not_close")
    first = platform.unload(hose_pressure_mpa=20.0, target_kg=20.0)
    assert first["resumable"]
    held_pressure = first["checkpoint"]["pressure_mpa"]

    # 故障没排除前续放仍失败，且不能拔管、检查点保留。
    refused = platform.resume_unload(1.0)
    assert not refused["ok"]
    assert platform.truck.state == "connected"
    assert platform.store.read("vent_checkpoint") is not None

    platform.clear_vent_fault()
    resumed = platform.resume_unload(1.0)
    assert resumed["ok"], resumed["violations"]
    assert platform.truck.state == "drained"
    # 两段最终都核对通过。
    assert resumed["vent_reconciliation"]["stage2"]["matched"]
    assert resumed["vent_reconciliation"]["total_matched"]
    # 续放完成后撤检查点，最终压力确实放到了确认值以下。
    assert platform.store.read("vent_checkpoint") is None
    assert held_pressure > platform.config.truck.vent_confirm_mpa


def test_resume_cannot_disconnect_until_vent_complete(platform):
    platform.config = dataclasses.replace(
        platform.config,
        vent=dataclasses.replace(platform.config.vent, switch_policy="hold", switch_max_attempts=1),
    )
    platform.inject_vent_fault("small_not_open")
    platform.unload(hose_pressure_mpa=20.0, target_kg=20.0)
    assert not platform.truck.discharge_ready()
    with pytest.raises(InterlockError):
        platform.truck.disconnect()


def test_resume_across_restart_from_persisted_checkpoint(tmp_path):
    from hydrorefuel.app import build_platform
    from hydrorefuel.clock import ManualClock

    store = str(tmp_path / "state")
    cfg = dataclasses.replace(
        default_config(),
        vent=dataclasses.replace(default_config().vent, switch_policy="hold", switch_max_attempts=1),
    )
    first = build_platform(cfg, clock=ManualClock(), storage_root=store)
    first.inject_vent_fault("large_not_close")
    report = first.unload(hose_pressure_mpa=20.0, target_kg=20.0)
    assert report["resumable"]

    # 新平台（等同跨重启）从同一存储读到检查点并续放成功。
    second = build_platform(cfg, clock=ManualClock(), storage_root=store)
    second.clear_vent_fault()
    resumed = second.resume_unload(1.0)
    assert resumed["ok"], resumed["violations"]
    assert second.truck.state == "drained"
    assert resumed["vent_reconciliation"]["total_matched"]


# ---- 段量对不上：报警、不许续放、标定核对后才能续 ------------------------


def test_meter_mismatch_blocks_and_recovers_after_calibration(platform):
    platform.set_vent_meter_bias(0.30)
    report = platform.unload(hose_pressure_mpa=20.0, target_kg=20.0)
    assert report["violations"] == ["vent_reconcile_mismatch"]
    assert report["checkpoint"]["hold_reason"] == "stage_reconcile_mismatch"
    assert platform.truck.state == "connected"

    # 账没平之前续放被拒绝，检查点保留。
    assert not platform.resume_unload(1.0)["ok"]
    assert platform.store.read("vent_checkpoint") is not None

    # 现场标定把多计的 30% 折回去，重新核对通过后才允许续放。
    platform.calibrate_vent_meter(1.0 / 1.3)
    assert platform.recheck_vent()["matched"] is True
    resumed = platform.resume_unload(1.0)
    assert resumed["ok"], resumed["violations"]
    assert platform.truck.state == "drained"
    assert resumed["vent_reconciliation"]["total_matched"]


def test_stage2_mid_flow_mismatch_freezes_only_stage2():
    cfg = _cfg()
    valve, meter, staging = _controller(cfg)
    staging.start(20.0)
    while staging.state != STAGE2:
        staging.tick(1.0)
    assert staging.stage_amount(1).matched
    meter.set_bias(0.30)
    while staging.state == STAGE2:
        staging.tick(1.0)
    assert staging.state == HOLD
    assert staging.hold_reason == "stage_reconcile_mismatch"
    assert not staging.stage_amount(2).matched
    # 标定修正后重新核对、续放、完成。
    meter.calibrate(1.0 / 1.3)
    assert staging.recheck()
    staging.resume()
    assert _run(staging) == COMPLETE
    assert staging.reconciliation_ok()
    staging.finish()


# ---- 配置：顺序与压力窗口在装配期就锁死 ----------------------------------


def test_small_orifice_must_be_smaller_than_large():
    cfg = dataclasses.replace(default_config())
    bad = dataclasses.replace(cfg.vent, small_orifice_mm=cfg.vent.orifice_mm + 1.0)
    with pytest.raises(ConfigError):
        bad.__class__.from_mapping(
            {
                "orifice_mm": 6.0,
                "small_orifice_mm": 9.0,
                "rate_coeff": 0.9,
                "ambient_mpa": 0.1,
                "latch_travel_mm": 40.0,
                "switch_mpa": 5.0,
                "switch_timeout_s": 10.0,
                "switch_policy": "hold",
                "rate_band": 0.25,
                "switch_max_attempts": 2,
                "reconcile_tolerance": 0.1,
                "stage1_timeout_s": 90.0,
                "stage2_timeout_s": 240.0,
            }
        )


def test_switch_pressure_must_be_above_confirm_pressure():
    from hydrorefuel.config import StationConfig, config_to_mapping

    cfg = dataclasses.replace(default_config())
    bad = dataclasses.replace(cfg, vent=dataclasses.replace(cfg.vent, switch_mpa=1.0))
    with pytest.raises(ConfigError):
        StationConfig.from_mapping(config_to_mapping(bad))


def test_switch_policy_must_be_hold_or_revert():
    from hydrorefuel.config import VentLimits

    with pytest.raises(ConfigError):
        VentLimits.from_mapping(
            {
                "orifice_mm": 6.0,
                "small_orifice_mm": 1.5,
                "rate_coeff": 0.9,
                "ambient_mpa": 0.1,
                "latch_travel_mm": 40.0,
                "switch_mpa": 5.0,
                "switch_timeout_s": 1.0,
                "switch_policy": "whatever",
                "rate_band": 0.25,
                "switch_max_attempts": 2,
                "reconcile_tolerance": 0.1,
                "stage1_timeout_s": 90.0,
                "stage2_timeout_s": 240.0,
            }
        )


# ---- 控制台：每段排量可展示、续放有接口 ----------------------------------


def test_console_vent_endpoints_expose_and_resume(platform):
    import json
    import urllib.request

    from hydrorefuel.console.server import ConsoleServer

    platform.config = dataclasses.replace(
        platform.config,
        vent=dataclasses.replace(platform.config.vent, switch_policy="hold", switch_max_attempts=1),
    )
    server = ConsoleServer(platform, port=0)
    base = server.start()
    try:
        platform.inject_vent_fault("large_not_close")
        platform.unload(hose_pressure_mpa=20.0, target_kg=20.0)

        with urllib.request.urlopen(f"{base}/api/vent", timeout=5) as response:
            vent = json.loads(response.read().decode("utf-8"))["vent"]
        assert vent["resumable"] is True
        assert vent["controller_state"] == "hold"

        request = urllib.request.Request(
            f"{base}/api/vent/fault/clear", data=b"{}", method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            assert response.status == 200

        request = urllib.request.Request(
            f"{base}/api/unload/resume", data=b"{}", method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            resumed = json.loads(response.read().decode("utf-8"))
        assert resumed["ok"] is True
    finally:
        server.stop()
