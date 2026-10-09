"""walkingpad-client — CLI front end for the walkingpad daemon.

Talks JSON lines over the daemon's Unix socket, prints one JSON reply per
command, and exits non-zero when the daemon reports an error, so it can be
scripted or bound to hotkeys. Pure standard library — no Python packages.
"""

import argparse
import asyncio
import json
import sys

DEFAULT_SOCKET = "/run/walkingpad/daemon.sock"


async def round_trip(socket_path, request, watch=False):
    reader, writer = await asyncio.open_unix_connection(socket_path)
    try:
        writer.write((json.dumps(request) + "\n").encode())
        await writer.drain()
        line = await reader.readline()
        if not line:
            raise RuntimeError("daemon closed the connection")
        reply = json.loads(line)
        while watch:
            # The daemon pushes one JSON state line per update until this
            # side hangs up.
            line = await reader.readline()
            if not line:
                break
            print(line.decode().strip(), flush=True)
        return reply
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass


def build_request(args):
    if args.command == "ping":
        return {"cmd": "ping"}, False
    if args.command == "status":
        return {"cmd": "status"}, False
    if args.command == "watch":
        return {"cmd": "watch"}, True
    if args.command == "properties":
        return {"cmd": "properties"}, False
    if args.command == "start":
        return {"cmd": "start", "speed": args.speed}, False
    if args.command == "speed":
        return {"cmd": "speed", "speed": args.speed}, False
    if args.command == "stop":
        return {"cmd": "stop"}, False
    if args.command == "pause":
        return {"cmd": "pause"}, False
    if args.command == "set-buzzer":
        return {"cmd": "set-buzzer", "value": args.value}, False
    raise AssertionError(args.command)


def parse_args():
    parser = argparse.ArgumentParser(
        prog="walkingpad-client",
        description="Control the walkingpad daemon (persistent WalkingPad link)")
    parser.add_argument("--socket", default=DEFAULT_SOCKET,
                        help="path of the daemon control socket")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("ping", help="check that the daemon answers")
    sub.add_parser("status", help="connection state and last telemetry")
    sub.add_parser("watch", help="stream state updates until interrupted")
    sub.add_parser("properties", help="dump vendor property records")
    p_start = sub.add_parser("start", help="start the belt")
    p_start.add_argument("--speed", type=float, help="target speed in km/h")
    p_speed = sub.add_parser("speed", help="set speed (starts belt if stopped)")
    p_speed.add_argument("speed", type=float, help="speed in km/h")
    sub.add_parser("stop", help="stop the belt (finalizes the pad session)")
    sub.add_parser("pause", help="pause the belt (keeps the session open)")
    p_buzzer = sub.add_parser("set-buzzer", help="toggle the pad's buzzer bit")
    p_buzzer.add_argument("value", choices=["on", "off"])
    return parser.parse_args()


def main():
    args = parse_args()
    request, watch = build_request(args)
    try:
        reply = asyncio.run(round_trip(args.socket, request, watch))
    except (OSError, RuntimeError) as exc:
        print("walkingpad-client: %s" % exc, file=sys.stderr)
        sys.exit(2)
    if not watch:
        print(json.dumps(reply, indent=2))
    sys.exit(0 if reply.get("ok") else 1)


if __name__ == "__main__":
    main()
