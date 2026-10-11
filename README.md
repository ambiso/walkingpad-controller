# walkingpad-controller

Control your KingSmith WalkingPad from GNOME's Quick Settings and keep track
of how much you walk. Start or stop the belt, adjust its speed, and see your
walking history without opening the phone app.

![WalkingPad extension in action](docs/promo.avif)

The seven-day chart updates as you walk. Hover a day to see its total walking
time, steps, and distance. The demo uses sample activity and speeds up the final
rise to show the chart updating.

## What you need

This project is aimed at **NixOS with GNOME and Bluetooth**. The extension is
optional: you can also control the pad from the command line. The packages can
be [run manually on other Linux systems with Nix](docs/development.md#running-without-the-nixos-module);
the installation below uses the NixOS service module.

Development has used a **KingSmith KS-HD-Z1E**, firmware `J41_V301.10.22`.
Other models and firmware may behave differently. The extension declares
support for GNOME Shell 45–50; the demo runs on GNOME 50.

## Install on NixOS

Add this input to your system flake:

```nix
inputs.walkingpad.url = "github:ambiso/walkingpad-controller";
```

Include `walkingpad` in your outputs arguments and add its module to your
existing system's modules:

```nix
outputs = { self, nixpkgs, walkingpad, ... }: {
  nixosConfigurations.machine = nixpkgs.lib.nixosSystem {
    modules = [
      ./configuration.nix
      walkingpad.nixosModules.default
    ];
  };
};
```

In your NixOS configuration, enable Bluetooth, the service, and the extension:

```nix
hardware.bluetooth.enable = true;
services.walkingpad = {
  enable = true;
  gnomeExtension = true; # omit this for command-line use only

  # Optional: replace this example with your pad's actual Bluetooth address.
  deviceAddress = "AA:BB:CC:DD:EE:FF";
};
```

Find your pad's address by opening `bluetoothctl` and scanning:

```text
$ bluetoothctl
[bluetooth]# scan on
# Wait for a device named KS-HD-* and note its address.
[bluetooth]# scan off
[bluetooth]# quit
```

You can omit `deviceAddress` to
let the daemon discover a device whose name begins with `KS-`. Pinning the
address is recommended when more than one pad is nearby.

Rebuild using your usual `sudo nixos-rebuild switch` command, including your
normal `--flake` argument if needed. On GNOME Wayland,
log out and back in so Shell can discover the newly installed extension, then
enable it:

```sh
gnome-extensions enable walkingpad@ambiso
```

Open the top-right Quick Settings menu to find the WalkingPad controls.

## Using the pad

Turn on the WalkingPad toggle to start walking. The extension shows a 3–2–1
countdown, then waits for the pad to report that it is moving. Use the slider
to adjust speed and turn the toggle off to stop. While walking, the caption
shows session duration, steps, and distance; hover the chart for daily totals.

You can also use the installed command-line client:

```sh
walkingpad-client status
walkingpad-client start --speed 3.0
walkingpad-client speed 4.5
walkingpad-client pause
walkingpad-client stop
walkingpad-client history --days 7
```

`pause` keeps the session open; `stop` finalizes it. The CLI history shows totals
for finalized sessions and lists the open session separately. The extension's
chart includes live activity. History survives service restarts and reboots.

The daemon keeps the pad's single Bluetooth connection open. To use the KS Fit
phone app, release the connection first:

```sh
sudo systemctl stop walkingpad
# When you want desktop control again:
sudo systemctl start walkingpad
```

## Connection problems and history limits

The daemon reconnects automatically when the link drops. If the pad has gone
into standby, a start request also triggers reconnection. To reconnect manually
or inspect the service:

```sh
walkingpad-client reconnect
systemctl status walkingpad
journalctl -u walkingpad -b
```

If no pad is found, check that Bluetooth and the pad are on, the configured
address is correct, and the phone app has released its connection.

Keep the service running while you walk for the most reliable history. The pad
has no session identifier, so stopping and restarting it while the computer is
off can make separate sessions indistinguishable. Long gaps can also produce
inaccurate totals. See [how session history works](docs/architecture.md#gaps-and-session-identity)
for the accounting rules and limitations.

## More information

- [Development and tests](docs/development.md)
- [Daemon and session accounting](docs/architecture.md)
- [Recording and tuning the demo](docs/promo.md)

## License

Copyright © 2026 ambiso and contributors.

The project's original code is licensed under the
[European Union Public Licence 1.2](LICENSE) (SPDX: `EUPL-1.2`).
Third-party code and artwork retain their respective licenses.

## Credits

- **[slandau3/z1-walkingpad-mcp](https://github.com/slandau3/z1-walkingpad-mcp)** —
  a macOS client for the same pad; its
  protocol write-up (MIT) is the basis for the daemon's frame formats
  and the vendor unlock handshake.
- **[mcdax/walkingpad-controller](https://github.com/mcdax/walkingpad-controller)** —
  the Python library (MIT) this repo packages; the one-shot CLI is
  built on it.
- **[anativ/R1-Walkingpad-Controller](https://github.com/anativ/R1-Walkingpad-Controller)** —
  an additional protocol reference.
- **duttke.de** — the Web Bluetooth implementation of the Z1 protocol.

## AI assistance

This project was developed with substantial AI assistance, including Mistral Vibe
and OpenAI Codex, using the [credited protocol references](#credits). Development has
included use with a KS-HD-Z1E (firmware J41_V301.10.22), offline tests, and mocked
demo sessions. Individual behaviors should not be assumed to have been verified
on hardware solely because the parser or a test covers them.
