# Standalone distance logger for the two HC-SR04s. Runs on boot, writes a new
# CSV (log_001.csv, log_002.csv, ...) to the Pico's flash. Columns:
#   t_ms, s1_cm, s2_cm   (empty cell = no echo)
from machine import Pin, time_pulse_us
from time import sleep_us, sleep_ms, ticks_ms, ticks_diff
import os

SENSORS = [("s1", 2, 3), ("s2", 4, 5)]   # name, trig GP, echo GP
GAP_MS = 50          # pause between the two sensors (no crosstalk)
PERIOD_MS = 100      # time between rounds
START_DELAY_S = 5    # grace time to hit Stop / Ctrl+C after power-up
MIN_FREE_BYTES = 100_000
FLUSH_EVERY = 20     # rows

led = Pin("LED", Pin.OUT)
pins = [(Pin(t, Pin.OUT), Pin(e, Pin.IN)) for _, t, e in SENSORS]


def read_cm(trig, echo):
    trig.value(0)
    sleep_us(2)
    trig.value(1)
    sleep_us(10)
    trig.value(0)
    t = time_pulse_us(echo, 1, 30000)
    if t < 0:
        return None
    return t * 0.0343 / 2


def next_log_name():
    n = 1
    existing = os.listdir()
    while "log_%03d.csv" % n in existing:
        n += 1
    return "log_%03d.csv" % n


def free_bytes():
    s = os.statvfs("/")
    return s[0] * s[3]


# blink during the grace period
for _ in range(START_DELAY_S * 2):
    led.toggle()
    sleep_ms(500)
led.value(1)

name = next_log_name()
f = open(name, "w")
f.write("t_ms,s1_cm,s2_cm\n")
t0 = ticks_ms()
rows = 0
try:
    while free_bytes() > MIN_FREE_BYTES:
        cells = []
        stamp = ticks_diff(ticks_ms(), t0)
        for trig, echo in pins:
            v = read_cm(trig, echo)
            cells.append("" if v is None else "%.1f" % v)
            sleep_ms(GAP_MS)
        f.write("%d,%s\n" % (stamp, ",".join(cells)))
        rows += 1
        if rows % FLUSH_EVERY == 0:
            f.flush()
        sleep_ms(PERIOD_MS)
finally:
    f.close()
    led.value(0)
