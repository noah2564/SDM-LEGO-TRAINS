"""Pico W simulator.

Speaks exactly the protocol the firmware speaks: retained status with LWT,
periodic telemetry, command subscription, backend-heartbeat safety timeout.
The backend cannot tell the difference, which is the point - no
simulator-specific code exists anywhere else in the system.

Run one train:
    python simulator.py --train-id train-001 --device-id pico-001

Run a fleet of three:
    python simulator.py --count 3

Simulate a device that vanishes 20s in (tests LWT -> offline):
    python simulator.py --train-id train-001 --die-after 20
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import paho.mqtt.client as mqtt

LOG_FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"
logger = logging.getLogger("simulator")

PROTOCOL_VERSION = 1
ACCELERATION_PER_SECOND = 60.0  # simulated ramp, units of speed per second


def iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"


@dataclass
class SimulatedTrain:
    """One simulated Pico W driving one simulated LEGO train."""

    train_id: str
    device_id: str
    host: str
    port: int
    username: str | None = None
    password: str | None = None
    telemetry_interval: float = 2.0
    safety_timeout: float = 6.0
    battery: float = 8.4
    die_after: float | None = None

    target_speed: int = 0
    speed: float = 0.0
    direction: str = "forward"
    emergency_stop: bool = False
    last_server_contact: float = field(default_factory=time.monotonic)
    started_at: float = field(default_factory=time.monotonic)
    _client: mqtt.Client | None = None
    _running: bool = True
    _log: logging.Logger = field(init=False)

    def __post_init__(self) -> None:
        self._log = logging.getLogger(self.train_id)

    # -- topics ---------------------------------------------------------
    @property
    def status_topic(self) -> str:
        return f"trains/{self.train_id}/status"

    @property
    def telemetry_topic(self) -> str:
        return f"trains/{self.train_id}/telemetry"

    @property
    def event_topic(self) -> str:
        return f"trains/{self.train_id}/event"

    @property
    def command_topic(self) -> str:
        return f"trains/{self.train_id}/command"

    # -- lifecycle ------------------------------------------------------
    def start(self) -> None:
        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2, client_id=f"sim-{self.device_id}", clean_session=True
        )
        if self.username:
            client.username_pw_set(self.username, self.password or "")
        client.will_set(
            self.status_topic,
            json.dumps(
                {
                    "v": PROTOCOL_VERSION,
                    "train_id": self.train_id,
                    "device_id": self.device_id,
                    "status": "offline",
                    "reason": "lwt",
                }
            ),
            qos=1,
            retain=True,
        )
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        client.reconnect_delay_set(min_delay=1, max_delay=10)
        self._client = client

        self._log.info("Connecting to %s:%s", self.host, self.port)
        client.connect_async(self.host, self.port, keepalive=15)
        client.loop_start()

        thread = threading.Thread(target=self._loop, name=f"{self.train_id}-loop", daemon=True)
        thread.start()

    def stop(self, graceful: bool = True) -> None:
        self._running = False
        client = self._client
        if client is None:
            return
        if graceful:
            self._set_speed(0)
            self._publish(
                self.status_topic,
                {
                    "v": PROTOCOL_VERSION,
                    "train_id": self.train_id,
                    "device_id": self.device_id,
                    "status": "offline",
                    "reason": "shutdown",
                },
                retain=True,
            )
            time.sleep(0.2)
            client.disconnect()
        else:
            # Yank the socket so Mosquitto publishes the Last Will.
            self._log.warning("Simulating a hard disconnect (no goodbye)")
            client._sock_close()  # noqa: SLF001 - deliberate ungraceful exit
        client.loop_stop()

    # -- MQTT callbacks --------------------------------------------------
    def _on_connect(self, client: mqtt.Client, _userdata: Any, _flags: Any, reason_code: Any, _props: Any = None) -> None:
        if getattr(reason_code, "is_failure", False) or (isinstance(reason_code, int) and reason_code != 0):
            self._log.error("Connect failed: %s", reason_code)
            return
        self._log.info("Connected")
        client.subscribe([(self.command_topic, 1), ("system/command", 1), ("system/heartbeat", 0)])
        self.last_server_contact = time.monotonic()
        self._publish(
            self.status_topic,
            {
                "v": PROTOCOL_VERSION,
                "train_id": self.train_id,
                "device_id": self.device_id,
                "status": "online",
                "firmware": "simulator-1.0",
            },
            retain=True,
        )
        self._publish(
            self.event_topic, {"type": "boot", "message": "Simulator connected", "severity": "info"}
        )

    def _on_disconnect(self, _client: mqtt.Client, _userdata: Any, *args: Any) -> None:
        self._log.warning("Disconnected from broker - stopping the motor")
        self._emergency_halt("mqtt_disconnected")

    def _on_message(self, _client: mqtt.Client, _userdata: Any, message: mqtt.MQTTMessage) -> None:
        self.last_server_contact = time.monotonic()
        if message.topic == "system/heartbeat":
            return
        try:
            payload = json.loads(message.payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            self._log.warning("Ignoring malformed command: %s", exc)
            return
        if not isinstance(payload, dict):
            return
        self._handle_command(payload)

    # -- command handling -------------------------------------------------
    def _handle_command(self, payload: dict[str, Any]) -> None:
        command = payload.get("command")
        self._log.info("Command received: %s", payload)

        if command == "emergency_stop":
            self.emergency_stop = True
            self.target_speed = 0
            self.speed = 0.0  # no ramp: emergency means now
            self._publish(
                self.event_topic,
                {
                    "type": "emergency_stop",
                    "message": payload.get("reason", "emergency stop"),
                    "severity": "warning",
                },
            )
        elif command == "clear_emergency":
            self.emergency_stop = False
            self.target_speed = 0
            self._publish(
                self.event_topic, {"type": "emergency_cleared", "message": "Ready to drive"}
            )
        elif command == "stop":
            self.target_speed = 0
        elif command == "set_speed":
            if self.emergency_stop:
                self._log.warning("Ignoring set_speed while emergency stopped")
                return
            self._set_speed(payload.get("speed", 0))
            if payload.get("direction") in {"forward", "backward"}:
                self.direction = payload["direction"]
        elif command == "set_direction":
            if payload.get("direction") in {"forward", "backward"}:
                if self.speed > 0:
                    self.target_speed = 0
                    self.speed = 0.0  # a real train must stop before reversing
                self.direction = payload["direction"]
        elif command == "set_config":
            if payload.get("safety_timeout_s"):
                self.safety_timeout = float(payload["safety_timeout_s"])
            if payload.get("telemetry_interval_s"):
                self.telemetry_interval = float(payload["telemetry_interval_s"])
        elif command == "ping":
            pass
        else:
            self._log.warning("Unknown command: %s", command)
            return
        self._publish_telemetry()

    def _set_speed(self, value: Any) -> None:
        try:
            speed = int(value)
        except (TypeError, ValueError):
            self._log.warning("Bad speed value: %r", value)
            return
        self.target_speed = max(0, min(100, speed))

    def _emergency_halt(self, reason: str) -> None:
        if self.speed or self.target_speed:
            self._log.warning("Halting: %s", reason)
        self.target_speed = 0
        self.speed = 0.0

    # -- main loop --------------------------------------------------------
    def _loop(self) -> None:
        last_telemetry = 0.0
        last_tick = time.monotonic()
        while self._running:
            now = time.monotonic()
            delta = now - last_tick
            last_tick = now

            if self.die_after is not None and now - self.started_at >= self.die_after:
                self._log.warning("die-after reached")
                self.stop(graceful=False)
                self.die_after = None
                self._running = False
                break

            # Safety timeout: nothing from the server for too long -> stop.
            if now - self.last_server_contact > self.safety_timeout:
                if self.speed > 0 or self.target_speed > 0:
                    self._emergency_halt(
                        f"no contact with the server for {self.safety_timeout:.0f}s"
                    )
                    self._publish(
                        self.event_topic,
                        {
                            "type": "safety_timeout",
                            "message": "Stopped: lost contact with the control server",
                            "severity": "error",
                        },
                    )

            self._ramp(delta)

            if now - last_telemetry >= self.telemetry_interval:
                self._publish_telemetry()
                last_telemetry = now
            time.sleep(0.1)

    def _ramp(self, delta: float) -> None:
        step = ACCELERATION_PER_SECOND * delta
        if self.speed < self.target_speed:
            self.speed = min(float(self.target_speed), self.speed + step)
        elif self.speed > self.target_speed:
            self.speed = max(float(self.target_speed), self.speed - step)
        # Battery sags under load and recovers a little when idle.
        drain = 0.0006 * delta * (1 + self.speed / 25.0)
        self.battery = max(6.0, min(8.4, self.battery - drain + random.uniform(0, 0.0004)))

    def _publish_telemetry(self) -> None:
        self._publish(
            self.telemetry_topic,
            {
                "v": PROTOCOL_VERSION,
                "train_id": self.train_id,
                "timestamp": iso_now(),
                "speed": int(round(self.speed)),
                "direction": self.direction,
                "battery": round(self.battery, 2),
                "connected": True,
                "emergency_stop": self.emergency_stop,
                "hub_connected": True,
                "uptime_s": round(time.monotonic() - self.started_at, 1),
                "rssi": random.randint(-72, -48),
            },
            qos=0,
        )

    def _publish(
        self, topic: str, payload: dict[str, Any], qos: int = 1, retain: bool = False
    ) -> None:
        client = self._client
        if client is None:
            return
        try:
            client.publish(topic, json.dumps(payload), qos=qos, retain=retain)
        except Exception as exc:  # broker gone mid-publish
            self._log.warning("Publish to %s failed: %s", topic, exc)


def build_trains(args: argparse.Namespace) -> list[SimulatedTrain]:
    common = {
        "host": args.host,
        "port": args.port,
        "username": args.username,
        "password": args.password,
        "telemetry_interval": args.telemetry_interval,
        "safety_timeout": args.safety_timeout,
        "die_after": args.die_after,
    }
    if args.count > 1:
        return [
            SimulatedTrain(
                train_id=f"train-{index:03d}", device_id=f"pico-{index:03d}", **common
            )
            for index in range(1, args.count + 1)
        ]
    return [SimulatedTrain(train_id=args.train_id, device_id=args.device_id, **common)]


def main() -> int:
    parser = argparse.ArgumentParser(description="Simulate one or more Pico W train controllers")
    parser.add_argument("--host", default=os.getenv("MQTT_HOST", "localhost"))
    parser.add_argument("--port", type=int, default=int(os.getenv("MQTT_PORT", "1883")))
    parser.add_argument("--username", default=os.getenv("MQTT_USERNAME") or None)
    parser.add_argument("--password", default=os.getenv("MQTT_PASSWORD") or None)
    parser.add_argument("--train-id", default=os.getenv("TRAIN_ID", "train-001"))
    parser.add_argument("--device-id", default=os.getenv("DEVICE_ID", "pico-001"))
    parser.add_argument(
        "--count",
        type=int,
        default=int(os.getenv("SIM_COUNT", "1")),
        help="Simulate N trains named train-001..train-00N",
    )
    parser.add_argument(
        "--telemetry-interval", type=float, default=float(os.getenv("TELEMETRY_INTERVAL", "2.0"))
    )
    parser.add_argument(
        "--safety-timeout", type=float, default=float(os.getenv("SAFETY_TIMEOUT", "6.0"))
    )
    parser.add_argument(
        "--die-after",
        type=float,
        default=None,
        help="Drop the connection without a goodbye after N seconds, to test LWT",
    )
    parser.add_argument("--log-level", default=os.getenv("LOG_LEVEL", "INFO"))
    args = parser.parse_args()

    logging.basicConfig(level=args.log_level.upper(), format=LOG_FORMAT)

    trains = build_trains(args)
    for train in trains:
        train.start()
    logger.info("Simulating %d train(s). Press Ctrl+C to stop.", len(trains))

    stopping = threading.Event()

    def shutdown(_signum: int, _frame: Any) -> None:
        stopping.set()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    stopping.wait()

    logger.info("Shutting down")
    for train in trains:
        train.stop(graceful=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
