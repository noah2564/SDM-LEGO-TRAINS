"""Train logic, independent of both MQTT and the hub hardware.

TrainController owns the safety-critical state: current speed, direction, the
emergency-stop latch and the communication watchdog. It is deliberately free
of any networking code so the same logic runs on the bench with a mock hub.
"""

try:
    import utime as time
except ImportError:
    import time


class TrainController:
    def __init__(self, hub, max_speed=100, safety_timeout_s=6.0, log=print):
        self._hub = hub
        self._log = log
        self.max_speed = max_speed
        self.safety_timeout_s = safety_timeout_s

        self.speed = 0
        self.direction = "forward"
        self.emergency_stop = False
        self.last_contact = time.time()
        self.last_fault = None

    # -- control surface -------------------------------------------------
    def set_speed(self, speed):
        """Set target speed 0-100. Ignored while the e-stop latch is set."""
        if self.emergency_stop:
            self._log("set_speed ignored: emergency stop is latched")
            return False
        try:
            speed = int(speed)
        except (TypeError, ValueError):
            self._log("set_speed ignored: bad value")
            return False
        self.speed = max(0, min(self.max_speed, speed))
        self._hub.apply(self.speed, self.direction)
        return True

    def set_direction(self, direction):
        if direction not in ("forward", "backward"):
            self._log("set_direction ignored: bad value")
            return False
        if direction != self.direction and self.speed > 0:
            # Never reverse a moving train: stop first.
            self.speed = 0
            self._hub.brake()
        self.direction = direction
        self._hub.apply(self.speed, self.direction)
        return True

    def stop(self):
        """Normal stop. The train can be driven again immediately."""
        self.speed = 0
        self._hub.brake()
        return True

    def emergency_stop_now(self, reason="emergency stop"):
        """Cut power and latch. Only clear_emergency() releases it."""
        self.speed = 0
        self.emergency_stop = True
        self.last_fault = reason
        self._hub.brake()
        self._log("EMERGENCY STOP: {}".format(reason))
        return True

    def clear_emergency(self):
        self.emergency_stop = False
        self.last_fault = None
        self.speed = 0
        self._hub.brake()
        self._log("Emergency stop cleared")
        return True

    # -- watchdog ---------------------------------------------------------
    def note_server_contact(self):
        """Call on every message received from the broker."""
        self.last_contact = time.time()

    def check_safety(self):
        """Stop the train if the server has gone quiet.

        Returns True when it just tripped, so the caller can report it.
        """
        silent_for = time.time() - self.last_contact
        if silent_for < self.safety_timeout_s:
            return False
        if self.speed == 0 and not self._hub_is_driving():
            return False
        self._log("Safety timeout after {:.0f}s without contact".format(silent_for))
        self.speed = 0
        self._hub.brake()
        self.last_fault = "communication lost"
        return True

    def _hub_is_driving(self):
        return getattr(self._hub, "speed", 0) > 0

    def on_connection_lost(self):
        """Called the moment Wi-Fi or MQTT drops. Stop immediately."""
        if self.speed > 0:
            self._log("Link lost while moving - braking")
        self.speed = 0
        self._hub.brake()
        self.last_fault = "link lost"

    # -- telemetry --------------------------------------------------------
    def telemetry(self):
        return {
            "speed": self.speed,
            "direction": self.direction,
            "emergency_stop": self.emergency_stop,
            "battery": self._hub.battery_voltage(),
            "hub_connected": self._hub.connected(),
            "connected": True,
        }

    def handle_command(self, payload):
        """Apply a validated-by-the-backend command. Returns (ok, event).

        The firmware still rejects anything it does not understand: the
        backend is trusted to be sane, not trusted to be perfect.
        """
        command = payload.get("command")
        if command == "emergency_stop":
            self.emergency_stop_now(payload.get("reason", "emergency stop"))
            return True, ("emergency_stop", payload.get("reason", "emergency stop"), "warning")
        if command == "clear_emergency":
            self.clear_emergency()
            return True, ("emergency_cleared", "Ready to drive", "info")
        if command == "stop":
            self.stop()
            return True, None
        if command == "set_speed":
            direction = payload.get("direction")
            if direction:
                self.set_direction(direction)
            ok = self.set_speed(payload.get("speed", 0))
            return ok, None
        if command == "set_direction":
            return self.set_direction(payload.get("direction")), None
        if command == "ping":
            return True, None
        if command == "set_config":
            timeout = payload.get("safety_timeout_s")
            if timeout:
                self.safety_timeout_s = float(timeout)
            return True, None
        self._log("Unknown command: {}".format(command))
        return False, ("unknown_command", str(command), "warning")
