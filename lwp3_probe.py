"""lwp3_probe.py - poke a LEGO Powered Up hub over Bluetooth LE (LWP3).

Standalone: not part of the Depot project. Needs Python 3.9+ and bleak:

    pip install bleak
    python lwp3_probe.py              # scan, pick a hub, connect
    python lwp3_probe.py --all        # scan and list EVERY BLE device (debugging)
    python lwp3_probe.py --address XX:XX:XX:XX:XX:XX

Before running: turn the hub on (press the green button, LED should blink),
close the official LEGO app, and make sure Bluetooth is on in Windows.

What it does:
  1. scans for devices advertising the LWP3 service / LEGO manufacturer id
  2. connects and subscribes to notifications
  3. asks the hub for name, firmware, battery, etc.
  4. prints every message the hub sends (decoded + raw hex)
  5. gives you a tiny prompt to send commands (type 'help')

Message layout (as I remember the LEGO spec - verify against
github.com/LEGO/lego-ble-wireless-protocol-docs):

    [length] [hub id = 0x00] [message type] [payload ...]
"""

import argparse
import asyncio
import sys

from bleak import BleakClient, BleakScanner

# One service, one characteristic: write commands to it, get notifications from it.
LWP3_SERVICE = "00001623-1212-efde-1623-785feabcd123"
LWP3_CHAR = "00001624-1212-efde-1623-785feabcd123"
LEGO_COMPANY_ID = 0x0397  # appears in the BLE advertisement's manufacturer data

# ---- lookup tables (from memory: unknown values just print as hex) ---------
SYSTEM_TYPES = {  # advertisement byte "system type & device number" - GUESSES
    0x40: "Move Hub (Boost)",
    0x41: "Powered Up Hub (City Hub 88009)",
    0x42: "Powered Up Remote",
    0x80: "Technic Hub (Control+)",
}

MSG_TYPES = {
    0x01: "Hub Property",
    0x02: "Hub Action",
    0x03: "Hub Alert",
    0x04: "Hub Attached I/O",
    0x05: "Generic Error",
    0x82: "Port Output Feedback",
}

HUB_PROPS = {
    0x01: "name",
    0x02: "button",
    0x03: "firmware version",
    0x04: "hardware version",
    0x05: "RSSI",
    0x06: "battery %",
    0x08: "manufacturer",
    0x09: "radio firmware",
    0x0A: "LWP version",
    0x0B: "system type id",
}

IO_TYPES = {  # what is plugged into a port (subset I'm fairly sure of)
    0x01: "motor",
    0x02: "train motor",
    0x08: "light",
    0x14: "hub voltage sensor",
    0x15: "hub current sensor",
    0x16: "hub piezo",
    0x17: "hub RGB LED",
    0x25: "color+distance sensor",
    0x26: "medium linear motor",
    0x2E: "large linear motor",
    0x2F: "XL motor",
}

PORT_LETTERS = {"A": 0, "B": 1, "C": 2, "D": 3}  # City hub: A=0, B=1


def hx(data: bytes) -> str:
    return " ".join(f"{b:02x}" for b in data)


# ---- scanning ----------------------------------------------------------------
def describe_advert(adv) -> str:
    """One-line summary of what a LEGO hub puts in its advertisement."""
    md = adv.manufacturer_data.get(LEGO_COMPANY_ID)
    if not md:
        return ""
    # md = button, system type, capabilities, network id, status, option
    if len(md) >= 2:
        guess = SYSTEM_TYPES.get(md[1], "unknown type")
        return f"  [LEGO advert: raw={hx(md)}  system type 0x{md[1]:02x} -> {guess}]"
    return f"  [LEGO advert: raw={hx(md)}]"


async def scan(timeout: float, show_all: bool):
    print(f"Scanning for {timeout:.0f}s ...")
    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    hubs = []
    for addr, (dev, adv) in found.items():
        is_lego = (
            LWP3_SERVICE in [u.lower() for u in adv.service_uuids]
            or LEGO_COMPANY_ID in adv.manufacturer_data
        )
        if is_lego or show_all:
            hubs.append((dev, adv, is_lego))
    return hubs


async def choose_hub(args):
    if args.address:
        return args.address
    hubs = await scan(args.scan_time, args.all)
    if not hubs:
        print("No LEGO hubs found. Is the hub on and blinking? Try --all to see everything.")
        sys.exit(1)
    for i, (dev, adv, is_lego) in enumerate(hubs):
        tag = "LEGO" if is_lego else "    "
        print(f"  [{i}] {tag} {dev.address}  name={adv.local_name or dev.name!r}  rssi={adv.rssi}"
              f"{describe_advert(adv)}")
    if len(hubs) == 1:
        return hubs[0][0].address
    idx = int(input("Which one? "))
    return hubs[idx][0].address


# ---- decoding notifications ----------------------------------------------------
def decode(data: bytes) -> str:
    """Turn one notification into a human-readable line."""
    if len(data) < 3:
        return f"(short) {hx(data)}"
    idx = 1
    if data[0] & 0x80:  # lengths > 127 use two bytes
        idx = 2
    mtype = data[idx + 1]
    payload = data[idx + 2:]
    name = MSG_TYPES.get(mtype, f"type 0x{mtype:02x}")
    detail = ""

    if mtype == 0x01 and len(payload) >= 2:  # hub property update
        prop, op, value = payload[0], payload[1], payload[2:]
        pname = HUB_PROPS.get(prop, f"prop 0x{prop:02x}")
        if prop in (0x01, 0x08):
            shown = value.decode("ascii", errors="replace")
        elif prop in (0x06, 0x05) and value:
            shown = str(int.from_bytes(value, "little", signed=(prop == 0x05)))
        else:
            shown = hx(value)
        detail = f"{pname} = {shown}"

    elif mtype == 0x04 and len(payload) >= 2:  # something (un)plugged on a port
        port, event = payload[0], payload[1]
        if event == 0x00:
            detail = f"port {port}: detached"
        elif event in (0x01, 0x02) and len(payload) >= 4:
            io = int.from_bytes(payload[2:4], "little")
            what = IO_TYPES.get(io, f"unknown device 0x{io:04x}")
            detail = f"port {port}: attached {what}" + (" (virtual)" if event == 2 else "")
        else:
            detail = f"port {port}: event {event}"

    elif mtype == 0x05 and len(payload) >= 2:  # error
        detail = f"error for cmd 0x{payload[0]:02x}, code 0x{payload[1]:02x}"

    return f"{name}: {detail}" if detail else f"{name}: {hx(payload)}"


# ---- the probe session ----------------------------------------------------------
class Probe:
    def __init__(self, client: BleakClient):
        self.client = client
        self.ports_used = set()

    def on_notify(self, _char, data: bytearray):
        print(f"\n  <- {hx(data)}\n     {decode(bytes(data))}")

    async def send(self, data: bytes):
        # length byte counts the whole packet including itself
        print(f"  -> {hx(data)}")
        await self.client.write_gatt_char(LWP3_CHAR, data, response=False)

    async def request_props(self):
        for prop in (0x01, 0x03, 0x04, 0x06, 0x08, 0x0A, 0x0B):
            # [len, hub, 0x01 = hub property, prop, 0x05 = "request update"]
            await self.send(bytes([0x05, 0x00, 0x01, prop, 0x05]))
            await asyncio.sleep(0.15)

    async def set_power(self, port: int, power: int):
        """Direct power. power: 1..100 fwd, -1..-100 back, 0 = float, 127 = brake.

        Uses WriteDirectModeData (0x51), mode 0. The older StartPower (0x01) form
        was accepted by this City Hub but did NOT move the motor.
        """
        self.ports_used.add(port)
        # [len, hub, 0x81 = port output cmd, port, 0x11 = start now + feedback,
        #  0x51 = WriteDirectModeData, mode 0, power as signed byte]
        await self.send(bytes([0x08, 0x00, 0x81, port, 0x11, 0x51, 0x00, power & 0xFF]))

    async def shutdown(self):
        for port in self.ports_used:
            try:
                await self.set_power(port, 0)
            except Exception:
                pass


def parse_port(text: str) -> int:
    t = text.upper()
    return PORT_LETTERS[t] if t in PORT_LETTERS else int(t, 0)


HELP = """
  props                 re-request name/firmware/battery/etc.
  power <port> <-100..100>   run a motor (port: A,B,C,D or a number like 0, 0x32)
  brake <port>          actively stop that port
  stop                  float (power 0) every port you've used
  raw <hex bytes>       send exactly what you type, e.g.  raw 07 00 81 00 11 01 32
  quit
"""


async def repl(probe: Probe):
    loop = asyncio.get_running_loop()
    print(HELP)
    while probe.client.is_connected:
        line = await loop.run_in_executor(None, input, "lwp3> ")
        parts = line.split()
        if not parts:
            continue
        cmd, rest = parts[0].lower(), parts[1:]
        try:
            if cmd in ("quit", "exit", "q"):
                return
            elif cmd == "help":
                print(HELP)
            elif cmd == "props":
                await probe.request_props()
            elif cmd == "power":
                await probe.set_power(parse_port(rest[0]), int(rest[1]))
            elif cmd == "brake":
                await probe.set_power(parse_port(rest[0]), 127)
            elif cmd == "stop":
                await probe.shutdown()
            elif cmd == "raw":
                await probe.send(bytes(int(x, 16) for x in rest))
            else:
                print("unknown command, try 'help'")
        except (IndexError, ValueError, KeyError) as e:
            print(f"  bad arguments ({e!r}); try 'help'")
        await asyncio.sleep(0.3)  # let any reply print before the next prompt


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--address", help="skip scanning, connect to this address")
    ap.add_argument("--all", action="store_true", help="list all BLE devices, not just LEGO")
    ap.add_argument("--scan-time", type=float, default=8.0)
    args = ap.parse_args()

    address = await choose_hub(args)
    print(f"Connecting to {address} ...")

    def on_disconnect(_client):
        print("\n*** hub disconnected ***")

    # use_cached_services=False: Windows caches GATT tables per device and a stale
    # or partial cache can hide the LWP3 service. Force a fresh discovery.
    async with BleakClient(address, disconnected_callback=on_disconnect,
                           winrt={"use_cached_services": False}) as client:
        print("Connected. GATT table the hub reports:")
        found_lwp3 = False
        for svc in client.services:
            print(f"  service {svc.uuid}")
            for ch in svc.characteristics:
                print(f"    char {ch.uuid}  props={ch.properties}")
                if ch.uuid.lower() == LWP3_CHAR:
                    found_lwp3 = True
        if not found_lwp3:
            print("LWP3 characteristic NOT in the table above. If the table is empty or tiny,")
            print("remove the hub from Windows Settings > Bluetooth & devices, then retry.")
            return
        print("LWP3 characteristic found.")
        probe = Probe(client)
        await client.start_notify(LWP3_CHAR, probe.on_notify)
        await asyncio.sleep(1.0)       # hub announces already-attached devices on its own
        await probe.request_props()
        await asyncio.sleep(1.0)
        try:
            await repl(probe)
        finally:
            await probe.shutdown()     # never leave a motor running on exit / Ctrl-C


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nbye")
