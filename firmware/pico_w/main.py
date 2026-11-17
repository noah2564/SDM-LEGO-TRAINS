"""Pico W train controller - single-file main.py.

Combines the MQTT/Wi-Fi train node with the LWP3 (LEGO Powered Up) BLE hub
driver. Every time the Pico receives a command from the backend over MQTT,
it immediately forwards the resulting motor state to the LEGO hub over BLE.

Priorities:
  1. Keep the train safe: stop when the backend, Wi-Fi or hub link goes quiet.
  2. Stay connected to Wi-Fi, MQTT and the hub, reconnecting forever.
  3. Execute commands and report telemetry.

Files needed on the Pico: config.py, mqtt_client.py (TrainMqttClient),
umqtt.simple and this main.py.

Optional config.py settings used here (defaults in brackets):
  HUB_ADDRESS     BLE address "AA:BB:CC:DD:EE:FF" of the hub  [None = strongest LEGO hub]
  MOTOR_PORTS     ports driven together                        [("A", "B")]
  MOTOR_INVERT    ports whose direction is flipped             [()]
  BLE_SCAN_MS     scan duration                                [5000]

Expected command payloads (dict, or JSON text/bytes):
  {"command": "set_speed", "speed": -100..100}   (also "action"; "speed" alone works)
  {"command": "stop"}  /  "brake"  /  "emergency_stop"
  An optional "direction": "reverse" flips the sign of "speed".
  payload None (server heartbeat) only resets the safety watchdog.
"""

import sys

import bluetooth
import machine
import network
from micropython import const

try:
    import utime as time
except ImportError:
    import time

try:
    import json
except ImportError:
    import ujson as json

import config
from mqtt_client import TrainMqttClient

from machine import Pin, I2C, ADC, PWM
# light sensor disabled
# light  = ADC(27)
# print(int(light.read_u16()))
# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------
LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}
_threshold = LEVELS.get(getattr(config, "LOG_LEVEL", "INFO"), 20)


def log(message, level="INFO"):
    if LEVELS.get(level, 20) >= _threshold:
        print("[{:>8}] {} {}".format(time.ticks_ms() / 1000, level, message))


# --------------------------------------------------------------------------
# LWP3 BLE hub driver
# --------------------------------------------------------------------------
LWP3_SERVICE = bluetooth.UUID("00001623-1212-efde-1623-785feabcd123")
LWP3_CHAR = bluetooth.UUID("00001624-1212-efde-1623-785feabcd123")
LEGO_COMPANY_ID = 0x0397

_IRQ_SCAN_RESULT = const(5)
_IRQ_SCAN_DONE = const(6)
_IRQ_PERIPHERAL_CONNECT = const(7)
_IRQ_PERIPHERAL_DISCONNECT = const(8)
_IRQ_GATTC_SERVICE_RESULT = const(9)
_IRQ_GATTC_SERVICE_DONE = const(10)
_IRQ_GATTC_CHARACTERISTIC_RESULT = const(11)
_IRQ_GATTC_CHARACTERISTIC_DONE = const(12)
_IRQ_GATTC_WRITE_DONE = const(17)
_IRQ_GATTC_NOTIFY = const(18)

_PROP_WRITE = const(0x08)
_PROP_WRITE_NO_RESPONSE = const(0x04)
_PROP_NOTIFY = const(0x10)

MAX_NOTIFICATION_SIZE = const(64)
NOTIFICATION_QUEUE_SIZE = const(8)
NOTIFICATION_RECORD_SIZE = const(MAX_NOTIFICATION_SIZE + 1)
OPERATION_TIMEOUT_MS = 10000

# 128-bit UUIDs in BLE advertising structures are little-endian.
LWP3_SERVICE_ADV = (
    0x23, 0xD1, 0xBC, 0xEA, 0x5F, 0x78, 0x23, 0x16,
    0xDE, 0xEF, 0x12, 0x12, 0x23, 0x16, 0x00, 0x00,
)

PORT_LETTERS = {"A": 0, "B": 1, "C": 2, "D": 3}


def hx(data):
    return " ".join("%02x" % value for value in data)


def parse_port(value):
    if isinstance(value, int):
        return value
    upper = str(value).upper()
    return PORT_LETTERS[upper] if upper in PORT_LETTERS else int(value, 0)


def is_lego_advertisement(data):
    """Check AD fields without creating objects, for use in the BLE IRQ."""
    offset = 0
    while offset < len(data):
        field_length = data[offset]
        if field_length == 0:
            break
        end = offset + 1 + field_length
        if end > len(data):
            break
        field_type = data[offset + 1]
        value_start = offset + 2
        value_length = field_length - 1
        if (
            field_type == 0xFF
            and value_length >= 2
            and data[value_start] == (LEGO_COMPANY_ID & 0xFF)
            and data[value_start + 1] == (LEGO_COMPANY_ID >> 8)
        ):
            return True
        if field_type in (0x06, 0x07):
            uuid_offset = value_start
            while uuid_offset + 16 <= end:
                matches = True
                for index in range(16):
                    if data[uuid_offset + index] != LWP3_SERVICE_ADV[index]:
                        matches = False
                        break
                if matches:
                    return True
                uuid_offset += 16
        offset = end
    return False


class Lwp3Hub:
    """Minimal LWP3 BLE central: finds a hub, connects, drives motors."""

    def __init__(self, log_fn, target_address=None, scan_ms=5000):
        self._log = log_fn
        self.scan_ms = scan_ms
        self.ble = bluetooth.BLE()
        self.ble.active(True)
        self.ble.irq(self._irq)

        self.target_address = None
        if target_address:
            text = target_address.replace(":", "").replace("-", "")
            if len(text) != 12:
                raise ValueError("HUB_ADDRESS must contain six hexadecimal bytes")
            self.target_address = bytes(
                int(text[i:i + 2], 16) for i in range(0, 12, 2)
            )

        # Best scan candidate (filled in the IRQ without allocating).
        self._cand = bytearray(7)  # addr_type + 6 address bytes
        self._cand_rssi = -128
        self._cand_found = False
        self.scan_done = False

        self.connected = False
        self.disconnected = False
        self.conn_handle = None
        self.service_done = False
        self.service_start = 0
        self.service_end = 0
        self.char_done = False
        self.char_handle = None
        self.char_properties = 0
        self.ports_used = set()
        self.battery = None

        self._nbuf = bytearray(NOTIFICATION_QUEUE_SIZE * NOTIFICATION_RECORD_SIZE)
        self._nread = 0
        self._nwrite = 0
        self.write_error = 0

    # -- BLE interrupt handler ---------------------------------------------
    def _irq(self, event, data):
        if event == _IRQ_SCAN_RESULT:
            addr_type, addr, _, rssi, adv_data = data
            is_target = self.target_address is not None
            if is_target:
                for i in range(6):
                    if addr[i] != self.target_address[i]:
                        is_target = False
                        break
            if not (is_target or (self.target_address is None
                                  and is_lego_advertisement(adv_data))):
                return
            if self._cand_found and rssi <= self._cand_rssi and not is_target:
                return
            self._cand[0] = addr_type
            for i in range(6):
                self._cand[1 + i] = addr[i]
            self._cand_rssi = rssi
            self._cand_found = True
        elif event == _IRQ_SCAN_DONE:
            self.scan_done = True
        elif event == _IRQ_PERIPHERAL_CONNECT:
            self.conn_handle, _, _ = data
            self.connected = True
            self.disconnected = False
        elif event == _IRQ_PERIPHERAL_DISCONNECT:
            self.conn_handle, _, _ = data
            self.connected = False
            self.disconnected = True
        elif event == _IRQ_GATTC_SERVICE_RESULT:
            conn_handle, start_handle, end_handle, uuid = data
            if conn_handle == self.conn_handle and uuid == LWP3_SERVICE:
                self.service_start = start_handle
                self.service_end = end_handle
        elif event == _IRQ_GATTC_SERVICE_DONE:
            self.service_done = True
        elif event == _IRQ_GATTC_CHARACTERISTIC_RESULT:
            conn_handle, _, value_handle, properties, uuid = data
            if conn_handle == self.conn_handle and uuid == LWP3_CHAR:
                self.char_handle = value_handle
                self.char_properties = properties
        elif event == _IRQ_GATTC_CHARACTERISTIC_DONE:
            self.char_done = True
        elif event == _IRQ_GATTC_NOTIFY:
            conn_handle, value_handle, notify_data = data
            if conn_handle == self.conn_handle and value_handle == self.char_handle:
                next_write = (self._nwrite + 1) % NOTIFICATION_QUEUE_SIZE
                if next_write != self._nread:
                    record = self._nwrite * NOTIFICATION_RECORD_SIZE
                    length = min(len(notify_data), MAX_NOTIFICATION_SIZE)
                    self._nbuf[record] = length
                    for i in range(length):
                        self._nbuf[record + 1 + i] = notify_data[i]
                    self._nwrite = next_write
        elif event == _IRQ_GATTC_WRITE_DONE:
            conn_handle, _, status = data
            if conn_handle == self.conn_handle:
                self.write_error = status

    # -- helpers -------------------------------------------------------------
    def _wait_for(self, predicate, description, timeout_ms=OPERATION_TIMEOUT_MS):
        started = time.ticks_ms()
        while not predicate():
            if time.ticks_diff(time.ticks_ms(), started) >= timeout_ms:
                raise RuntimeError("Timed out waiting for " + description)
            time.sleep_ms(20)

    # -- connection ------------------------------------------------------------
    def connect(self):
        """Scan, connect and discover. Raises RuntimeError on failure."""
        self._reset_link_state()
        self._cand_found = False
        self._cand_rssi = -128
        self.scan_done = False
        self._log("Scanning for LEGO hub ({} ms)".format(self.scan_ms))
        self.ble.gap_scan(self.scan_ms, 30000, 30000, True)
        self._wait_for(lambda: self.scan_done, "BLE scan", self.scan_ms + 3000)
        if not self._cand_found:
            raise RuntimeError("No LEGO hub found; is it switched on?")

        addr_type = self._cand[0]
        address = bytes(self._cand[1:7])
        self._log("Connecting to hub {} (RSSI {})".format(hx(address), self._cand_rssi))
        self.ble.gap_connect(addr_type, address, 15000)
        self._wait_for(lambda: self.connected or self.disconnected,
                       "hub connection", 18000)
        if not self.connected:
            raise RuntimeError("Hub disconnected while connecting")

        try:
            self._discover()
        except Exception:
            self.disconnect()
            raise
        self._log("Hub connected")

    def _reset_link_state(self):
        self.connected = False
        self.disconnected = False
        self.char_handle = None
        self.char_properties = 0
        self.service_start = 0
        self.service_end = 0
        self._nread = 0
        self._nwrite = 0
        self.write_error = 0
        self.ports_used = set()

    def _discover(self):
        self.service_done = False
        self.ble.gattc_discover_services(self.conn_handle)
        self._wait_for(lambda: self.service_done, "service discovery")
        if not self.service_start:
            raise RuntimeError("Hub does not expose the LWP3 service")

        self.char_done = False
        self.char_handle = None
        self.ble.gattc_discover_characteristics(
            self.conn_handle, self.service_start, self.service_end
        )
        self._wait_for(lambda: self.char_done, "characteristic discovery")
        if self.char_handle is None:
            raise RuntimeError("LWP3 characteristic not found")
        if not self.char_properties & _PROP_NOTIFY:
            raise RuntimeError("LWP3 characteristic has no notifications")
        if not self.char_properties & (_PROP_WRITE | _PROP_WRITE_NO_RESPONSE):
            raise RuntimeError("LWP3 characteristic is not writable")

        # Enable notifications (CCCD sits right after the value handle).
        try:
            self.ble.gattc_write(self.conn_handle, self.char_handle + 1,
                                 b"\x01\x00", 1)
        except Exception:
            pass
        time.sleep_ms(300)
        self.request_battery()

    def disconnect(self):
        if self.connected and self.conn_handle is not None:
            try:
                self.ble.gap_disconnect(self.conn_handle)
            except Exception:
                pass
        self.connected = False

    def close(self):
        self.disconnect()
        try:
            self.ble.active(False)
        except Exception:
            pass

    # -- I/O ---------------------------------------------------------------------
    def send(self, data):
        if not self.connected:
            raise RuntimeError("Not connected to the hub")
        # Write without response when available: lowest latency.
        mode = 0 if self.char_properties & _PROP_WRITE_NO_RESPONSE else 1
        self.write_error = 0
        self.ble.gattc_write(self.conn_handle, self.char_handle, data, mode)

    def set_power(self, port, power):
        if (power < -100 or power > 100) and power != 127:
            raise ValueError("power must be -100..100, or 127 to brake")
        self.ports_used.add(port)
        self.send(bytes((0x08, 0x00, 0x81, port, 0x11, 0x51, 0x00, power & 0xFF)))

    def request_battery(self):
        try:
            self.send(bytes((0x05, 0x00, 0x01, 0x06, 0x05)))
        except Exception:
            pass

    def poll(self):
        """Drain notifications (main loop only). Tracks battery level."""
        while self._nread != self._nwrite:
            record = self._nread * NOTIFICATION_RECORD_SIZE
            length = self._nbuf[record]
            message = bytes(self._nbuf[record + 1:record + 1 + length])
            self._nread = (self._nread + 1) % NOTIFICATION_QUEUE_SIZE
            index = 2 if message and message[0] & 0x80 else 1
            # Hub property (0x01) -> battery % (0x06)
            if (len(message) > index + 3 and message[index + 1] == 0x01
                    and message[index + 2] == 0x06):
                self.battery = message[index + 4] if len(message) > index + 4 else None
            else:
                self._log("Hub msg: " + hx(message))
        if self.write_error:
            status = self.write_error
            self.write_error = 0
            self._log("GATT write failed, status {}".format(status))


# --------------------------------------------------------------------------
# Obstacle guard: HC-SR04 sensors, decided entirely on the Pico
# --------------------------------------------------------------------------
FAR_CM = 999.0


class ObstacleGuard:
    """Reads the front sensors round-robin (one ping per update() call, so the
    main loop never blocks for long) and turns the nearest distance into a
    forward speed limit of 0..100 %.

      distance >= SENSOR_SLOW_START_CM         no limit
      CRAWL_CM < distance < SLOW_START_CM      limit shrinks to the crawl speed
      STOP_CM < distance <= CRAWL_CM           crawl speed
      distance <= STOP_CM                      stop

    Assumption: nothing that is not a train sits within CRAWL_CM of the track
    (no tunnel mouths, no decoration). If something stays within CRAWL_CM for
    SENSOR_HALT_AFTER_S the train halts, and stays halted until the way is
    clear again. When it clears, the train resumes at crawl speed and speeds up
    gradually (SENSOR_ACCEL_PCT_S). A fast approach counts as nearer than it is:
    SENSOR_LOOKAHEAD_S seconds of closing speed are subtracted from the
    distance. No echo means 'clear'. Only forward driving is limited; reversing
    is always allowed, which is the way out of a halt. Thresholds: config.py.
    """

    def __init__(self):
        def cfg(name, default):
            return getattr(config, name, default)

        self.enabled = bool(cfg("SENSORS_ENABLED", True))
        self._timeout_us = cfg("SENSOR_TIMEOUT_US", 12000)
        self._min_cm = cfg("SENSOR_MIN_CM", 2.0)
        self._window = cfg("SENSOR_WINDOW", 3)
        self._slow_cm = cfg("SENSOR_SLOW_START_CM", 30)
        self._crawl_cm = cfg("SENSOR_CRAWL_CM", 10)
        self._stop_cm = cfg("SENSOR_STOP_CM", 5)
        self._crawl_speed = cfg("SENSOR_CRAWL_SPEED", 20)
        self._halt_after_s = cfg("SENSOR_HALT_AFTER_S", 1.5)
        self._accel = cfg("SENSOR_ACCEL_PCT_S", 40)
        self._lookahead_s = cfg("SENSOR_LOOKAHEAD_S", 0.6)
        self._max_closing = cfg("SENSOR_MAX_CLOSING_CM_S", 100)
        self._resume_cm = cfg("SENSOR_RESUME_MARGIN_CM", 3)

        specs = cfg("SENSOR_PINS", ())
        self.names = []
        self._pins = []
        self._hist = []
        self._next = 0
        self._trace = []
        self.distances = []
        self.nearest = FAR_CM
        self.closing = 0.0
        self.limit = 100.0
        self.state = "clear"
        self._close_since = None     # when nearest first dropped to CRAWL_CM
        self._halted = False
        self._last_decide = time.ticks_ms()
        if not specs:
            self.enabled = False
        if not self.enabled:
            return
        for name, trig, echo in specs:
            self.names.append(name)
            self._pins.append((Pin(trig, Pin.OUT, value=0), Pin(echo, Pin.IN)))
            self._hist.append([])
            self.distances.append(FAR_CM)

    def _ping(self, index):
        trig, echo = self._pins[index]
        trig.value(0)
        time.sleep_us(2)
        trig.value(1)
        time.sleep_us(10)
        trig.value(0)
        micros = machine.time_pulse_us(echo, 1, self._timeout_us)
        if micros < 0:
            return FAR_CM
        # Sound travels ~0.0343 cm/us; halve it for the round trip. Anything
        # under the sensor's minimum range is clamped: too close is still close.
        return max(micros * 0.0343 / 2, self._min_cm)

    def update(self):
        """Ping the next sensor and re-evaluate the speed limit."""
        if not self.enabled:
            return
        index = self._next
        self._next = (index + 1) % len(self._pins)
        history = self._hist[index]
        history.append(self._ping(index))
        if len(history) > self._window:
            history.pop(0)
        self.distances[index] = sorted(history)[len(history) // 2]  # median

        now = time.ticks_ms()
        self.nearest = min(self.distances)
        trace = self._trace
        trace.append((now, self.nearest))
        while time.ticks_diff(now, trace[0][0]) > 1000:
            trace.pop(0)
        # cm/s the nearest object is getting closer (negative = moving away),
        # measured against the newest sample that is at least 400 ms old.
        self.closing = 0.0
        base = None
        for sample in trace:
            if time.ticks_diff(now, sample[0]) >= 400:
                base = sample
            else:
                break
        if base is not None and base[1] < FAR_CM:
            closing = (base[1] - self.nearest) * 1000 / time.ticks_diff(now, base[0])
            # Cap it: an object popping into view (curve, tunnel) is not a real 3 m/s.
            self.closing = min(closing, self._max_closing)
        self._decide(now)

    def _decide(self, now):
        crawl = float(self._crawl_speed)
        d = self.nearest - max(0.0, self.closing) * self._lookahead_s
        if d <= self._stop_cm:
            target = 0.0
        elif d >= self._slow_cm:
            target = 100.0
        elif d <= self._crawl_cm:
            target = crawl
        else:
            target = crawl + (100.0 - crawl) * (d - self._crawl_cm) / (self._slow_cm - self._crawl_cm)

        # Stuck close to something for too long: halt until it is clear again.
        if self.nearest <= self._crawl_cm:
            if self._close_since is None:
                self._close_since = now
            elif time.ticks_diff(now, self._close_since) >= self._halt_after_s * 1000:
                self._halted = True
        elif self.nearest > self._crawl_cm + self._resume_cm:
            self._close_since = None
            self._halted = False
        if self._halted:
            target = 0.0

        # Brake at once, speed up gradually.
        dt = time.ticks_diff(now, self._last_decide) / 1000
        self._last_decide = now
        if target > self.limit:
            target = min(target, self.limit + self._accel * dt)
            if 0 < target < crawl:      # motors stall below crawl speed
                target = crawl
        self.limit = target
        self.state = "stopped" if target == 0 else ("slowing" if target < 100 else "clear")

    def apply(self, commanded):
        """Signed speed the hub should actually get for this commanded speed."""
        if not self.enabled or commanded <= 0:
            return commanded
        return min(commanded, int(self.limit))


# --------------------------------------------------------------------------
# Status LED
# --------------------------------------------------------------------------
class StatusLed:
    """Onboard LED: solid when driving, blinking when a link is down."""

    def __init__(self, enabled=True):
        self._pin = None
        if not enabled:
            return
        try:
            self._pin = machine.Pin("LED", machine.Pin.OUT)
        except Exception:
            self._pin = None

    def set(self, on):
        if self._pin is not None:
            self._pin.value(1 if on else 0)

    def blink(self, times=2, interval=0.1):
        for _ in range(times):
            self.set(True)
            time.sleep(interval)
            self.set(False)
            time.sleep(interval)


# --------------------------------------------------------------------------
# Train node
# --------------------------------------------------------------------------
class TrainNode:
    def __init__(self):
        self.led = StatusLed(getattr(config, "STATUS_LED_ENABLED", True))
        self.hub = Lwp3Hub(
            lambda message: log(message, "INFO"),
            target_address=getattr(config, "HUB_ADDRESS", None),
            scan_ms=getattr(config, "BLE_SCAN_MS", 5000),
        )
        self.ports = [parse_port(p) for p in getattr(config, "MOTOR_PORTS", ("A", "B"))]
        self.invert = set(parse_port(p) for p in getattr(config, "MOTOR_INVERT", ()))
        self.mqtt = TrainMqttClient(config, on_command=self._handle_command, log=log)
        self.wlan = network.WLAN(network.STA_IF)

        try:
            self.guard = ObstacleGuard()
        except Exception as exc:
            log("Obstacle sensors unavailable: {}".format(exc), "ERROR")
            self.guard = ObstacleGuard.__new__(ObstacleGuard)
            self.guard.enabled = False
        self._applied = 0            # speed last sent to the hub
        self._last_guard_event = 0
        self._csv = None
        self._csv_rows = 0
        self._open_csv()

        self.speed = 0
        self._last_contact = time.ticks_ms()
        self._stopped_by_watchdog = False
        self._last_telemetry = 0
        self._last_ping = 0
        self._last_battery_req = 0
        self._pending_events = []
        self._last_status_ms = 0
        self.light = 0               # light brightness 0-100, set from the web UI
        self._light_sent = None      # last value written to the hub (None = unknown)

    # -- Wi-Fi ---------------------------------------------------------------
    def connect_wifi(self):
        try:
            network.country(config.WIFI_COUNTRY)
        except Exception:
            pass
        self.wlan.active(True)
        try:
            self.wlan.config(pm=0xA11140)  # performance power mode: joins hotspots more reliably
        except Exception:
            pass
        if self.wlan.isconnected():
            return True
        log("Connecting to Wi-Fi '{}'".format(config.WIFI_SSID))
        self.wlan.connect(config.WIFI_SSID, config.WIFI_PASSWORD)
        deadline = time.time() + config.WIFI_CONNECT_TIMEOUT_S
        while not self.wlan.isconnected() and time.time() < deadline:
            self.led.blink(1, 0.1)
            time.sleep(0.4)
        if self.wlan.isconnected():
            log("Wi-Fi up: {}".format(self.wlan.ifconfig()[0]))
            return True
        log("Wi-Fi connection failed", "ERROR")
        return False

    def wifi_ok(self):
        try:
            return self.wlan.isconnected()
        except Exception:
            return False

    # -- hub output -------------------------------------------------------------
    def _apply_to_hub(self, speed):
        """Send the given signed speed (-100..100) to every motor port."""
        for port in self.ports:
            if port == 1:
                continue
            else:
                power = -speed if port in self.invert else speed
                self.hub.set_power(port, power)
                time.sleep_ms(10)

    def _update_drive(self, force=False):
        """Send min(commanded speed, sensor limit) to the hub when it changes."""
        if not self.hub.connected:
            return
        target = self.guard.apply(self.speed)
        if force or target != self._applied:
            self._apply_to_hub(target)
            self._applied = target

    def _guard_step(self):
        """One sensor ping, then re-apply the limit and report state changes."""
        if not self.guard.enabled:
            return
        before = self.guard.state
        self.guard.update()
        self._update_drive()
        self._log_row()
        now = time.ticks_ms()
        if (self.guard.state != before and self.speed > 0
                and time.ticks_diff(now, self._last_guard_event) >= 1000):
            self._last_guard_event = now
            severity = "warning" if self.guard.state == "stopped" else "info"
            self._pending_events.append((
                "obstacle",
                "{}: nearest {:.0f} cm, speed limit {}%, applied {}".format(
                    self.guard.state, self.guard.nearest,
                    int(self.guard.limit), self._applied),
                severity,
            ))
            self._publish_telemetry(force=True)

    def _open_csv(self):
        if not (self.guard.enabled and getattr(config, "SENSOR_LOG_CSV", False)):
            return
        try:
            import os
            n = 1
            existing = os.listdir()
            while "log_%03d.csv" % n in existing:
                n += 1
            self._csv = open("log_%03d.csv" % n, "w")
            self._csv.write("t_ms,%s,nearest_cm,closing_cm_s,limit,commanded,applied\n"
                            % ",".join(self.guard.names))
        except Exception as exc:
            log("CSV log unavailable: {}".format(exc), "WARNING")
            self._csv = None

    def _log_row(self):
        if self._csv is None:
            return
        try:
            self._csv.write("%d,%s,%.1f,%.1f,%d,%d,%d\n" % (
                time.ticks_ms(),
                ",".join("%.1f" % d for d in self.guard.distances),
                self.guard.nearest, self.guard.closing,
                int(self.guard.limit), self.speed, self._applied))
            self._csv_rows += 1
            if self._csv_rows % 20 == 0:
                self._csv.flush()
            if self._csv_rows % 200 == 0:
                import os
                stat = os.statvfs("/")
                if stat[0] * stat[3] < 100000:   # keep some flash free
                    self._csv.close()
                    self._csv = None
        except Exception:
            self._csv = None

    def _stop_hub(self, reason):
        """Force speed to 0 and tell the hub (best effort)."""
        self.speed = 0
        self._applied = 0
        if not self.hub.connected:
            return
        try:
            self._apply_to_hub(0)  # motors only; the lights keep their slider value
        except Exception as exc:
            log("Hub stop failed ({}): {}".format(reason, exc), "ERROR")

    # -- commands from the backend ---------------------------------------------
    def note_server_contact(self):
        self._last_contact = time.ticks_ms()
        self._stopped_by_watchdog = False

    @staticmethod
    def _parse_payload(payload):
        if isinstance(payload, (bytes, bytearray)):
            payload = payload.decode()
        if isinstance(payload, str):
            payload = json.loads(payload)
        if not isinstance(payload, dict):
            raise ValueError("command payload must be an object")
        return payload

    def _handle_command(self, payload):
        # Any inbound message - including the heartbeat - proves the control
        # server is alive, so it resets the safety watchdog.
        self.note_server_contact()
        if payload is None:
            return
        try:
            cmd = self._parse_payload(payload)
            name = str(cmd.get("command", cmd.get("action", "set_speed"))).lower()

            if name in ("set_light", "light"):
                self._set_light(cmd.get("brightness", cmd.get("light", 0)))
                return

            if name in ("stop", "brake", "emergency_stop", "estop"):
                target = 0
            elif name in ("set_speed", "speed", "drive", "move"):
                target = int(cmd.get("speed", 0))
                if str(cmd.get("direction", "forward")).lower() in ("reverse", "backward"):
                    target = -abs(target)
                target = max(-100, min(100, target))
            else:
                raise ValueError("unknown command '{}'".format(name))

            self.speed = target
            # The backend update is forwarded to the hub right away.
            if not self.hub.connected:
                self.speed = 0
                self._pending_events.append(
                    ("command_rejected", "Hub not connected", "warning")
                )
                return
            self._update_drive(force=True)
            log("Backend -> hub: speed {} (applied {})".format(target, self._applied), "INFO")
            self._pending_events.append(("command", "speed {}".format(target), "info"))
            self._publish_telemetry(force=True)
        except Exception as exc:
            log("Command handling failed: {}".format(exc), "ERROR")
            self._stop_hub("command error")
            self._pending_events.append(("command_error", str(exc), "error"))

    # -- lights ----------------------------------------------------------------
    def _set_light(self, value):
        """Light brightness 0-100 on the hub's light port (port 1, 'B')."""
        try:
            self.light = max(0, min(100, int(value)))
        except (TypeError, ValueError):
            log("Ignoring bad brightness {!r}".format(value), "WARNING")
            return
        self._sync_light()
        log("Backend -> lights {}%".format(self.light), "INFO")
        self._publish_telemetry(force=True)

    def _sync_light(self):
        """Write the light value to the hub when it changed or the hub reconnected."""
        if not self.hub.connected or self._light_sent == self.light:
            return
        try:
            self.hub.set_power(1, self.light)
            self._light_sent = self.light
        except Exception as exc:
            log("Light update failed: {}".format(exc), "ERROR")

    # -- safety -----------------------------------------------------------------
    def check_safety(self):
        """Stop if the server has gone quiet. Returns True when it stops."""
        if self._stopped_by_watchdog or self.speed == 0:
            return False
        silent_ms = time.ticks_diff(time.ticks_ms(), self._last_contact)
        if silent_ms > config.SAFETY_TIMEOUT_S * 1000:
            self._stopped_by_watchdog = True
            self._stop_hub("safety timeout")
            return True
        return False

    def on_connection_lost(self):
        if self.speed != 0:
            log("Link lost, stopping", "WARNING")
        self._stop_hub("link lost")

    # -- telemetry -----------------------------------------------------------------
    def _publish_telemetry(self, force=False):
        now = time.time()
        if not force and now - self._last_telemetry < config.TELEMETRY_INTERVAL_S:
            return
        self._last_telemetry = now
        telemetry = {
            "train_id": config.TRAIN_ID,
            "speed": self.speed,
            "speed_applied": self._applied,
            "hub_connected": self.hub.connected,
            "battery": self.hub.battery,
            "firmware": config.FIRMWARE_VERSION,
            "light": self.light,
        }
        if self.guard.enabled:
            telemetry["distance_cm"] = [
                None if d >= FAR_CM else round(d, 1) for d in self.guard.distances
            ]
            telemetry["obstacle"] = self.guard.state
            telemetry["speed_limit"] = int(self.guard.limit)
        try:
            telemetry["rssi"] = self.wlan.status("rssi")
        except Exception:
            pass
        self.mqtt.publish_telemetry(telemetry)

    def _flush_events(self):
        while self._pending_events:
            event_type, message, severity = self._pending_events.pop(0)
            self.mqtt.publish_event(event_type, message, severity)

    # -- hub connection supervision -------------------------------------------------
    def _ensure_hub(self):
        if self.hub.connected:
            return True
        self.speed = 0
        try:
            self.hub.connect()
            self._pending_events.append(("hub", "LEGO hub connected", "info"))
            return True
        except Exception as exc:
            log("Hub connect failed: {}".format(exc), "WARNING")
            self.led.blink(2, 0.08)
            return False

    # -- console heartbeat ----------------------------------------------------------
    def _print_status(self):
        """Every 5 s: one line saying what the node is doing right now."""
        now = time.ticks_ms()
        if time.ticks_diff(now, self._last_status_ms) < 5000:
            return
        self._last_status_ms = now
        near = "off"
        if self.guard.enabled:
            near = "{:.0f}cm {}".format(min(self.guard.nearest, 999), self.guard.state)
        log("alive: wifi={} mqtt={} hub={} speed={} applied={} near={}".format(
            self.wifi_ok(), self.mqtt.connected, self.hub.connected,
            self.speed, self._applied, near))

    # -- main loop -------------------------------------------------------------------
    def run(self):
        log("Train node {} ({}) starting".format(config.TRAIN_ID, config.DEVICE_ID))
        backoff = 1
        then = time.time()
        while True:
            try:
                self._print_status()
                if not self.wifi_ok():
                    self.on_connection_lost()
                    self.mqtt.connected = False
                    if not self.connect_wifi():
                        time.sleep(backoff)
                        backoff = min(backoff * 2, 30)
                        continue
                    backoff = 1

                if not self.mqtt.connected:
                    self.on_connection_lost()
                    try:
                        self.mqtt.connect()
                        self.note_server_contact()
                        self.mqtt.publish_event(
                            "boot", "Firmware {} online".format(config.FIRMWARE_VERSION)
                        )
                        backoff = 1
                    except Exception as exc:
                        log("MQTT connect failed: {}".format(exc), "ERROR")
                        self.led.blink(3, 0.08)
                        time.sleep(backoff)
                        backoff = min(backoff * 2, 30)
                        continue

                if not self.hub.connected:
                    self._light_sent = None  # re-send the light value once it reconnects
                    self._ensure_hub()  # blocks during the BLE scan
                    # Keep the MQTT session alive after a long scan.
                    self.mqtt.check_messages()
                    self.mqtt.ping()
                    if not self.hub.connected:
                        time.sleep(1)
                        continue

                self.mqtt.check_messages()
                self.hub.poll()
                self._guard_step()
                self._sync_light()

                if self.check_safety():
                    self._pending_events.append(
                        ("safety_timeout",
                         "Stopped: no contact with the control server", "error")
                    )

                self._flush_events()
                self._publish_telemetry()
                
                
                now = time.time()
                # light sensor disabled
                # if (now-then)>1:
                #     daylight = int(light.read_u16()/65000*100)
                #     print(daylight)
                #     self.hub.set_power(1, daylight if daylight>15 else 0)
                #     then = now
                    
                
                if now - self._last_ping >= max(2, config.MQTT_KEEPALIVE_S // 3):
                    self._last_ping = now
                    self.mqtt.ping()
                if now - self._last_battery_req >= 60:
                    self._last_battery_req = now
                    self.hub.request_battery()

                self.led.set(self.speed != 0)
                time.sleep(0.05)

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                # Never let an unexpected error leave the train rolling.
                log("Unhandled error in main loop: {}".format(exc), "ERROR")
                if hasattr(sys, "print_exception"):
                    sys.print_exception(exc)
                self._stop_hub("firmware error")
                self.mqtt.connected = False
                time.sleep(1)

    def shutdown(self):
        log("Shutting down")
        self._stop_hub("shutdown")
        try:
            self.mqtt.disconnect()
        except Exception:
            pass
        self.hub.close()
        if self._csv is not None:
            self._csv.close()
        self.led.set(False)


def main():
    print("=== train firmware {} starting ===".format(getattr(config, "FIRMWARE_VERSION", "?")))
    node = TrainNode()
    try:
        node.run()
    except KeyboardInterrupt:
        node.shutdown()
    except Exception as exc:
        log("Fatal: {}".format(exc), "ERROR")
        node._stop_hub("fatal error")
        time.sleep(5)
        machine.reset()  # a rebooting Pico is safer than a stuck one


if __name__ == "__main__":
    main()