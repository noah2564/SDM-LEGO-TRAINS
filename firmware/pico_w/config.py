"""Per-device configuration for the Pico W firmware.

Copy this file to the Pico as config.py and edit the values. Every Pico gets
its own TRAIN_ID and DEVICE_ID - one Pico drives exactly one train.

Nothing else in the firmware hardcodes any of these values.
"""

# --- Identity ---------------------------------------------------------
# Must match the train registered in the web UI. Used verbatim in MQTT
# topics, so stick to letters, digits, '-', '_' and '.'.
TRAIN_ID = "train-001"
DEVICE_ID = "pico-001"
TRAIN_NAME = "ICE"
FIRMWARE_VERSION = "1.0.0"

# --- Wi-Fi ------------------------------------------------------------
WIFI_SSID = "your-wifi-ssid"
WIFI_PASSWORD = "your-wifi-password"
WIFI_COUNTRY = "DK"  # regulatory domain; affects the usable channels
WIFI_CONNECT_TIMEOUT_S = 20

# --- MQTT -------------------------------------------------------------
# IP or hostname of the machine running the Docker stack.
MQTT_HOST = "192.168.1.10"
MQTT_PORT = 1883
MQTT_USERNAME = None  # set when broker authentication is enabled
MQTT_PASSWORD = None
MQTT_KEEPALIVE_S = 15  # broker declares us dead (and fires the LWT) at ~1.5x this

# --- Safety -----------------------------------------------------------
# Stop the train if nothing is heard from the server for this long. The
# backend publishes system/heartbeat every 2s by default, so anything above
# ~5s is safe from false positives. Keep this small: it is the time the train
# keeps rolling blind after the network dies.
SAFETY_TIMEOUT_S = 6.0

# --- Telemetry --------------------------------------------------------
TELEMETRY_INTERVAL_S = 2.0

# --- Motor / hub ------------------------------------------------------
# Which hub implementation to use: "pwm" drives an H-bridge from the Pico's
# GPIO pins; "mock" logs to the console and is useful on the bench.
HUB_DRIVER = "pwm"

# Pins for the "pwm" driver (an L298N/DRV8833-style H-bridge).
MOTOR_PWM_PIN = 15
MOTOR_DIR_A_PIN = 14
MOTOR_DIR_B_PIN = 13
MOTOR_PWM_FREQ_HZ = 500
# Speed 1-100 maps into this duty range. Below MIN_DUTY most LEGO motors
# buzz without turning, so small speed values are lifted to it.
MOTOR_MIN_DUTY = 25
MOTOR_MAX_DUTY = 100

# Optional: battery sense via ADC voltage divider. Set to None to disable.
BATTERY_ADC_PIN = None
BATTERY_DIVIDER_RATIO = 3.0

STATUS_LED_ENABLED = True
LOG_LEVEL = "INFO"
