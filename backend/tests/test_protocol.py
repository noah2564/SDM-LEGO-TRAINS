"""Command validation and payload sanitising."""

from __future__ import annotations

import pytest

from app import protocol
from app.protocol import CommandError, validate_command


class TestCommandValidation:
    def test_set_speed_is_normalised(self) -> None:
        assert validate_command({"command": "set_speed", "speed": 50}) == {
            "command": "set_speed",
            "speed": 50,
        }

    def test_set_speed_with_direction(self) -> None:
        result = validate_command(
            {"command": "set_speed", "speed": 30, "direction": "backward"}
        )
        assert result["direction"] == "backward"

    @pytest.mark.parametrize("speed", [-1, 101, 1000])
    def test_speed_out_of_range_is_rejected(self, speed: int) -> None:
        with pytest.raises(CommandError):
            validate_command({"command": "set_speed", "speed": speed})

    def test_speed_above_configured_limit_is_rejected(self) -> None:
        with pytest.raises(CommandError, match="exceeds configured limit"):
            validate_command({"command": "set_speed", "speed": 80}, max_speed=60)

    def test_speed_must_be_a_number(self) -> None:
        with pytest.raises(CommandError):
            validate_command({"command": "set_speed", "speed": "fast"})

    def test_unknown_command_is_rejected(self) -> None:
        with pytest.raises(CommandError, match="Unknown command"):
            validate_command({"command": "self_destruct"})

    def test_missing_command_is_rejected(self) -> None:
        with pytest.raises(CommandError, match="Missing"):
            validate_command({"speed": 10})

    def test_unexpected_fields_are_rejected(self) -> None:
        with pytest.raises(CommandError):
            validate_command({"command": "stop", "speed": 99})

    def test_bad_direction_is_rejected(self) -> None:
        with pytest.raises(CommandError):
            validate_command({"command": "set_direction", "direction": "sideways"})

    def test_non_object_payload_is_rejected(self) -> None:
        with pytest.raises(CommandError):
            validate_command(["stop"])  # type: ignore[arg-type]

    def test_emergency_stop_accepts_reason(self) -> None:
        result = validate_command({"command": "emergency_stop", "reason": "kid on track"})
        assert result["reason"] == "kid on track"

    def test_set_config_requires_a_field(self) -> None:
        with pytest.raises(CommandError):
            validate_command({"command": "set_config"})

    def test_envelope_carries_identity_and_time(self) -> None:
        envelope = protocol.build_command_envelope("train-001", {"command": "stop"})
        assert envelope["train_id"] == "train-001"
        assert envelope["command"] == "stop"
        assert envelope["v"] == protocol.PROTOCOL_VERSION
        assert envelope["command_id"] and envelope["ts"]


class TestTopics:
    def test_round_trip(self) -> None:
        assert protocol.parse_topic("trains/train-001/telemetry") == ("train-001", "telemetry")
        assert protocol.command_topic("train-002") == "trains/train-002/command"

    @pytest.mark.parametrize(
        "topic", ["trains/train-001", "other/train-001/status", "trains//status", "nonsense"]
    )
    def test_invalid_topics(self, topic: str) -> None:
        assert protocol.parse_topic(topic) is None


class TestSanitising:
    def test_telemetry_is_clamped(self) -> None:
        clean = protocol.sanitize_telemetry(
            {"speed": 500, "direction": "forward", "battery": 7.4}, max_speed=100
        )
        assert clean["speed"] == 100
        assert clean["battery"] == 7.4

    def test_telemetry_rejects_garbage_direction(self) -> None:
        clean = protocol.sanitize_telemetry({"direction": "up"})
        assert "direction" not in clean

    def test_unknown_telemetry_fields_are_kept_but_isolated(self) -> None:
        clean = protocol.sanitize_telemetry({"speed": 10, "track_sensor": "green"})
        assert clean["extra"] == {"track_sensor": "green"}

    def test_status_falls_back_to_unknown(self) -> None:
        assert protocol.sanitize_status({"status": "banana"})["status"] == "unknown"

    def test_will_payload_is_offline(self) -> None:
        payload = protocol.offline_will_payload("train-001", "pico-001")
        assert payload["status"] == "offline"
        assert payload["device_id"] == "pico-001"
