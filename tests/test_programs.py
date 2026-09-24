from modbuslite import DataStore
from plant.programs import Distribution, Intake, Treatment

DT = 50 * 600 / 3600 / 1000   # 50 ms scan at 600x = 0.00833 simulated hours


def intake(seed=1):
    return Intake(DataStore(coils=32, discrete=32, holding=256, input=64), seed=seed)


def test_intake_auto_level_control_starts_and_stops_pump():
    p = intake()
    p.volume = 0.30 * p.TANK_M3
    for _ in range(20):
        p.execute(DT)
    assert p.pump_run and p.get("PUMP_RUNNING") == 1 and p.get("FT-101") > 0
    p.volume = 0.90 * p.TANK_M3
    for _ in range(40):
        p.execute(DT)
    assert not p.pump_run and p.get("FT-101") == 0


def test_intake_high_high_interlock_latches_until_reset():
    p = intake()
    p.set("AUTO_MODE", 0)
    p.set("PUMP_START_CMD", 1)
    p.execute(DT)
    assert p.pump_run
    p.volume = 0.97 * p.TANK_M3
    p.execute(DT)          # physics publishes the new level
    p.execute(DT)          # logic sees it on the next scan
    assert p.lahh and not p.pump_run and p.get("LAHH_TRIP") == 1 and p.store.holding[2] == 1
    p.set("ALARM_RESET", 1)      # still above the reset band -> stays latched
    p.execute(DT)
    assert p.lahh
    p.volume = 0.80 * p.TANK_M3
    p.execute(DT)          # level published
    p.set("ALARM_RESET", 1)
    p.execute(DT)
    assert not p.lahh and p.get("ALARM_RESET") == 0


def test_intake_setpoints_are_clamped_to_engineering_limits():
    p = intake()
    p.set("LEVEL_START_SP", 99.0)
    p.set("PUMP_SPEED_SP", 250.0)
    p.execute(DT)
    assert p.get("LEVEL_START_SP") == p.get("LEVEL_STOP_SP") - 5.0
    assert p.get("PUMP_SPEED_SP") == p.get("PUMP_MAX_SPEED")


def test_intake_manual_commands_are_momentary():
    p = intake()
    p.set("AUTO_MODE", 0)
    p.set("PUMP_STOP_CMD", 1)
    p.execute(DT)
    assert p.get("PUMP_STOP_CMD") == 0 and not p.pump_run


def test_treatment_dosing_controller_holds_setpoint():
    p = Treatment(DataStore(coils=32, discrete=32, holding=256, input=64), seed=3)
    p.remote["PLC-001"] = {"LT-101": 60.0}
    p.remote_ok["PLC-001"] = True
    for _ in range(3000):
        p.execute(DT)
    assert abs(p.get("AT-201") - 1.5) < 0.25
    assert p.get("DOSING_RUNNING") == 1 and p.get("FT-201") > 100


def test_treatment_high_high_chlorine_interlock_stops_dosing():
    p = Treatment(DataStore(coils=32, discrete=32, holding=256, input=64), seed=3)
    p.remote["PLC-001"] = {"LT-101": 60.0}
    p.remote_ok["PLC-001"] = True
    for _ in range(50):
        p.execute(DT)
    p.residual = 4.5
    p.execute(DT)          # the analyser publishes the new residual
    p.execute(DT)          # the logic acts on the measured value on the next scan
    assert p.aahh and not p.dosing_run and p.get("AAHH_TRIP") == 1


def test_treatment_backwash_resets_filter_dp():
    p = Treatment(DataStore(coils=32, discrete=32, holding=256, input=64), seed=3)
    p.remote["PLC-001"] = {"LT-101": 60.0}
    p.remote_ok["PLC-001"] = True
    p.dp = 70.0
    p.execute(DT)
    assert p.get("BACKWASH_ACTIVE") == 1
    for _ in range(40):
        p.execute(DT)
    assert p.dp < 20 and p.get("BACKWASH_ACTIVE") == 0


def test_distribution_pressure_control_and_low_low_interlock():
    p = Distribution(DataStore(coils=32, discrete=32, holding=256, input=64), seed=5)
    p.remote["PLC-002"] = {"FT-201": 120.0}
    p.remote_ok["PLC-002"] = True
    for _ in range(1500):
        p.execute(DT)
    assert abs(p.get("PT-301") - 4.0) < 0.4 and p.get("P301_RUNNING") == 1
    p.volume = 0.05 * p.TANK_M3
    p.execute(DT)
    p.execute(DT)
    assert p.lall and not any(p.run) and p.store.holding[2] & 1


def test_manual_commands_pressed_in_auto_are_discarded():
    p = intake()
    p.set("PUMP_START_CMD", 1)          # pressed while in AUTO
    p.execute(DT)
    assert p.get("PUMP_START_CMD") == 0  # consumed, not latched for a later mode change


def test_stale_remote_values_are_not_used_without_a_healthy_link():
    p = Distribution(DataStore(coils=32, discrete=32, holding=256, input=64), seed=5)
    p.remote["PLC-002"] = {"FT-201": 120.0}    # value present but link not healthy
    v0 = p.volume
    for _ in range(20):
        p.execute(DT)
    assert p.volume < v0                        # no inflow was credited


def test_lag_pump_drops_out_when_lead_pump_holds_pressure():
    p = Distribution(DataStore(coils=32, discrete=32, holding=256, input=64), seed=5)
    p.remote["PLC-002"] = {"FT-201": 120.0}
    p.remote_ok["PLC-002"] = True
    p.run = [True, True]
    p.lag_timer = 10
    for _ in range(600):
        p.execute(DT)
    assert not p.run[1]
