# WalkingPad daemon, socket client, GNOME extension, and development shell.
# Run the client against the installed service: nix run . -- status

{
  description = "walkingpad-controller (KingSmith WalkingPad BLE/FTMS control) dev shell";

  # Use your usual nixpkgs ref; pinned here for reproducibility.
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      # Flake-wide helpers: iterate over the systems you care about.
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAllSystems = nixpkgs.lib.genAttrs systems;

      pkgsFor = system: import nixpkgs { inherit system; };
    in
    {
      packages = forAllSystems (system:
        let
          pkgs = pkgsFor system;

          # The daemon. It deliberately does not use the
          # walkingpad-controller library: Z1-series firmware gates all FTMS
          # control behind a vendor unlock handshake on the KingSmith
          # supplement service, which the library never sends — its
          # REQUEST_CONTROL times out and stop/start answer result 5
          # "control not permitted" while the belt keeps running. The daemon
          # speaks the wire protocol itself (see src/wp_daemon.py).
          walkingpad-daemon = pkgs.writers.writePython3Bin "walkingpad-daemon" {
            libraries = [ pkgs.python3Packages.bleak ];
            flakeIgnore = [ "E501" ];
          } (builtins.readFile ./src/wp_daemon.py);

          # The client: pure standard library, talks JSON lines to the
          # daemon's Unix socket.
          walkingpad-client = pkgs.writers.writePython3Bin "walkingpad-client" {
            flakeIgnore = [ "E501" ];
          } (builtins.readFile ./src/wp_client.py);

          # Shell completions for the client, generated from its argparse
          # parser at build time (shtab) so they cannot drift from the
          # CLI. shtab is a build-time dependency only; the client stays
          # pure standard library.
          walkingpad-client-completions = pkgs.stdenv.mkDerivation {
            pname = "walkingpad-client-completions";
            version = "0.1.0";
            src = ./src;
            nativeBuildInputs = [
              (pkgs.python3.withPackages (ps: [ ps.shtab ]))
            ];
            dontConfigure = true;
            dontInstall = true;
            buildPhase = ''
              python generate-completions.py $out
            '';
          };

          # GNOME Shell extension: a Quick Settings toggle + speed slider
          # fed by the daemon's watch stream. No build step; the extension
          # is plain ESM JavaScript plus a metadata file and a symbolic
          # icon. Installed under its uuid so `gnome-extensions enable
          # walkingpad@ambiso` finds it once it is on XDG_DATA_DIRS via
          # environment.systemPackages (the NixOS module wires this up
          # behind services.walkingpad.gnomeExtension).
          walkingpad-gnome-extension = pkgs.stdenv.mkDerivation {
            pname = "walkingpad-gnome-shell-extension";
            version = "0.1.0";
            src = ./gnome-extension;
            installPhase = ''
              runHook preInstall
              mkdir -p "$out/share/gnome-shell/extensions/walkingpad@ambiso"
              cp -r "$src"/. "$out/share/gnome-shell/extensions/walkingpad@ambiso/"
              cp ${./LICENSE} "$out/share/gnome-shell/extensions/walkingpad@ambiso/LICENSE"
              runHook postInstall
            '';
            passthru.extensionUuid = "walkingpad@ambiso";
            meta = {
              description = "GNOME Shell Quick Settings extension for the WalkingPad daemon";
              license = pkgs.lib.licenses.eupl12;
            };
          };

          promoWallpaper = pkgs.runCommand "walkingpad-promo-wallpaper" {
            nativeBuildInputs = [ pkgs.libjxl ];
          } ''
            mkdir -p "$out"
            djxl ${pkgs.gnome-backgrounds}/share/backgrounds/gnome/adwaita-d.jxl "$out/wallpaper.png"
            printf '%s' '<svg xmlns="http://www.w3.org/2000/svg" width="1920" height="1080" viewBox="0 0 1920 1080" preserveAspectRatio="xMidYMid slice"><image width="1920" height="1080" preserveAspectRatio="xMidYMid slice" href="wallpaper.png"/></svg>' > "$out/wallpaper.svg"
          '';

          promoSource = pkgs.runCommand "walkingpad-promo-source" { } ''
            mkdir -p "$out/scripts" "$out/gnome-extension"
            cp -r ${./scripts}/. "$out/scripts/"
            cp -r ${./gnome-extension}/. "$out/gnome-extension/"
          '';

          walkingpad-promo = pkgs.writeShellApplication {
            name = "walkingpad-promo";
            runtimeInputs = [
              pkgs.gnome-shell pkgs.glib pkgs.dbus pkgs.pipewire
              pkgs.wireplumber pkgs.ffmpeg
              (pkgs.python3.withPackages (ps: [ ps.pygobject3 ]))
            ];
            text = ''
              export GI_TYPELIB_PATH=${pkgs.gst_all_1.gstreamer.out}/lib/girepository-1.0:"''${GI_TYPELIB_PATH:-}"
              export GST_PLUGIN_SYSTEM_PATH_1_0=${pkgs.lib.makeSearchPath "lib/gstreamer-1.0" [ pkgs.gst_all_1.gstreamer.out pkgs.gst_all_1.gst-plugins-base pkgs.gst_all_1.gst-plugins-good pkgs.gst_all_1.gst-plugins-bad pkgs.pipewire ]}:"''${GST_PLUGIN_SYSTEM_PATH_1_0:-}"
              export WALKINGPAD_DEMO_WALLPAPER=${promoWallpaper}
              export WALKINGPAD_DEMO_DBUS_CONFIG=${pkgs.dbus}/share/dbus-1/session.conf
              export XDG_DATA_DIRS=${pkgs.gnome-shell}/share/gsettings-schemas/gnome-shell-${pkgs.gnome-shell.version}:${pkgs.gsettings-desktop-schemas}/share/gsettings-schemas/gsettings-desktop-schemas-${pkgs.gsettings-desktop-schemas.version}:${pkgs.gnome-shell}/share:"''${XDG_DATA_DIRS:-/usr/local/share:/usr/share}"
              exec python ${promoSource}/scripts/record-promo.py "$@"
            '';
          };

        in {
          walkingpad-daemon = walkingpad-daemon;
          # The client with fish/bash/zsh completions merged in.
          walkingpad-client = pkgs.buildEnv {
            name = "walkingpad-client";
            paths = [ walkingpad-client walkingpad-client-completions ];
          };
          walkingpad-gnome-extension = walkingpad-gnome-extension;
          walkingpad-promo = walkingpad-promo;
          default = self.packages.${system}.walkingpad-client;
        });

      # NixOS module: services.walkingpad — the daemon as a systemd service
      # with tight sandboxing (DynamicUser, AF_UNIX only, ...) plus the
      # client CLI on PATH. self is handed to the module file at import time
      # so option defaults reference this flake's own packages without
      # requiring the overlay on the host.
      nixosModules.default = import ./nix/walkingpad-module.nix self;

      # Development needs the same BLE library as the daemon.
      devShells = forAllSystems (system:
        let
          pkgs = pkgsFor system;

          pythonEnv = pkgs.python3.withPackages (ps: [
            ps.bleak
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
