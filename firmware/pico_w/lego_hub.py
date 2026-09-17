"""Hardware abstraction for whatever actually drives the motor.

The rest of the firmware only ever calls the LEGOHubController interface:

    apply(speed, direction)   # speed 0-100, direction "forward"/"backward"
    coast()                   # release the motor
    brake()                   # actively stop
    battery_voltage()         # volts, or None if not measurable
    connected()               # is the hub reachable

Two implementations ship here:

* PWMHubController - drives an H-bridge from the Pico's GPIO. This is the
  wiring most people use to retrofit a LEGO 9V/PF motor, and it works today.
* MockHubController - prints what it would do. The whole system is fully
  functional with this, which is how you develop without hardware.

If you are driving a Powered Up / Control+ hub over Bluetooth instead, add a
third class here implementing the same four methods. Nothing else in the
firmware changes: that is the entire point of this file.
"""

try:
    from machine import ADC, PWM, Pin
except ImportError:  # running on a desktop for linting/tests
    ADC = PWM = Pin = None  # type: ignore


class LEGOHubController:
    """Interface every hub driver implements."""

    def apply(self, speed, direction):
        raise NotImplementedError

    def coast(self):
        raise NotImplementedError

    def brake(self):
        raise NotImplementedError

    def battery_voltage(self):
        return None

    def connected(self):
        return True

    def close(self):
        pass


class MockHubController(LEGOHubController):
    """No hardware. Records the last command so telemetry stays truthful."""

    def __init__(self, log=print):
        self._log = log
        self.speed = 0
        self.direction = "forward"

    def apply(self, speed, direction):
        self.speed = speed
        self.direction = direction
        self._log("[mock hub] {} at {}".format(direction, speed))

    def coast(self):
        self.speed = 0
        self._log("[mock hub] coast")

    def brake(self):
        self.speed = 0
        self._log("[mock hub] brake")

    def battery_voltage(self):
        return 7.4

    def connected(self):
        return True


class PWMHubController(LEGOHubController):
    """H-bridge driver: one PWM pin for speed, two pins for direction.

    Wiring (DRV8833 / L298N style):
        MOTOR_PWM_PIN   -> ENA / nSLEEP
        MOTOR_DIR_A_PIN -> IN1
        MOTOR_DIR_B_PIN -> IN2
        motor           -> OUT1 / OUT2
    """

    def __init__(self, config):
        if PWM is None:  # pragma: no cover - desktop guard
            raise RuntimeError("PWMHubController requires MicroPython on a Pico")
        self._min_duty = config.MOTOR_MIN_DUTY
        self._max_duty = config.MOTOR_MAX_DUTY
        self._pwm = PWM(Pin(config.MOTOR_PWM_PIN))
        self._pwm.freq(config.MOTOR_PWM_FREQ_HZ)
        self._pwm.duty_u16(0)
        self._dir_a = Pin(config.MOTOR_DIR_A_PIN, Pin.OUT, value=0)
        self._dir_b = Pin(config.MOTOR_DIR_B_PIN, Pin.OUT, value=0)
        self._adc = None
        self._divider = config.BATTERY_DIVIDER_RATIO
        if config.BATTERY_ADC_PIN is not None:
            self._adc = ADC(Pin(config.BATTERY_ADC_PIN))

    def apply(self, speed, direction):
        speed = max(0, min(100, int(speed)))
        if speed == 0:
            self.brake()
            return
        forward = direction != "backward"
        self._dir_a.value(1 if forward else 0)
        self._dir_b.value(0 if forward else 1)
        duty_percent = self._min_duty + (self._max_duty - self._min_duty) * speed / 100.0
        self._pwm.duty_u16(int(duty_percent * 65535 / 100))

    def coast(self):
        self._pwm.duty_u16(0)
        self._dir_a.value(0)
        self._dir_b.value(0)

    def brake(self):
        # Both direction pins high shorts the motor terminals: active braking.
        self._pwm.duty_u16(0)
        self._dir_a.value(1)
        self._dir_b.value(1)

    def battery_voltage(self):
        if self._adc is None:
            return None
        raw = self._adc.read_u16()
        return round(raw / 65535 * 3.3 * self._divider, 2)

    def connected(self):
        return True

    def close(self):
        self.coast()
        self._pwm.deinit()


def create_hub(config, log=print):
    """Factory chosen by config.HUB_DRIVER."""
    driver = getattr(config, "HUB_DRIVER", "mock")
    if driver == "pwm":
        try:
            return PWMHubController(config)
        except Exception as exc:
            log("Falling back to the mock hub: {}".format(exc))
            return MockHubController(log=log)
    if driver == "mock":
        return MockHubController(log=log)
    raise ValueError("Unknown HUB_DRIVER: {}".format(driver))
