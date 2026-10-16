# Development

[Back to the README](../README.md)

## Source layout

| Path | Contents |
| --- | --- |
| `src/wp_daemon.py` | BLE connection, control socket, and session accounting |
| `src/wp_client.py` | Command-line client for the daemon |
| `gnome-extension/` | GNOME Shell Quick Settings extension |
| `nix/` and `flake.nix` | Packaging and the NixOS module |
| `scripts/promo/` and `scripts/record-promo.py` | Isolated demo recording |
| `tests/` | Python, extension, and cursor-motion tests |

## Checks

```sh
nix develop -c python -m unittest discover -s tests
nix shell nixpkgs#nodejs -c node --test tests/test_extension.js tests/test_promo_motion.mjs
```

The D-Bus integration test uses a private bus and the real proxy to check that
BlueZ calls and notifications pass while unrelated services and name ownership
are blocked. It skips when the D-Bus tools are unavailable; to include it, run:

```sh
nix shell nixpkgs#xdg-dbus-proxy nixpkgs#dbus --command nix develop -c python -m unittest discover -s tests
```

Python tests exercise session accounting and command handling using mocked
connections. Extension tests execute the history-update methods with actor and
socket stubs, including asynchronous replies, session finalization, live duration,
and recovery around midnight. Cursor-motion tests check continuity at spline
joins and the straight history sweep.

The [demo recorder](promo.md) exercises actual GNOME pointer interactions in a
private desktop with a mocked daemon. It checks UI state and live totals during
the tour. These checks do not establish how real hardware behaves.

The extension declares support for GNOME Shell 45–50 in `metadata.json`.
The demo recording has been exercised with GNOME Shell 50; this is not a test
of every declared version.

## Running without the NixOS module

On Linux with Nix flakes enabled, BlueZ running, and permission to use its system
D-Bus interface, start the daemon in a terminal:

```sh
nix run github:ambiso/walkingpad-controller#walkingpad-daemon -- \
  --socket /tmp/walkingpad.sock --history ./walkingpad-history.jsonl
```

In another terminal, use the same socket path:

```sh
nix run github:ambiso/walkingpad-controller#walkingpad-client -- \
  --socket /tmp/walkingpad.sock status
```

Replace `status` with another client command as needed. Add `--address` and the
pad's Bluetooth address to the daemon command to select a specific pad. You
must keep the daemon running yourself or configure your own service; this does
not install or enable the GNOME extension. The extension expects the NixOS
module's socket path, `/run/walkingpad/daemon.sock`.
