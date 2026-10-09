# flake.nix — Nix flake exposing:
#   * overlays.default            the "module": a Nixpkgs overlay that registers
#                                 walkingpad-controller (defined in
#                                 nix/walkingpad-controller.nix) in every Python package set
#   * devShells.<system>.default  a dev shell whose pythonEnv contains the package
#   * packages.<system>.walkingpad-controller
#                                 the package itself, buildable standalone
#   * packages.<system>.walkingpad-cli
#                                 small CLI (scan/info/start/speed/stop) built
#                                 from the package; source lives in
#                                 nix/wp_cli.py, pulled in via builtins.readFile
#   * packages.<system>.walkingpad-sandboxed
#                                 the CLI under bubblewrap, isolated except for
#                                 the host's D-Bus system bus socket
#   * packages.<system>.walkingpad-daemon
#                                 persistent daemon: holds the pad's single BLE
#                                 link, performs the KingSmith vendor unlock
#                                 the library lacks (without it control-point
#                                 commands are refused with result 5), serves a
#                                 JSON-lines control socket
#   * packages.<system>.walkingpad-client
#                                 pure-stdlib client CLI for that socket
#   * nixosModules.default         services.walkingpad: the daemon as a
#                                 systemd service with tight sandboxing
#                                 (DynamicUser, AF_UNIX only), client on PATH
#
# Usage:
#   nix develop                          # enter the dev shell
#   nix build .#walkingpad-controller    # build the package alone
#   nix run .#walkingpad-sandboxed -- scan
#   nix run .#walkingpad-sandboxed -- info
#   nix run .#walkingpad-sandboxed -- start --speed 3.0
#   nix run .#walkingpad-sandboxed -- speed 4.0
#   nix run .#walkingpad-sandboxed -- stop
#
# Inside the dev shell, iterate on the CLI source directly (no rebuild):
#   python nix/wp_cli.py scan
#
# Daemon + client (persistent link; connect/disconnect beeps happen once
# per service start instead of once per command):
#   nix run .#walkingpad-daemon -- --socket /tmp/walkingpad.sock
#   nix run .#walkingpad-client -- --socket /tmp/walkingpad.sock status
#   nix run .#walkingpad-client -- --socket /tmp/walkingpad.sock start --speed 3.0
#   nix run .#walkingpad-client -- --socket /tmp/walkingpad.sock stop
#
# Or as a NixOS service (see nix/walkingpad-module.nix):
#   services.walkingpad.enable = true;   # then: walkingpad-client status
#
# Requires nixpkgs >= 23.05 (pythonPackagesExtensions was introduced then).

{
  description = "walkingpad-controller (KingSmith WalkingPad BLE/FTMS control) dev shell";

  # Use your usual nixpkgs ref; pinned here for reproducibility.
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      # Flake-wide helpers: iterate over the systems you care about.
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAllSystems = nixpkgs.lib.genAttrs systems;

      # ------------------------------------------------------------------
      # The module: a Nixpkgs overlay registering walkingpad-controller in
      # the Python package sets.
      #
      # pythonPackagesExtensions is the mechanism the nixpkgs Python manual
      # prescribes for extending *every* interpreter's package set
      # (python3Packages, python312Packages, ...) from one place, so the
      # package resolves regardless of which interpreter the consumer uses.
      #
      # callPackage on py-final resolves nix/walkingpad-controller.nix's
      # arguments (bleak, bleak-retry-connector, buildPythonPackage,
      # fetchurl, lib) from that same extended Python set.
      # ------------------------------------------------------------------
      walkingpadOverlay = final: prev: {
        pythonPackagesExtensions = prev.pythonPackagesExtensions ++ [
          (py-final: py-prev: {
            walkingpad-controller = py-final.callPackage ./nix/walkingpad-controller.nix { };
          })
        ];
      };

      # Nixpkgs evaluated per system with the overlay applied, so that
      # pkgs.python3Packages.walkingpad-controller resolves for the devShell.
      pkgsFor = system: import nixpkgs {
        inherit system;
        overlays = [ walkingpadOverlay ];
      };
    in
    {
      # Exported so the same module can be consumed from a NixOS configuration:
      #   nixpkgs.overlays = [ walkingpad-flake.overlays.default ];
      overlays.default = walkingpadOverlay;

      # The package in isolation, plus two conveniences:
      #   walkingpad-cli       — small CLI (scan / info / start / speed / stop)
      #                          built from the package
      #   walkingpad-sandboxed — the CLI wrapped in a bubblewrap sandbox that
      #                          bind-mounts the host's D-Bus system bus
      #                          socket (the only interface needed; org.bluez
      #                          lives on the host's system bus).
      packages = forAllSystems (system:
        let
          pkgs = pkgsFor system;

          # The CLI. Note there is no "pairing" step for FTMS WalkingPads:
          # the association model is unauthenticated Just Works, so connecting
          # is pairing — no PIN, no stored bond, no KS Fit app required.
          # Remember the single-client rule: the pad serves exactly one BLE
          # connection, so no phone app may be attached when the CLI connects.
          #
          # The script lives in nix/wp_cli.py at normal top-level
          # indentation: editors and flake8 lint it directly, and inside the
          # dev shell it runs without a rebuild. builtins.readFile splices it
          # into the derivation, where writers still flake8-checks the text
          # at build time.
          walkingpad-cli = pkgs.writers.writePython3Bin "walkingpad" {
            libraries = [ pkgs.python3Packages.walkingpad-controller ];
            flakeIgnore = [ "E501" ];
          } (builtins.readFile ./nix/wp_cli.py);

          # The daemon. It deliberately does not use the
          # walkingpad-controller library: Z1-series firmware gates all FTMS
          # control behind a vendor unlock handshake on the KingSmith
          # supplement service, which the library never sends — its
          # REQUEST_CONTROL times out and stop/start answer result 5
          # "control not permitted" while the belt keeps running. The daemon
          # speaks the wire protocol itself (see nix/wp_daemon.py).
          walkingpad-daemon = pkgs.writers.writePython3Bin "walkingpad-daemon" {
            libraries = [ pkgs.python3Packages.bleak ];
            flakeIgnore = [ "E501" ];
          } (builtins.readFile ./nix/wp_daemon.py);

          # The client: pure standard library, talks JSON lines to the
          # daemon's Unix socket.
          walkingpad-client = pkgs.writers.writePython3Bin "walkingpad-client" {
            flakeIgnore = [ "E501" ];
          } (builtins.readFile ./nix/wp_client.py);

          # bubblewrap launcher: filesystem/process/network isolation while
          # exposing exactly one host resource — the system bus socket, which
          # is the only interface the CLI needs. "$@" forwards subcommands and
          # options to the CLI inside the sandbox.
          walkingpad-sandboxed = pkgs.writeShellScriptBin "walkingpad-sandboxed" ''
            exec ${pkgs.bubblewrap}/bin/bwrap \
              --unshare-all \
              --die-with-parent \
              --proc /proc \
              --dev /dev \
              --tmpfs /tmp \
              --ro-bind /nix /nix \
              --ro-bind-try /run/dbus/system_bus_socket /run/dbus/system_bus_socket \
              --setenv DBUS_SYSTEM_BUS_ADDRESS unix:path=/run/dbus/system_bus_socket \
              ${walkingpad-cli}/bin/walkingpad "$@"
          '';
        in {
          walkingpad-controller = pkgs.python3Packages.walkingpad-controller;
          walkingpad-cli = walkingpad-cli;
          walkingpad-daemon = walkingpad-daemon;
          walkingpad-client = walkingpad-client;
          walkingpad-sandboxed = walkingpad-sandboxed;
          default = walkingpad-sandboxed;
        });

      # NixOS module: services.walkingpad — the daemon as a systemd service
      # with tight sandboxing (DynamicUser, AF_UNIX only, ...) plus the
      # client CLI on PATH. self is handed to the module file at import time
      # so option defaults reference this flake's own packages without
      # requiring the overlay on the host.
      nixosModules.default = import ./nix/walkingpad-module.nix self;

      # The dev shell: mkShell wrapping a pythonEnv whose only extra package
      # is the one the overlay provides; propagatedBuildInputs (bleak and
      # friends) come along automatically through withPackages.
      devShells = forAllSystems (system:
        let
          pkgs = pkgsFor system;

          # A self-contained interpreter with walkingpad-controller on
          # sys.path — no venv, no pip.
          pythonEnv = pkgs.python3.withPackages (ps: [
            ps.walkingpad-controller
          ]);
        in
        {
          default = pkgs.mkShell {
            packages = [
              pythonEnv
              pkgs.bluez # bluetoothctl, for scanning and debugging the pad
            ];

            shellHook = ''
              # dbus-fast falls back to /var/run/dbus/system_bus_socket when
              # DBUS_SYSTEM_BUS_ADDRESS is unset. In environments where
              # /var/run is not a symlink to /run (containers, some chroots)
              # that path is dead and connect() fails with ENOENT. Pin the
              # canonical path explicitly; harmless when the fallback would
              # have been correct anyway.
              export DBUS_SYSTEM_BUS_ADDRESS=unix:path=/run/dbus/system_bus_socket
              echo "walkingpad-controller ready. Try:"
              echo "  python -c 'import asyncio; from bleak import BleakScanner; print(asyncio.run(BleakScanner.discover()))'"
            '';
          };
        });
    };
}
