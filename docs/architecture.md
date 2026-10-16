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

## Command scheduling

A single priority queue owns vendor and FTMS control exchanges.
Each exchange includes its response, with at least 400 ms between writes. This
is a conservative scheduling choice; hardware has not established that spacing
is required across the two channels. Stop is selected before any ordinary
queued exchange, including work waiting for pacing. A request already sent is
drained or times out before another exchange begins.

Stop immediately invalidates unfinished Start sequences and queued speed
changes. Pause and newer Start requests also invalidate older movement work;
only the latest unsent speed target is written. Start's wake, countdown wait,
and optional speed adjustment do not hold the exchange queue. Each continuation
checks its movement generation before issuing further work. If Stop is requested
while disconnected, it remains queued and is attempted before a subsequent
queued Start on reconnection.

Start has an eight-second deadline from the request until the pad reports that
it has started. Expiry cancels the pending Start and clears `starting`; it does
not leave a request that could execute when the pad is powered on later. If the
link is still connected, expiry also queues Stop to cancel a potentially
accepted countdown. Ordinary background reconnection continues. A successful
Start without an explicit target speed leaves the pad's startup speed alone.

Notification waiters are installed immediately before writing, after pacing.
Vendor responses are matched by reply type, and FTMS responses by request
opcode; unsolicited property-change events cannot satisfy property reads.
Because neither response carries a transaction identifier, a timeout invalidates
the connection before any subsequent request can reuse it. Queued exchanges and
notification callbacks from older connection generations are discarded.

The extension takes device state only from the watch socket. Command replies
report results but cannot overwrite newer telemetry received on that stream.
Replies to older toggle actions cannot cancel a newer countdown. Stop/pause
notifications update observed belt state but do not invalidate newer user
requests; an explicit Stop or Pause request cancels pending movement work.

These scheduling interleavings are covered by mocked regression tests; they
have not yet been exercised on the physical pad. The successful auto-stop
exchange is separately documented in [pad settings](pad-settings.md).

## Bluetooth access

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

## Starting from standby

Before Start on a stopped belt, the daemon reads vendor property 10. When
mode bits 5–7 report sleep (2), it writes manual mode (0), preserving all other
bits, and checks that the pad reports manual mode before sending FTMS Start.
A refused wake returns failure without sending Start. Already moving belts
continue to use the existing speed-change path.

Observed on KS-HD-Z1E firmware `J41_V301.10.22`: Start in standby returned
FTMS result 4; waking with the physical remote changed property 10 from
`0x0240` to `0x0200`, after which Start worked. The automatic property write
and refusal paths are covered by mocked tests; the automatic wake sequence
still needs a hardware test.
