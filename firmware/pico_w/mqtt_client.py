"""MQTT plumbing for the Pico W, built on umqtt.simple.

Install the dependency once on the Pico:

    import mip; mip.install("umqtt.simple")

This module knows nothing about motors and the motor code knows nothing about
MQTT; main.py wires the two together.
"""

import json

try:
    import utime as time
except ImportError:
    import time

from umqtt.simple import MQTTClient


class TrainMqttClient:
    """Thin wrapper adding LWT, JSON payloads and the topic scheme."""

    def __init__(self, config, on_command, log=print):
        self._config = config
        self._on_command = on_command
        self._log = log
        self._client = None
        self.connected = False

        self.train_id = config.TRAIN_ID
        self.device_id = config.DEVICE_ID
        self.topic_status = "trains/{}/status".format(self.train_id)
        self.topic_telemetry = "trains/{}/telemetry".format(self.train_id)
        self.topic_event = "trains/{}/event".format(self.train_id)
        self.topic_command = "trains/{}/command".format(self.train_id)
        self.topic_system_command = "system/command"
        self.topic_heartbeat = "system/heartbeat"

    # -- connection -------------------------------------------------------
    def connect(self):
        """Connect, register the LWT, subscribe and announce ourselves."""
        config = self._config
        will = json.dumps(
            {
                "v": 1,
                "train_id": self.train_id,
                "device_id": self.device_id,
                "status": "offline",
                "reason": "lwt",
            }
        )
        client = MQTTClient(
            client_id="pico-{}".format(self.device_id),
            server=config.MQTT_HOST,
            port=config.MQTT_PORT,
            user=config.MQTT_USERNAME,
            password=config.MQTT_PASSWORD,
            keepalive=config.MQTT_KEEPALIVE_S,
        )
        # Retained, QoS 1: the broker publishes this for us if we vanish, and
        # the backend sees it even if it was restarted in the meantime.
        client.set_last_will(self.topic_status, will, retain=True, qos=1)
        client.set_callback(self._on_message)
        client.connect()

        client.subscribe(self.topic_command, qos=1)
        client.subscribe(self.topic_system_command, qos=1)
        client.subscribe(self.topic_heartbeat, qos=0)

        self._client = client
        self.connected = True
        self._log("MQTT connected to {}:{}".format(config.MQTT_HOST, config.MQTT_PORT))
        self.publish_status("online")
        return True

    def disconnect(self, reason="shutdown"):
        if self._client is None:
            return
        try:
            self.publish_status("offline", reason=reason)
            self._client.disconnect()
        except Exception as exc:
            self._log("Disconnect error: {}".format(exc))
        finally:
            self._client = None
            self.connected = False

    def check_messages(self):
        """Non-blocking poll. Returns False if the link has died."""
        if self._client is None:
            return False
        try:
            self._client.check_msg()
            return True
        except Exception as exc:
            self._log("MQTT receive failed: {}".format(exc))
            self.connected = False
            self._client = None
            return False

    def ping(self):
        """Keep the broker from declaring us dead when we are idle."""
        if self._client is None:
            return False
        try:
            self._client.ping()
            return True
        except Exception as exc:
            self._log("MQTT ping failed: {}".format(exc))
            self.connected = False
            self._client = None
            return False

    # -- publishing --------------------------------------------------------
    def publish_status(self, status, reason=None):
        payload = {
            "v": 1,
            "train_id": self.train_id,
            "device_id": self.device_id,
            "status": status,
            "firmware": getattr(self._config, "FIRMWARE_VERSION", "unknown"),
        }
        if reason:
            payload["reason"] = reason
        return self._publish(self.topic_status, payload, retain=True, qos=1)

    def publish_telemetry(self, telemetry):
        payload = {"v": 1, "train_id": self.train_id, "timestamp": _iso_ish()}
        payload.update(telemetry)
        return self._publish(self.topic_telemetry, payload, retain=False, qos=0)

    def publish_event(self, event_type, message, severity="info"):
        return self._publish(
            self.topic_event,
            {
                "v": 1,
                "train_id": self.train_id,
                "type": event_type,
                "message": message,
                "severity": severity,
            },
            retain=False,
            qos=1,
        )

    def _publish(self, topic, payload, retain=False, qos=0):
        if self._client is None:
            return False
        try:
            self._client.publish(topic, json.dumps(payload), retain=retain, qos=qos)
            return True
        except Exception as exc:
            self._log("Publish to {} failed: {}".format(topic, exc))
            self.connected = False
            self._client = None
            return False

    # -- receiving ----------------------------------------------------------
    def _on_message(self, topic, message):
        topic = topic.decode() if isinstance(topic, bytes) else topic
        if topic == self.topic_heartbeat:
            self._on_command(None)  # counts as server contact, nothing to do
            return
        try:
            payload = json.loads(message.decode() if isinstance(message, bytes) else message)
        except Exception as exc:
            self._log("Ignoring malformed message on {}: {}".format(topic, exc))
            return
        if not isinstance(payload, dict):
            return
        self._on_command(payload)


def _iso_ish():
    """A timestamp the backend can parse. The Pico has no RTC by default, so
    this is uptime-based unless NTP has been configured."""
    try:
        year, month, day, hour, minute, second, _, _ = time.localtime()
        return "{:04d}-{:02d}-{:02d}T{:02d}:{:02d}:{:02d}Z".format(
            year, month, day, hour, minute, second
        )
    except Exception:
        return None
