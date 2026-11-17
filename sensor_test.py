from machine import Pin, time_pulse_us
from time import sleep_us, sleep_ms

SENSORS = {"s1": (2, 3), "s2": (4, 5)}   # name: (trig GP, echo GP)
pins = {n: (Pin(t, Pin.OUT), Pin(e, Pin.IN)) for n, (t, e) in SENSORS.items()}


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


PERIOD_MS = 500   # time between rounds


def fmt(v):
    return "  --  " if v is None else "%6.1f" % v


while True:
    line = []
    for name, (trig, echo) in pins.items():
        line.append(name + " " + fmt(read_cm(trig, echo)))
        sleep_ms(60)
    print(" | ".join(line))
    sleep_ms(PERIOD_MS)
