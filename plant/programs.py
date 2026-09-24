"""PLC control programs and the physical process models they control.

Each program owns a section of the water works:

  intake       PLC-001  raw water intake pump and intake tank
  treatment    PLC-002  filtration and chlorine dosing
  distribution PLC-003  clearwell, high-lift pumps and network pressure

A program runs one scan at a time: read commands and setpoints from the
Modbus tables, execute interlocks and control logic, advance the physics
by the simulated time step, then publish process values and status bits.
"""
from __future__ import annotations

import math
import random
from typing import Dict, List, Optional

from modbuslite import DataStore

from .registers import (
    HR_ALARM, HR_INTERLOCK, HR_MODE, HR_REMOTE_OK, HR_SCAN, HR_SIMHOUR, HR_UPTIME,
    Tag, TagMap, status_tags,
)


class Program:
    """Base class for a PLC control program."""

    name = "base"
    tags: List[Tag] = []
    # remote tags this program reads from other PLCs: {plc name: [(tag name, table, address, scale)]}
    remote_tags: Dict[str, List[tuple]] = {}
    description = ""

    def __init__(self, store: DataStore, seed: Optional[int] = None):
        self.store = store
        self.map = TagMap(list(self.tags) + status_tags())
        self.rng = random.Random(seed)
        self.remote: Dict[str, Dict[str, float]] = {}
        self.remote_ok: Dict[str, bool] = {}
        self.sim_hours = 0.0
        self.scan = 0
        self.interlock_word = 0
        self.alarm_word = 0
        self.faults = None          # FaultBoard from the testbed instrumentation (optional)
        self.defaults()

    def actuator_failed(self, name: str) -> bool:
        return bool(self.faults and self.faults.actuator_failed(name))

    def reset_requested(self) -> bool:
        """Either the engineering reset coil or the supervisor reset command from the HMI."""
        a = self.consume("ALARM_RESET")
        b = self.consume("RESET_CMD")
        return a or b

    # --- helpers ---
    def get(self, name: str) -> float:
        return self.map.get(self.store, name)

    def set(self, name: str, value: float) -> None:
        self.map.set(self.store, name, value)

    def coil(self, name: str) -> bool:
        return bool(self.get(name))

    def consume(self, name: str) -> bool:
        """Read a momentary command coil and reset it."""
        v = self.coil(name)
        if v:
            self.set(name, 0)
        return v

    def clamp_setpoint(self, name: str, lo: float, hi: float) -> float:
        v = self.get(name)
        c = max(lo, min(hi, v))
        if c != v:
            self.set(name, c)
        return c

    def remote_value(self, plc: str, tag: str, default: float) -> float:
        return self.remote.get(plc, {}).get(tag, default)

    def measured(self, name: str, physical: float) -> float:
        """What the control logic sees: the transmitter value published on the last scan (faults included),
        or the physical state before anything has been published. A spoofed sensor therefore drives the PLC."""
        return self.get(name) if self.scan > 1 else physical

    def noise(self, sigma: float) -> float:
        return self.rng.gauss(0.0, sigma)

    def defaults(self) -> None:
        raise NotImplementedError

    def execute(self, dt_h: float) -> None:
        """Run one scan with a simulated time step of dt_h hours."""
        self.scan += 1
        self.sim_hours += dt_h
        self.logic(dt_h)
        self.physics(dt_h)
        self.publish()
        if self.faults:
            self.faults.apply_sensors(self)
        hr = self.store.holding
        hr[HR_MODE] = 1
        hr[HR_SCAN] = self.scan & 0xFFFF
        hr[HR_INTERLOCK] = self.interlock_word
        hr[HR_ALARM] = self.alarm_word
        hr[HR_UPTIME] = int(self.sim_hours * 60) & 0xFFFF
        hr[HR_SIMHOUR] = int((self.sim_hours % 24) * 100)
        word = 0
        for i, name in enumerate(self.remote_tags):
            if self.remote_ok.get(name):
                word |= 1 << i
        hr[HR_REMOTE_OK] = word

    def logic(self, dt_h: float) -> None:
        raise NotImplementedError

    def physics(self, dt_h: float) -> None:
        raise NotImplementedError

    def publish(self) -> None:
        raise NotImplementedError


# =============================================================================
class Intake(Program):
    name = "intake"
    description = "Raw water intake: river pump P-101, intake valve MOV-101, intake tank T-101"
    tags = [
        Tag("LT-101", "input", 0, 100, "%", "Intake tank T-101 level"),
        Tag("FT-101", "input", 1, 10, "m3/h", "Raw water flow"),
        Tag("AT-101", "input", 2, 10, "NTU", "Raw water turbidity"),
        Tag("SC-101", "input", 3, 100, "%", "P-101 speed feedback"),
        Tag("LT-100", "input", 4, 100, "m", "River level"),
        Tag("IT-101", "input", 5, 10, "A", "P-101 motor current"),
        Tag("FT-201-R", "input", 6, 10, "m3/h", "Treatment draw (remote from PLC-002)"),
        Tag("LEVEL_START_SP", "holding", 100, 100, "%", "Start pump below level", "operator"),
        Tag("LEVEL_STOP_SP", "holding", 101, 100, "%", "Stop pump above level", "operator"),
        Tag("PUMP_SPEED_SP", "holding", 102, 100, "%", "P-101 speed setpoint", "operator"),
        Tag("LAHH_LIMIT", "holding", 200, 100, "%", "Level high-high interlock", "engineer"),
        Tag("TURB_HIGH_LIMIT", "holding", 201, 10, "NTU", "Turbidity high alarm", "engineer"),
        Tag("PUMP_MAX_SPEED", "holding", 202, 100, "%", "P-101 maximum speed", "engineer"),
        Tag("LALL_LIMIT", "holding", 203, 100, "%", "Level low-low alarm", "engineer"),
        Tag("AUTO_MODE", "coils", 0, 1, "", "Automatic level control", "operator"),
        Tag("PUMP_START_CMD", "coils", 1, 1, "", "Manual: start P-101", "operator"),
        Tag("PUMP_STOP_CMD", "coils", 2, 1, "", "Manual: stop P-101", "operator"),
        Tag("VALVE_OPEN_CMD", "coils", 3, 1, "", "Manual: open MOV-101", "operator"),
        Tag("PUMP_RUNNING", "discrete", 0, 1, "", "P-101 running"),
        Tag("VALVE_OPEN", "discrete", 1, 1, "", "MOV-101 open"),
        Tag("LAHH_TRIP", "discrete", 2, 1, "", "Interlock: tank high-high"),
        Tag("TURB_HIGH", "discrete", 3, 1, "", "Alarm: turbidity high"),
        Tag("LALL_ALARM", "discrete", 4, 1, "", "Alarm: tank low-low"),
        Tag("AUTO_ACTIVE", "discrete", 5, 1, "", "Automatic mode active"),
        Tag("REMOTE_OK_002", "discrete", 6, 1, "", "Link to PLC-002 healthy"),
    ]
    remote_tags = {"PLC-002": [("FT-201", "input", 0, 10)]}
    TANK_M3 = 500.0
    PUMP_M3H = 150.0

    def defaults(self) -> None:
        self.volume = 0.55 * self.TANK_M3
        self.level = 55.0
        self.flow = 0.0
        self.turbidity = 3.2
        self.speed_fb = 0.0
        self.current = 0.0
        self.pump_run = False
        self.valve_open = False
        self.lahh = False
        self.set("LEVEL_START_SP", 40.0)
        self.set("LEVEL_STOP_SP", 85.0)
        self.set("PUMP_SPEED_SP", 80.0)
        self.set("LAHH_LIMIT", 95.0)
        self.set("TURB_HIGH_LIMIT", 20.0)
        self.set("PUMP_MAX_SPEED", 100.0)
        self.set("LALL_LIMIT", 10.0)
        self.set("AUTO_MODE", 1)

    def logic(self, dt_h: float) -> None:
        lahh_limit = self.clamp_setpoint("LAHH_LIMIT", 60.0, 99.0)
        max_speed = self.clamp_setpoint("PUMP_MAX_SPEED", 20.0, 100.0)
        stop_sp = self.clamp_setpoint("LEVEL_STOP_SP", 20.0, lahh_limit - 2.0)
        start_sp = self.clamp_setpoint("LEVEL_START_SP", 5.0, stop_sp - 5.0)
        speed_sp = self.clamp_setpoint("PUMP_SPEED_SP", 0.0, max_speed)
        self.clamp_setpoint("LALL_LIMIT", 2.0, start_sp - 2.0)
        self.clamp_setpoint("TURB_HIGH_LIMIT", 1.0, 200.0)

        level, turbidity = self.measured("LT-101", self.level), self.measured("AT-101", self.turbidity)
        if self.reset_requested() and level < lahh_limit - 2.0:
            self.lahh = False
        if level >= lahh_limit:
            self.lahh = True

        auto = self.coil("AUTO_MODE")
        if auto:
            self.consume("PUMP_START_CMD")   # manual commands are discarded in AUTO, never latched for later
            self.consume("PUMP_STOP_CMD")
            if level < start_sp:
                self.pump_run = True
            elif level > stop_sp:
                self.pump_run = False
            self.valve_open = self.pump_run
        else:
            if self.consume("PUMP_START_CMD"):
                self.pump_run = True
            if self.consume("PUMP_STOP_CMD"):
                self.pump_run = False
            self.valve_open = self.coil("VALVE_OPEN_CMD")
        if self.lahh or self.actuator_failed("P-101"):
            self.pump_run = False
        self.speed_cmd = speed_sp if self.pump_run else 0.0

        self.interlock_word = 1 if self.lahh else 0
        self.alarm_word = (2 if turbidity > self.get("TURB_HIGH_LIMIT") else 0) | \
                          (4 if level < self.get("LALL_LIMIT") else 0)

    def physics(self, dt_h: float) -> None:
        self.speed_fb += (self.speed_cmd - self.speed_fb) * min(1.0, dt_h * 60.0)
        if self.speed_fb < 0.5:
            self.speed_fb = 0.0
        self.flow = self.PUMP_M3H * self.speed_fb / 100.0 if self.valve_open else 0.0
        draw = self.remote_value("PLC-002", "FT-201", 0.0) if (self.level > 2.0 and self.remote_ok.get("PLC-002")) else 0.0
        self.volume = max(0.0, min(self.TANK_M3, self.volume + (self.flow - draw) * dt_h))
        self.level = self.volume / self.TANK_M3 * 100.0
        hour = self.sim_hours % 24
        self.river = 2.0 + 0.3 * math.sin(self.sim_hours / 4.0) + self.noise(0.01)
        target_turb = 3.0 + (12.0 if 2.0 < (self.sim_hours % 31) < 3.5 else 0.0)  # a rain event now and then
        self.turbidity = max(0.1, self.turbidity + (target_turb - self.turbidity) * min(1.0, dt_h * 4) + self.noise(0.05))
        self.current = (12.0 + 30.0 * self.speed_fb / 100.0 + self.noise(0.2)) if self.speed_fb > 0 else 0.0
        self.draw = draw
        _ = hour

    def publish(self) -> None:
        self.set("LT-101", self.level)
        self.set("FT-101", self.flow)
        self.set("AT-101", self.turbidity)
        self.set("SC-101", self.speed_fb)
        self.set("LT-100", self.river)
        self.set("IT-101", self.current)
        self.set("FT-201-R", self.draw)
        self.set("PUMP_RUNNING", self.speed_fb > 0)
        self.set("VALVE_OPEN", self.valve_open)
        self.set("LAHH_TRIP", self.lahh)
        self.set("TURB_HIGH", self.alarm_word & 2)
        self.set("LALL_ALARM", self.alarm_word & 4)
        self.set("AUTO_ACTIVE", self.coil("AUTO_MODE"))
        self.set("REMOTE_OK_002", self.remote_ok.get("PLC-002", False))


# =============================================================================
class Treatment(Program):
    name = "treatment"
    description = "Treatment: transfer pump P-202, sand filter F-201, chlorine dosing pump P-201"
    tags = [
        Tag("FT-201", "input", 0, 10, "m3/h", "Treated water flow"),
        Tag("AT-201", "input", 1, 100, "mg/L", "Free chlorine residual"),
        Tag("AT-202", "input", 2, 100, "pH", "pH"),
        Tag("PDT-201", "input", 3, 10, "kPa", "Filter differential pressure"),
        Tag("LT-201", "input", 4, 100, "%", "Contact tank level"),
        Tag("SC-201", "input", 5, 100, "%", "P-201 dosing stroke feedback"),
        Tag("LT-101-R", "input", 6, 100, "%", "Intake tank level (remote from PLC-001)"),
        Tag("LT-301-R", "input", 7, 100, "%", "Clearwell level (remote from PLC-003)"),
        Tag("SEQ_STEP", "input", 8, 1, "", "Plant sequence step: 0 stopped, 1 wait upstream, 2 filling, 3 running, 4 stopping, 5 held"),
        Tag("CL2_SP", "holding", 100, 100, "mg/L", "Chlorine residual setpoint", "operator"),
        Tag("FLOW_SP", "holding", 101, 10, "m3/h", "Treatment flow setpoint", "operator"),
        Tag("BACKWASH_DP_SP", "holding", 102, 10, "kPa", "Backwash trigger dP", "operator"),
        Tag("MANUAL_STROKE_SP", "holding", 103, 100, "%", "Manual dosing stroke", "operator"),
        Tag("AAHH_LIMIT", "holding", 200, 100, "mg/L", "Chlorine high-high interlock", "engineer"),
        Tag("AALL_LIMIT", "holding", 201, 100, "mg/L", "Chlorine low alarm", "engineer"),
        Tag("PI_KP", "holding", 202, 100, "", "Dosing controller gain", "engineer"),
        Tag("PI_TI", "holding", 203, 10, "s", "Dosing controller integral time", "engineer"),
        Tag("MAX_STROKE", "holding", 204, 100, "%", "Dosing pump maximum stroke", "engineer"),
        Tag("CLEARWELL_STOP", "holding", 205, 100, "%", "Stop transfer above clearwell level", "engineer"),
        Tag("CLEARWELL_RESTART", "holding", 206, 100, "%", "Restart transfer below clearwell level", "engineer"),
        Tag("AUTO_MODE", "coils", 0, 1, "", "Automatic dosing control", "operator"),
        Tag("DOSING_ENABLE_CMD", "coils", 1, 1, "", "Manual: dosing pump enable", "operator"),
        Tag("BACKWASH_CMD", "coils", 2, 1, "", "Start filter backwash", "operator"),
        Tag("TRANSFER_RUN_CMD", "coils", 3, 1, "", "Manual: transfer pump run", "operator"),
        Tag("PLANT_START_CMD", "coils", 5, 1, "", "Start the plant sequence", "supervisor"),
        Tag("PLANT_STOP_CMD", "coils", 6, 1, "", "Stop the plant sequence", "supervisor"),
        Tag("DOSING_RUNNING", "discrete", 0, 1, "", "P-201 dosing"),
        Tag("BACKWASH_ACTIVE", "discrete", 1, 1, "", "Backwash in progress"),
        Tag("AAHH_TRIP", "discrete", 2, 1, "", "Interlock: chlorine high-high"),
        Tag("AALL_ALARM", "discrete", 3, 1, "", "Alarm: chlorine low"),
        Tag("DP_HIGH", "discrete", 4, 1, "", "Alarm: filter dP high"),
        Tag("AUTO_ACTIVE", "discrete", 5, 1, "", "Automatic mode active"),
        Tag("REMOTE_OK_001", "discrete", 6, 1, "", "Link to PLC-001 healthy"),
        Tag("TRANSFER_RUNNING", "discrete", 7, 1, "", "P-202 transfer pump running"),
        Tag("REMOTE_OK_003", "discrete", 8, 1, "", "Link to PLC-003 healthy"),
        Tag("CLEARWELL_HOLD", "discrete", 9, 1, "", "Transfer held: clearwell high"),
        Tag("PLANT_RUNNING", "discrete", 10, 1, "", "Plant sequence in RUN"),
    ]
    SEQ_STOPPED, SEQ_WAIT, SEQ_FILL, SEQ_RUN, SEQ_STOP, SEQ_HELD = 0, 1, 2, 3, 4, 5
    remote_tags = {"PLC-001": [("LT-101", "input", 0, 100)], "PLC-003": [("LT-301", "input", 0, 100)]}

    def defaults(self) -> None:
        self.flow = 0.0
        self.residual = 1.4
        self.ph = 7.2
        self.dp = 12.0
        self.contact_level = 55.0
        self.stroke = 30.0
        self.dosing_run = False
        self.transfer_run = False
        self.backwash_left = 0.0
        self.aahh = False
        self.prev_error = 0.0
        self.clearwell_hold = False
        self.upstream_level = 0.0
        self.clearwell_level = 0.0
        self.seq = self.SEQ_RUN            # the plant is delivered running; supervisors can stop and restart it
        self.seq_timer = 0.0
        self.set("CL2_SP", 1.50)
        self.set("FLOW_SP", 120.0)
        self.set("BACKWASH_DP_SP", 60.0)
        self.set("MANUAL_STROKE_SP", 40.0)
        self.set("AAHH_LIMIT", 4.00)
        self.set("AALL_LIMIT", 0.50)
        self.set("PI_KP", 2.00)
        self.set("PI_TI", 60.0)
        self.set("MAX_STROKE", 100.0)
        self.set("CLEARWELL_STOP", 88.0)
        self.set("CLEARWELL_RESTART", 70.0)
        self.set("AUTO_MODE", 1)

    def logic(self, dt_h: float) -> None:
        aahh = self.clamp_setpoint("AAHH_LIMIT", 2.0, 8.0)
        cl2_sp = self.clamp_setpoint("CL2_SP", 0.2, aahh - 0.5)
        flow_sp = self.clamp_setpoint("FLOW_SP", 20.0, 200.0)
        bw_sp = self.clamp_setpoint("BACKWASH_DP_SP", 20.0, 100.0)
        max_stroke = self.clamp_setpoint("MAX_STROKE", 10.0, 100.0)
        manual_stroke = self.clamp_setpoint("MANUAL_STROKE_SP", 0.0, max_stroke)
        self.clamp_setpoint("AALL_LIMIT", 0.1, cl2_sp)
        kp = self.clamp_setpoint("PI_KP", 0.1, 20.0)
        ti = self.clamp_setpoint("PI_TI", 5.0, 600.0)

        residual, dp, flow = self.measured("AT-201", self.residual), self.measured("PDT-201", self.dp), self.measured("FT-201", self.flow)
        if self.reset_requested() and residual < aahh - 0.5:
            self.aahh = False
        if residual >= aahh:
            self.aahh = True

        upstream_level = self.remote_value("PLC-001", "LT-101", 0.0)
        available = upstream_level > 3.0 and bool(self.remote_ok.get("PLC-001"))
        cw_stop = self.clamp_setpoint("CLEARWELL_STOP", 50.0, 97.0)
        cw_restart = self.clamp_setpoint("CLEARWELL_RESTART", 20.0, cw_stop - 5.0)
        clearwell = self.remote_value("PLC-003", "LT-301", 50.0)
        if clearwell >= cw_stop:
            self.clearwell_hold = True
        elif clearwell <= cw_restart:
            self.clearwell_hold = False
        auto = self.coil("AUTO_MODE")

        if self.consume("BACKWASH_CMD") and self.backwash_left <= 0:
            self.backwash_left = 0.1
        if dp >= bw_sp and self.backwash_left <= 0:
            self.backwash_left = 0.1   # filter protection runs in AUTO and MANUAL alike
        backwash = self.backwash_left > 0

        start_cmd, stop_cmd = self.consume("PLANT_START_CMD"), self.consume("PLANT_STOP_CMD")
        if auto:
            self._sequence(dt_h, start_cmd, stop_cmd, available, flow)
            self.transfer_run = self.seq in (self.SEQ_FILL, self.SEQ_RUN) and available and not backwash and not self.clearwell_hold
        else:
            self.transfer_run = self.coil("TRANSFER_RUN_CMD") and available and not backwash
        self.flow_cmd = flow_sp if self.transfer_run else 0.0

        dosing_wanted = flow > 5.0 and (not auto or self.seq == self.SEQ_RUN)
        if auto:
            self.dosing_run = dosing_wanted and not self.aahh
            if self.dosing_run:
                dt_s = dt_h * 3600.0
                error = cl2_sp - residual
                self.stroke += (kp * (error - self.prev_error) + kp / ti * error * dt_s) * 10.0
                self.prev_error = error
                self.stroke = max(0.0, min(max_stroke, self.stroke))
        else:
            self.dosing_run = self.coil("DOSING_ENABLE_CMD") and dosing_wanted and not self.aahh
            self.stroke = manual_stroke
        if self.actuator_failed("P-201"):
            self.dosing_run = False
        if self.actuator_failed("P-202"):
            self.transfer_run = False
            self.flow_cmd = 0.0
        if not self.dosing_run:
            self.prev_error = 0.0

        self.interlock_word = 1 if self.aahh else 0
        low = self.dosing_run and residual < self.get("AALL_LIMIT")
        self.alarm_word = (2 if low else 0) | (4 if dp > bw_sp * 0.9 else 0)

    def _sequence(self, dt_h: float, start: bool, stop: bool, available: bool, flow: float) -> None:
        """Plant start/stop sequence (supervisor commands): STOPPED -> WAIT UPSTREAM -> FILL -> RUN -> STOP."""
        flow_sp = self.get("FLOW_SP")
        if stop and self.seq not in (self.SEQ_STOPPED, self.SEQ_STOP):
            self.seq, self.seq_timer = self.SEQ_STOP, 0.0
        elif start and self.seq == self.SEQ_STOPPED:
            self.seq, self.seq_timer = self.SEQ_WAIT, 0.0
        if self.seq == self.SEQ_WAIT:
            if available and self.upstream_level > 15.0:
                self.seq, self.seq_timer = self.SEQ_FILL, 0.0
        elif self.seq == self.SEQ_FILL:
            self.seq_timer += dt_h
            if flow > 0.8 * flow_sp:
                self.seq, self.seq_timer = self.SEQ_RUN, 0.0
            elif self.seq_timer > 1.0:
                self.seq = self.SEQ_HELD               # could not establish flow within one simulated hour
        elif self.seq == self.SEQ_RUN:
            if not available:
                self.seq, self.seq_timer = self.SEQ_HELD, 0.0
        elif self.seq == self.SEQ_HELD:
            if available and self.upstream_level > 15.0:
                self.seq, self.seq_timer = self.SEQ_FILL, 0.0
        elif self.seq == self.SEQ_STOP:
            self.seq_timer += dt_h
            if flow < 1.0 or self.seq_timer > 0.2:
                self.seq = self.SEQ_STOPPED

    def physics(self, dt_h: float) -> None:
        if self.backwash_left > 0:
            self.backwash_left -= dt_h
            self.dp = max(8.0, self.dp - 600.0 * dt_h)
        self.flow += (self.flow_cmd - self.flow) * min(1.0, dt_h * 30.0)
        if self.flow < 0.5:
            self.flow = 0.0
        applied = (self.stroke / 100.0) * 6.0 * (120.0 / max(self.flow, 10.0)) if (self.dosing_run and self.flow > 0) else 0.0
        demand = 0.8 + self.noise(0.02)
        eq = max(0.0, applied - demand) if self.flow > 0 else self.residual * 0.98
        self.residual = max(0.0, self.residual + (eq - self.residual) * min(1.0, dt_h / 0.2) + self.noise(0.005))
        self.ph = max(6.5, min(8.5, self.ph + (7.2 - self.ph) * dt_h + self.noise(0.005)))
        if self.backwash_left <= 0:
            self.dp += self.flow * dt_h * 0.15 + self.noise(0.02)
        self.dp = max(8.0, self.dp)
        target_level = 40.0 + 30.0 * self.flow / 120.0
        self.contact_level += (target_level - self.contact_level) * min(1.0, dt_h * 6) + self.noise(0.05)
        self.upstream_level = self.remote_value("PLC-001", "LT-101", 0.0)
        self.clearwell_level = self.remote_value("PLC-003", "LT-301", 0.0)

    def publish(self) -> None:
        self.set("FT-201", self.flow)
        self.set("AT-201", self.residual)
        self.set("AT-202", self.ph)
        self.set("PDT-201", self.dp)
        self.set("LT-201", self.contact_level)
        self.set("SC-201", self.stroke if self.dosing_run else 0.0)
        self.set("LT-101-R", self.upstream_level)
        self.set("LT-301-R", self.clearwell_level)
        self.set("SEQ_STEP", self.seq)
        self.set("PLANT_RUNNING", self.seq == self.SEQ_RUN)
        self.set("DOSING_RUNNING", self.dosing_run)
        self.set("BACKWASH_ACTIVE", self.backwash_left > 0)
        self.set("AAHH_TRIP", self.aahh)
        self.set("AALL_ALARM", self.alarm_word & 2)
        self.set("DP_HIGH", self.alarm_word & 4)
        self.set("AUTO_ACTIVE", self.coil("AUTO_MODE"))
        self.set("REMOTE_OK_001", self.remote_ok.get("PLC-001", False))
        self.set("TRANSFER_RUNNING", self.transfer_run)      # pump command state, not flow: the invariant checks flow
        self.set("REMOTE_OK_003", self.remote_ok.get("PLC-003", False))
        self.set("CLEARWELL_HOLD", self.clearwell_hold)


# =============================================================================
class Distribution(Program):
    name = "distribution"
    description = "Distribution: clearwell T-301, high-lift pumps P-301/P-302, network pressure control"
    tags = [
        Tag("LT-301", "input", 0, 100, "%", "Clearwell T-301 level"),
        Tag("PT-301", "input", 1, 100, "bar", "Network pressure"),
        Tag("FT-301", "input", 2, 10, "m3/h", "Distribution flow"),
        Tag("SC-301", "input", 3, 100, "%", "P-301 VFD speed"),
        Tag("SC-302", "input", 4, 100, "%", "P-302 VFD speed"),
        Tag("FQ-301", "input", 5, 10, "m3/h", "Estimated network demand"),
        Tag("FT-201-R", "input", 6, 10, "m3/h", "Treated inflow (remote from PLC-002)"),
        Tag("PRESSURE_SP", "holding", 100, 100, "bar", "Network pressure setpoint", "operator"),
        Tag("LAG_START_DELTA", "holding", 101, 100, "bar", "Lag pump start below SP by", "operator"),
        Tag("LALL_LIMIT", "holding", 200, 100, "%", "Clearwell low-low interlock", "engineer"),
        Tag("PAHH_LIMIT", "holding", 201, 100, "bar", "Pressure high-high interlock", "engineer"),
        Tag("PALL_LIMIT", "holding", 202, 100, "bar", "Pressure low alarm", "engineer"),
        Tag("MAX_SPEED", "holding", 203, 100, "%", "VFD maximum speed", "engineer"),
        Tag("AUTO_MODE", "coils", 0, 1, "", "Automatic pressure control", "operator"),
        Tag("P301_RUN_CMD", "coils", 1, 1, "", "Manual: P-301 run", "operator"),
        Tag("P302_RUN_CMD", "coils", 2, 1, "", "Manual: P-302 run", "operator"),
        Tag("STOP_ALL_CMD", "coils", 3, 1, "", "Manual: stop all pumps", "operator"),
        Tag("P301_RUNNING", "discrete", 0, 1, "", "P-301 running"),
        Tag("P302_RUNNING", "discrete", 1, 1, "", "P-302 running"),
        Tag("LALL_TRIP", "discrete", 2, 1, "", "Interlock: clearwell low-low"),
        Tag("PAHH_TRIP", "discrete", 3, 1, "", "Interlock: pressure high-high"),
        Tag("PALL_ALARM", "discrete", 4, 1, "", "Alarm: pressure low"),
        Tag("AUTO_ACTIVE", "discrete", 5, 1, "", "Automatic mode active"),
        Tag("REMOTE_OK_002", "discrete", 6, 1, "", "Link to PLC-002 healthy"),
    ]
    remote_tags = {"PLC-002": [("FT-201", "input", 0, 10)]}
    TANK_M3 = 800.0
    PUMP_M3H = 110.0

    def defaults(self) -> None:
        self.volume = 0.6 * self.TANK_M3
        self.level = 60.0
        self.pressure = 3.8
        self.flow = 0.0
        self.demand = 60.0
        self.speed = [0.0, 0.0]
        self.run = [False, False]
        self.speed_cmd = 0.0
        self.lag_timer = 0
        self.lall = False
        self.pahh = False
        self.integral = 0.0
        self.set("PRESSURE_SP", 4.00)
        self.set("LAG_START_DELTA", 0.50)
        self.set("LALL_LIMIT", 10.0)
        self.set("PAHH_LIMIT", 6.00)
        self.set("PALL_LIMIT", 2.50)
        self.set("MAX_SPEED", 100.0)
        self.set("AUTO_MODE", 1)

    def logic(self, dt_h: float) -> None:
        pahh = self.clamp_setpoint("PAHH_LIMIT", 3.0, 8.0)
        sp = self.clamp_setpoint("PRESSURE_SP", 2.0, pahh - 1.0)
        delta = self.clamp_setpoint("LAG_START_DELTA", 0.1, 2.0)
        lall = self.clamp_setpoint("LALL_LIMIT", 2.0, 40.0)
        max_speed = self.clamp_setpoint("MAX_SPEED", 30.0, 100.0)
        self.clamp_setpoint("PALL_LIMIT", 0.5, sp - 0.5)
        level, pressure = self.measured("LT-301", self.level), self.measured("PT-301", self.pressure)

        if self.reset_requested():
            if level > lall + 5.0:
                self.lall = False
            if pressure < pahh - 0.5:
                self.pahh = False
        if level <= lall:
            self.lall = True
        if pressure >= pahh:
            self.pahh = True
        tripped = self.lall or self.pahh

        auto = self.coil("AUTO_MODE")
        if auto:
            self.consume("STOP_ALL_CMD")
            self.run[0] = not tripped
            error = sp - pressure
            dt_s = dt_h * 3600.0
            self.integral = max(-50.0, min(50.0, self.integral + error * dt_s * 0.05))
            self.speed_cmd = max(0.0, min(max_speed, 70.0 + error * 25.0 + self.integral))
            if pressure < sp - delta and self.run[0]:
                self.lag_timer += 1
            elif pressure >= sp - delta / 4 and self.demand < self.PUMP_M3H * 0.8:
                self.lag_timer -= 1   # one pump covers the demand with 20% margin: the lag pump can drop out
            self.lag_timer = max(-40, min(10, self.lag_timer))
            if self.lag_timer >= 10:
                self.run[1] = True
            if self.lag_timer <= -40:
                self.run[1] = False
            self.run[1] = self.run[1] and not tripped
        else:
            if self.consume("STOP_ALL_CMD"):
                self.set("P301_RUN_CMD", 0)
                self.set("P302_RUN_CMD", 0)
            self.run[0] = self.coil("P301_RUN_CMD") and not tripped
            self.run[1] = self.coil("P302_RUN_CMD") and not tripped
            self.speed_cmd = max_speed * 0.85
        if tripped:
            self.run = [False, False]
        if self.actuator_failed("P-301"):
            self.run[0] = False
        if self.actuator_failed("P-302"):
            self.run[1] = False

        self.interlock_word = (1 if self.lall else 0) | (2 if self.pahh else 0)
        self.alarm_word = 4 if (pressure < self.get("PALL_LIMIT") and any(self.run)) else 0

    def physics(self, dt_h: float) -> None:
        hour = self.sim_hours % 24
        base = 60.0 + 35.0 * math.sin(2 * math.pi * (hour - 8.0) / 24.0) + 20.0 * math.sin(4 * math.pi * (hour - 8.0) / 24.0)
        self.demand = max(20.0, min(160.0, base + self.noise(1.0)))
        for i in range(2):
            target = self.speed_cmd if self.run[i] else 0.0
            self.speed[i] += (target - self.speed[i]) * min(1.0, dt_h * 60.0)
            if self.speed[i] < 0.5:
                self.speed[i] = 0.0
        capacity = sum(self.PUMP_M3H * s / 100.0 for s in self.speed) if self.level > 0.5 else 0.0
        head = max((6.5 * (s / 100.0) ** 2 for s in self.speed), default=0.0)
        self.flow = min(capacity, self.demand)
        deficit = max(0.0, self.demand - capacity)
        target_p = max(0.0, head - 0.00012 * self.flow ** 2 - deficit * 0.02)
        self.pressure += (target_p - self.pressure) * min(1.0, dt_h * 40.0) + self.noise(0.005)
        self.pressure = max(0.0, self.pressure)
        inflow = self.remote_value("PLC-002", "FT-201", 0.0) if self.remote_ok.get("PLC-002") else 0.0
        self.volume = max(0.0, min(self.TANK_M3, self.volume + (inflow - self.flow) * dt_h))
        self.level = self.volume / self.TANK_M3 * 100.0
        self.inflow = inflow

    def publish(self) -> None:
        self.set("LT-301", self.level)
        self.set("PT-301", self.pressure)
        self.set("FT-301", self.flow)
        self.set("SC-301", self.speed[0])
        self.set("SC-302", self.speed[1])
        self.set("FQ-301", self.demand)
        self.set("FT-201-R", self.inflow)
        self.set("P301_RUNNING", self.speed[0] > 0)
        self.set("P302_RUNNING", self.speed[1] > 0)
        self.set("LALL_TRIP", self.lall)
        self.set("PAHH_TRIP", self.pahh)
        self.set("PALL_ALARM", self.alarm_word & 4)
        self.set("AUTO_ACTIVE", self.coil("AUTO_MODE"))
        self.set("REMOTE_OK_002", self.remote_ok.get("PLC-002", False))


PROGRAMS = {"intake": Intake, "treatment": Treatment, "distribution": Distribution}
