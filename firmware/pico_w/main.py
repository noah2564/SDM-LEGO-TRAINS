"""Pico W train controller - entry point.

Responsibilities, in priority order:
  1. Keep the train safe: stop whenever the link or the server goes quiet.
  2. Stay connected to Wi-Fi and MQTT, reconnecting forever.
  3. Execute commands and report telemetry.

Copy config.py, lego_hub.py, train_controller.py, mqtt_client.py and this
file to the Pico, install umqtt.simple, and reset the board.
"""

import sys

import machine
import network

try:
    import utime as time
except ImportError:
    import time

import config
from lego_hub import create_hub
from mqtt_client import TrainMqttClient
from train_controller import TrainController

LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}
_threshold = LEVELS.get(getattr(config, "LOG_LEVEL", "INFO"), 20)


def log(message, level="INFO"):
    if LEVELS.get(level, 20) >= _threshold:
        print("[{:>8}] {} {}".format(time.ticks_ms() / 1000, level, message))


class StatusLed:
    """Onboard LED: solid when driving, blinking when the link is down."""

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


class TrainNode:
    def __init__(self):
        self.led = StatusLed(getattr(config, "STATUS_LED_ENABLED", True))
        self.hub = create_hub(config, log=lambda message: log(message, "DEBUG"))
        self.controller = TrainController(
            self.hub,
            max_speed=100,
            safety_timeout_s=config.SAFETY_TIMEOUT_S,
            log=lambda message: log(message, "WARNING"),
        )
        self.mqtt = TrainMqttClient(config, on_command=self._handle_command, log=log)
        self.wlan = network.WLAN(network.STA_IF)
        self._last_telemetry = 0
        self._last_ping = 0
        self._pending_events = []

    # -- Wi-Fi -------------------------------------------------------------
    def connect_wifi(self):
        try:
            network.country(config.WIFI_COUNTRY)
        except Exception:
            pass
        self.wlan.active(True)
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

    # -- commands -----------------------------------------------------------
    def _handle_command(self, payload):
        # Any inbound message - including the server heartbeat - proves the
        # control server is alive, so it resets the safety watchdog.
        self.controller.note_server_contact()
        if payload is None:
            return
        try:
            ok, event = self.controller.handle_command(payload)
        except Exception as exc:
            log("Command handling failed: {}".format(exc), "ERROR")
            self.controller.emergency_stop_now("command error")
            self._pending_events.append(("command_error", str(exc), "error"))
            return
        if event is not None:
            self._pending_events.append(event)
        if ok:
            self._publish_telemetry(force=True)

    # -- telemetry -----------------------------------------------------------
    def _publish_telemetry(self, force=False):
        now = time.time()
        if not force and now - self._last_telemetry < config.TELEMETRY_INTERVAL_S:
            return
        self._last_telemetry = now
        telemetry = self.controller.telemetry()
        try:
            telemetry["rssi"] = self.wlan.status("rssi")
        except Exception:
            pass
        self.mqtt.publish_telemetry(telemetry)

    def _flush_events(self):
        while self._pending_events:
            event_type, message, severity = self._pending_events.pop(0)
            self.mqtt.publish_event(event_type, message, severity)

    # -- main loop ------------------------------------------------------------
    def run(self):
        log("Train node {} ({}) starting".format(config.TRAIN_ID, config.DEVICE_ID))
        backoff = 1
        while True:
            try:
                if not self.wifi_ok():
                    # No network means no supervision: stop before retrying.
                    self.controller.on_connection_lost()
                    self.mqtt.connected = False
                    if not self.connect_wifi():
                        time.sleep(backoff)
                        backoff = min(backoff * 2, 30)
                        continue
                    backoff = 1

                if not self.mqtt.connected:
                    self.controller.on_connection_lost()
                    try:
                        self.mqtt.connect()
                        self.controller.note_server_contact()
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

                self.mqtt.check_messages()

                if self.controller.check_safety():
                    self._pending_events.append(
                        (
                            "safety_timeout",
                            "Stopped: no contact with the control server",
                            "error",
                        )
                    )

                self._flush_events()
                self._publish_telemetry()

                now = time.time()
                if now - self._last_ping >= max(2, config.MQTT_KEEPALIVE_S // 3):
                    self._last_ping = now
                    self.mqtt.ping()

                self.led.set(self.controller.speed > 0)
                time.sleep(0.05)

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                # Never let an unexpected error leave the train rolling.
                log("Unhandled error in main loop: {}".format(exc), "ERROR")
                sys.print_exception(exc) if hasattr(sys, "print_exception") else None
                self.controller.emergency_stop_now("firmware error")
                self.mqtt.connected = False
                time.sleep(1)

    def shutdown(self):
        log("Shutting down")
        self.controller.stop()
        self.mqtt.disconnect()
        self.hub.close()
        self.led.set(False)


def main():
    node = TrainNode()
    try:
        node.run()
    except KeyboardInterrupt:
        node.shutdown()
    except Exception as exc:
        log("Fatal: {}".format(exc), "ERROR")
        node.controller.emergency_stop_now("fatal error")
        time.sleep(5)
        machine.reset()  # a rebooting Pico is safer than a stuck one


if __name__ == "__main__":
    main()
