import network
import time

import config

network.country(config.WIFI_COUNTRY or "DK")
w = network.WLAN(network.STA_IF)

print("--- trying", repr(config.WIFI_SSID))
for attempt in range(1, 4):
    w.active(False)
    time.sleep(1)
    w.active(True)
    w.config(pm=0xA11140)  # power saving off
    print("attempt", attempt)
    w.connect(config.WIFI_SSID, config.WIFI_PASSWORD)
    for i in range(15):
        time.sleep(1)
        status = w.status()
        print("  ", i + 1, "status", status)
        if status == 3 or status < 0:
            break
    if w.isconnected():
        print("--- CONNECTED", w.ifconfig())
        break
else:
    print("--- FAILED, last status", w.status())
