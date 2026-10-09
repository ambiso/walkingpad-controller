"""walkingpad — CLI for KingSmith WalkingPad treadmills over BLE/FTMS.

Consumed by flake.nix through writers.writePython3Bin, which reads this
file with builtins.readFile and flake8-checks it at build time (E501 is
ignored there). writers supplies the interpreter shebang itself, so this
file intentionally carries none — a stray `#!` past line 1 is flagged
E265. Inside the dev shell it runs directly, no rebuild:

    python nix/wp_cli.py scan
"""

import argparse
import asyncio
import sys

from bleak import BleakScanner
from walkingpad_controller import WalkingPadController

DEFAULT_PREFIX = "KS-"  # KingSmith BLE names, e.g. KS-HD-Z1E


async def find_device(address=None, prefix=DEFAULT_PREFIX, timeout=10.0):
    # Locate the pad: by BLE address if given, otherwise by
    # advertised-name prefix.
    if address:
        return await BleakScanner.find_device_by_address(address, timeout=timeout)

    def match(device, _adv):
        return device.name is not None and device.name.startswith(prefix)

    return await BleakScanner.find_device_by_filter(match, timeout=timeout)


async def connect(args):
    device = await find_device(args.address, args.prefix)
    if device is None:
        print("no WalkingPad found (power it on; the link icon should blink)",
              file=sys.stderr)
        sys.exit(1)
    controller = WalkingPadController(ble_device=device)
    await controller.connect()
    return controller


async def cmd_scan(_args):
    for d in await BleakScanner.discover(timeout=10.0):
        print(f"{d.address}  {d.name!r}")


async def cmd_info(args):
    c = await connect(args)
    print(f"protocol:  {c.protocol.value}")
    print(f"firmware:  {c.firmware_version or 'n/a'}")
    print(f"speed:     {c.min_speed}-{c.max_speed} km/h (step {c.speed_increment})")
    await c.update_state()
    s = c.status
    print(f"state:     belt_state={s.belt_state} speed={s.speed} km/h")
    print(f"session:   distance={s.distance} m duration={s.duration} s "
          f"steps={s.steps} kcal={s.calories}")
    await c.disconnect()


async def cmd_start(args):
    c = await connect(args)
    # Belt starts at minimum speed; the library handles the KS
    # START_OR_RESUME + stabilization quirk.
    await c.start()
    if args.speed:
        await c.set_speed(args.speed)
    await c.update_state()
    print(f"running at {c.status.speed} km/h")
    await c.disconnect()


async def cmd_speed(args):
    c = await connect(args)
    await c.set_speed(args.speed)  # starts the belt if stopped
    await c.update_state()
    print(f"speed set to {c.status.speed} km/h")
    await c.disconnect()


async def cmd_stop(args):
    c = await connect(args)
    await c.stop()
    print("belt stopped")
    await c.disconnect()


def main():
    p = argparse.ArgumentParser(prog="walkingpad")
    p.add_argument("--address", help="BLE address of the pad (skips name matching)")
    p.add_argument("--prefix", default=DEFAULT_PREFIX,
                   help="BLE name prefix to match during discovery")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("scan", help="list nearby BLE devices")
    sub.add_parser("info", help="connect; print protocol, firmware, status")
    p_start = sub.add_parser("start", help="start the belt")
    p_start.add_argument("--speed", type=float, help="target speed in km/h")
    p_speed = sub.add_parser("speed", help="set speed (starts belt if stopped)")
    p_speed.add_argument("speed", type=float, help="speed in km/h")
    sub.add_parser("stop", help="stop the belt")
    args = p.parse_args()
    handler = {"scan": cmd_scan, "info": cmd_info, "start": cmd_start,
               "speed": cmd_speed, "stop": cmd_stop}[args.cmd]
    asyncio.run(handler(args))


if __name__ == "__main__":
    main()
