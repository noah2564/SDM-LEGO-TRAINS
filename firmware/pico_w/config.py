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
FIRMWARE_VERSION = "1.1.0"

# --- Wi-Fi ------------------------------------------------------------
WIFI_SSID = "MARK-V 6730"
WIFI_PASSWORD = "LegoTrains"
WIFI_COUNTRY = "DK"  # regulatory domain; affects the usable channels
WIFI_CONNECT_TIMEOUT_S = 20

# --- MQTT -------------------------------------------------------------
# IP or hostname of the machine running the Docker stack.
MQTT_HOST = "192.168.137.1"
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

# --- Obstacle sensors (HC-SR04, handled on the Pico) -------------------
SENSORS_ENABLED = True
# (name, trig GPIO, echo GPIO). Echo needs a 5V->3.3V divider if the sensor
# is powered from 5V; from 3V3 it is wired straight to the GPIO.
SENSOR_PINS = (("left", 2, 3), ("right", 4, 5))
SENSOR_TIMEOUT_US = 12000     # echo wait per ping, ~2 m
SENSOR_MIN_CM = 2.0           # closer than this is clamped, not ignored
SENSOR_WINDOW = 3             # median over the last N pings per sensor
SENSOR_SLOW_START_CM = 30     # start slowing below this distance
SENSOR_CRAWL_CM = 10          # at or below this: crawl speed
SENSOR_STOP_CM = 5            # at or below this: stop
SENSOR_CRAWL_SPEED = 20       # % power while crawling (motors stall below ~this)
SENSOR_HALT_AFTER_S = 1.5     # halt if still within CRAWL_CM after this long
SENSOR_ACCEL_PCT_S = 40       # how fast the limit may rise again, % per second
SENSOR_LOOKAHEAD_S = 0.6      # fast approaches count as nearer by this much
SENSOR_MAX_CLOSING_CM_S = 100 # closing speeds above this are capped (noise, objects popping into view)
SENSOR_RESUME_MARGIN_CM = 3   # extra room needed before resuming after a stop
SENSOR_LOG_CSV = False        # True: write log_NNN.csv on the Pico for tuning
