"""Exercise the real proxy against a private bus; no pad or host bus is used."""
import asyncio
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

try:
    from dbus_fast import Message, MessageType
    from dbus_fast.aio import MessageBus
    from dbus_fast.service import ServiceInterface, method, signal
except ImportError:
    MessageBus = None


@unittest.skipUnless(MessageBus and shutil.which("xdg-dbus-proxy") and
                     shutil.which("dbus-daemon"), "needs dbus-fast and D-Bus tools")
class ProxyTests(unittest.IsolatedAsyncioTestCase):
    async def test_bluez_calls_and_notifications_but_no_other_services(self):
        class Example(ServiceInterface):
            def __init__(self):
                super().__init__("org.example.Test")

            @method()
            def Ping(self) -> 's':
                return "pong"

            @signal()
            def Updated(self, value: 's') -> 's':
                return value

        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "bus.conf"
            config.write_text(f"""<busconfig><type>session</type>
              <listen>unix:tmpdir={directory}</listen><auth>EXTERNAL</auth>
              <policy context="default"><allow own="*"/>
                <allow send_destination="*"/><allow receive_sender="*"/>
              </policy></busconfig>""")
            bus_process = subprocess.Popen(
                ["dbus-daemon", f"--config-file={config}", "--nofork", "--print-address=1"],
                stdout=subprocess.PIPE, text=True)
            self.addCleanup(bus_process.stdout.close)
            self.addCleanup(bus_process.wait)
            self.addCleanup(bus_process.terminate)
            address = bus_process.stdout.readline().strip()
            self.assertTrue(address.startswith("unix:"), address)
            buses = []
            try:
                bluez = await MessageBus(bus_address=address).connect()
                buses.append(bluez)
                interface = Example()
                bluez.export("/org/bluez/test", interface)
                await bluez.request_name("org.bluez")
                other = await MessageBus(bus_address=address).connect()
                buses.append(other)
                other.export("/org/bluez/test", Example())
                await other.request_name("org.example.Other")
                read_fd, write_fd = os.pipe()
                proxy = subprocess.Popen([
                    "xdg-dbus-proxy", address, str(Path(directory) / "bus"),
                    "--filter", "--talk=org.bluez", f"--fd={write_fd}"],
                    pass_fds=[write_fd])
                os.close(write_fd)
                try:
                    ready = await asyncio.wait_for(
                        asyncio.to_thread(os.read, read_fd, 1), 5)
                    self.assertEqual(ready, b"x")
                    client = await MessageBus(
                        bus_address=f"unix:path={directory}/bus").connect()
                    buses.append(client)
                    for name, expected in [("org.bluez", MessageType.METHOD_RETURN),
                                           ("org.example.Other", MessageType.ERROR),
                                           (other.unique_name, MessageType.ERROR)]:
                        reply = await client.call(Message(destination=name,
                            path="/org/bluez/test", interface="org.example.Test", member="Ping"))
                        self.assertEqual(reply.message_type, expected)
                        if name == "org.bluez":
                            self.assertEqual(reply.body, ["pong"])
                    reply = await client.call(Message(destination="org.freedesktop.DBus",
                        path="/org/freedesktop/DBus", interface="org.freedesktop.DBus",
                        member="RequestName", signature="su", body=["org.example.Attacker", 0]))
                    self.assertEqual(reply.message_type, MessageType.ERROR)
                    updated = asyncio.Event()
                    client.add_message_handler(lambda msg: updated.set()
                        if msg.message_type == MessageType.SIGNAL and msg.member == "Updated" else None)
                    # Bleak also subscribes without a sender constraint.
                    for sender in ["sender='org.bluez',", ""]:
                        rule = f"type='signal',{sender}interface='org.example.Test'"
                        reply = await client.call(Message(destination="org.freedesktop.DBus",
                            path="/org/freedesktop/DBus", interface="org.freedesktop.DBus",
                            member="AddMatch", signature="s", body=[rule]))
                        self.assertEqual(reply.message_type, MessageType.METHOD_RETURN)
                        updated.clear()
                        interface.Updated("telemetry")
                        await asyncio.wait_for(updated.wait(), 5)
                        reply = await client.call(Message(destination="org.freedesktop.DBus",
                            path="/org/freedesktop/DBus", interface="org.freedesktop.DBus",
                            member="RemoveMatch", signature="s", body=[rule]))
                        self.assertEqual(reply.message_type, MessageType.METHOD_RETURN)
                finally:
                    os.close(read_fd)
                    proxy.terminate()
                    proxy.wait()
            finally:
                for bus in buses:
                    bus.disconnect()
                for bus in buses:
                    await bus.wait_for_disconnect()
