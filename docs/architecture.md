# Daemon and session accounting

[Back to the README](../README.md)

## Connection and control

The daemon maintains a BLE connection through BlueZ, implements the KingSmith
vendor unlock before issuing FTMS controls, and exposes a JSON-lines Unix socket
at `/run/walkingpad/daemon.sock`. The client and extension use that socket rather
than opening their own BLE links. Silent property reads check the connection
(default interval: 30 seconds); failures trigger reconnection. A start requested
while disconnected is queued and wakes the reconnect loop. The `reconnect`
command forces a fresh link.

The daemon also asks BlueZ to drop stale connections before discovery, since a
stale link can prevent the pad from advertising. Connection beeps can recur when
reconnecting; keeping the daemon running avoids opening a new connection for
every client command.

The NixOS module runs the daemon under a dynamic systemd user, with filesystem
hardening and only AF_UNIX sockets allowed. Bluetooth access goes through a
separate `xdg-dbus-proxy` service that permits calls and notifications for
`org.bluez`. The daemon cannot access the raw system bus, and the proxy runs
under a separate user. This still permits access to other Bluetooth devices
through BlueZ; it is not a restriction to the configured pad.
Clients use EXTERNAL authentication without an explicit UID, so the system bus
uses the proxy's kernel-provided credentials. The NixOS service sets
`BLEAK_DBUS_AUTH_UID=-1` for Bleak; the daemon's direct D-Bus calls use the same
authentication mode. The proxy remains unprivileged and enforces its filter.
The control socket is writable by every local user, so every local user can
control the belt. Session history is retrieved through the socket as well.

## Stored activity

Distance, duration, and steps are accumulated from per-frame counter deltas.
A new session starts from a zero baseline, including activity before the daemon
connected in its first sample. Ordinary counter wraps add to the totals; live
totals do not wrap with the pad's counters.

Finalized sessions are appended to `/var/lib/walkingpad/history.jsonl`.
`/var/lib/walkingpad/current.json` is an atomic snapshot of the open session,
updated at most once every five seconds while telemetry arrives. It is restored
at startup and resolved by subsequent telemetry, including idle telemetry.
Recent activity after the last snapshot can be lost in an unclean shutdown.
Accounting continues when persistence is disabled.

The CLI groups finalized records by their recorded local date and displays the
open session separately. The histogram adds the open session to its cached daily
totals and updates with live telemetry. Finalized records use the local date at
finalization; records are not split into individual days at midnight.

## Gaps and session identity

The pad's telemetry provides no session identifier. After a gap, the daemon
assumes continuity unless a modulo counter increment exceeds 3,600 seconds,
50,000 steps, or 50,000 metres. Such a jump finalizes the previous totals and
allows a new session to start from the incoming counters when walking.

This heuristic suits use with the computer present and short interruptions such
as a reboot. It cannot recover every session missed while disconnected:

- 500 steps before a gap and 600 afterward are counted as 100 additional steps,
  even if the pad stopped and started another session during the gap.
- More than an hour of continued walking during a gap may be treated as a reset,
  causing the old totals to overlap newly seeded totals.
- Complete counter wraps before the first sample or during long gaps cannot be
  reconstructed reliably.

## Status handling

The parser treats `02 01` as stop, `02 02` as pause, `01` as reset, and `03` as
safety stop. Stop and safety-stop frames finalize accounting; reset finalizes
and clears the baseline; pause preserves the open session. A stop command that
succeeds also finalizes accounting. Trailing telemetry after a known stop is
ignored until a start or reset establishes the next session.

If a `02` status frame omits its stop/pause parameter, the displayed belt state
becomes stopped but accounting stays open until a known stop or counter reset.

These are implemented parser behaviors, covered by mocked tests. In particular,
the safety-stop test injects a `03` frame; it does not demonstrate that the
KS-HD-Z1E actually emits one. There is no hardware trace in this repository
establishing that behavior.
