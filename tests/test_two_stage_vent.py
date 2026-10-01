"""两段放散：顺序、切段核对、分段排量、失败策略与续放。"""

from __future__ import annotations

import dataclasses

import pytest

from hydrorefuel.config import (
    VENT_POLICY_HOLD,
    VENT_POLICY_REVERT,
    default_config,
)
from hydrorefuel.errors import ConfigError, InterlockError
from hydrorefuel.vent.staged import StagedVent


def _hold_config():
    cfg = default_config()
    return dataclasses.replace(
        cfg, vent=dataclasses.replace(cfg.vent, switch_failure_policy=VENT_POLICY_HOLD)
    )


def test_two_stage_order_is_fixed_high_then_low(platform):
    report = platform.unload(hose_pressure_mpa=20.0, target_kg=20.0)
    assert report["ok"], report["violations"]
    actions = [step["action"] for step in report["steps"]]
    assert actions.index("vent_switch") < actions.index("vent_reconcile")
    assert actions.index("vent_reconcile") < actions.index("disconnect")
    switch = [s for s in report["steps"] if s["action"] == "vent_switch"][0]["detail"]
    assert (switch["requested"], switch["actual"]) == ("low", "low")
    assert platform.status()["vent"]["stage_order"] == ["high", "low"]


def test_each_stage_meters_separately_and_reconciles(platform):
    report = platform.unload(hose_pressure_mpa=20.0, target_kg=20.0)
    assert report["ok"], report["violations"]
    detail = [s for s in report["steps"] if s["action"] == "vent_reconcile"][0]["detail"]
    assert detail["ok"] is True
    assert detail["high_vented_mpa"] > 0.0
    assert detail["low_vented_mpa"] > 0.0
    # 阀表分段底数差与分段台账一致，两段之和等于总压降。
    assert abs(detail["meter_high_vented_mpa"] - detail["high_vented_mpa"]) < 1e-6
    assert abs(detail["meter_low_vented_mpa"] - detail["low_vented_mpa"]) < 1e-6
    assert (
        abs(
            detail["high_vented_mpa"]
            + detail["low_vented_mpa"]
            - (detail["initial_pressure_mpa"] - detail["final_pressure_mpa"])
        )
        < 1e-6
    )


def test_cannot_disconnect_until_vent_completes(platform):
    platform.truck.reset()
    platform.truck.connect(20.0)
    with pytest.raises(InterlockError):
        platform.truck.disconnect()


def test_switch_jam_revert_retries_then_fails(platform):
    platform.vent.force_switch_jam(True)
    report = platform.unload(hose_pressure_mpa=20.0, target_kg=20.0)
    assert not report["ok"]
    assert "vent_switch_stuck" in report["violations"]
    assert report["resumable"] is False
    assert platform.truck.state != "drained"
    switches = [s for s in report["steps"] if s["action"] == "vent_switch"]
    assert len(switches) == 3
    assert all(s["detail"]["ok"] is False for s in switches)
    assert all(s["detail"]["policy"] == VENT_POLICY_REVERT for s in switches)


def test_transient_jam_self_heals_under_revert(platform):
    platform.truck.reset()
    platform.truck.connect(20.0)
    platform.truck.open_unload()
    for _ in range(4000):
        if platform.truck.delivered_kg >= 20.0:
            break
        platform.truck.transfer(1.0)
    platform.truck.finish_unload()
    platform.vent_latch.engage()
    platform.vent.force_switch_jam(True)
    staged = platform.staged_vent
    staged.begin(
        platform.truck.pressure_mpa, "run-selfheal", confirm_mpa=platform.config.truck.vent_confirm_mpa
    )
    failures = 0
    while staged.state == "running":
        outcome = staged.step(platform.truck.pressure_mpa, 1.0)
        platform.truck.bleed(platform.truck.pressure_mpa - outcome["pressure_mpa"])
        if outcome["switch_attempt"] is not None and not outcome["switch_attempt"]["ok"]:
            failures += 1
            if failures == 2:
                platform.vent.force_switch_jam(False)
    report = staged.complete(platform.truck.pressure_mpa, platform.config.truck.vent_confirm_mpa)
    assert report["ok"]
    assert [a.ok for a in staged.switch_attempts()] == [False, False, True]


def test_hold_policy_pauses_and_resume_finishes(tmp_path, clock):
    from hydrorefuel.app import build_platform

    platform = build_platform(_hold_config(), clock=clock, storage_root=str(tmp_path / "state"))
    platform.vent.force_switch_jam(True)
    report = platform.unload(hose_pressure_mpa=20.0, target_kg=20.0)
    assert not report["ok"]
    assert "vent_switch_stuck_hold" in report["violations"]
    assert report["resumable"] is True
    run_id = report["run_id"]
    assert platform.truck.state != "drained"
    assert report["vent"]["stages"][0]["vented_mpa"] > 0.0

    # 没排障就续放：被拒绝且仍停在部分失败态。
    rejected = platform.resume_unload()
    assert not rejected["ok"]
    assert platform.staged_vent.is_paused()

    platform.vent.force_switch_jam(False)
    resumed = platform.resume_unload()
    assert resumed["ok"], resumed["violations"]
    assert resumed["run_id"] == run_id
    assert platform.truck.state == "drained"


def test_resume_without_interruption_is_rejected(platform):
    report = platform.resume_unload()
    assert not report["ok"]
    assert "vent:InterlockError" in report["violations"]


def test_audit_carries_run_id_and_stage_quantities(tmp_path, clock):
    from hydrorefuel.app import build_platform

    platform = build_platform(_hold_config(), clock=clock, storage_root=str(tmp_path / "state"))
    platform.vent.force_switch_jam(True)
    first = platform.unload(hose_pressure_mpa=20.0, target_kg=20.0)
    run_id = first["run_id"]
    platform.vent.force_switch_jam(False)
    platform.resume_unload()
    entries = platform.audit.tail(200)
    matched = [e for e in entries if e["payload"].get("run_id") == run_id]
    actions = {e["action"] for e in matched}
    assert {"vent_begin", "vent_paused", "vent_resume", "vent_reconcile"} <= actions
    reconcile = [e for e in matched if e["action"] == "vent_reconcile"]
    assert reconcile and "high_vented_mpa" in reconcile[0]["payload"]


def test_console_exposes_vent_run(platform):
    from hydrorefuel.console.server import ConsoleServer

    server = ConsoleServer(platform, port=0)
    base = server.start()
    try:
        import json
        import urllib.request

        with urllib.request.urlopen(f"{base}/api/vent/run", timeout=5) as response:
            payload = json.loads(response.read().decode())
        assert payload["vent_run"]["stage_order"] == ["high", "low"]
    finally:
        server.stop()


def test_switch_pressure_must_sit_between_confirm_and_max():
    cfg = default_config()
    bad = dataclasses.replace(cfg, vent=dataclasses.replace(cfg.vent, switch_pressure_mpa=1.0))
    with pytest.raises(ConfigError):
        build_and_validate(bad)
    bad_policy = dataclasses.replace(cfg, vent=dataclasses.replace(cfg.vent, switch_failure_policy="abort"))
    with pytest.raises(ConfigError):
        build_and_validate(bad_policy)


def build_and_validate(config):
    from hydrorefuel.config import StationConfig, config_to_mapping

    return StationConfig.from_mapping(config_to_mapping(config))


def test_low_orifice_must_be_smaller_than_high():
    cfg = default_config()
    bad = dataclasses.replace(
        cfg,
        vent=dataclasses.replace(cfg.vent, high_orifice_mm=2.0, low_orifice_mm=9.0),
    )
    with pytest.raises(ConfigError):
        build_and_validate(bad)


def test_begin_requires_reachable_confirm(platform):
    staged = StagedVent(platform.vent, platform.config.vent)
    with pytest.raises(InterlockError):
        staged.begin(20.0, "run", confirm_mpa=platform.config.vent.ambient_mpa)
    with pytest.raises(InterlockError):
        staged.begin(20.0, "run", confirm_mpa=platform.config.vent.switch_pressure_mpa + 1.0)
