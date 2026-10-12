# walkingpad-module.nix — NixOS module: services.walkingpad.
#
# Wiring: flake.nix exposes
#   nixosModules.default = import ./nix/walkingpad-module.nix self;
# so this file receives the flake itself as `self` at import time. That
# lets the option defaults below point at this flake's own packages
# (walkingpad-daemon, walkingpad-client) without requiring the overlay on
# the host — importing this module is sufficient, any NixOS configuration
# can do it directly.
#
# What the module provides:
#   * systemd.services.walkingpad — the persistent daemon holding the
#     pad's single BLE link (KingSmith vendor unlock included, which the
#     walkingpad-controller library lacks). Runs under a DynamicUser in
#     an AF_UNIX-only sandbox: the only socket family the daemon ever
#     needs is AF_UNIX (the D-Bus system bus for BlueZ, plus its own
#     JSON-lines control socket), so everything else is denied.
#   * environment.systemPackages — walkingpad-client, so any local user
#     can steer the belt:
#       walkingpad-client status
#       walkingpad-client start --speed 3.0
#       walkingpad-client stop
#
# The pad serves exactly one BLE connection: while this service runs the
# KS Fit app cannot attach. `systemctl stop walkingpad` frees the belt for
# the app again.

self:
{
  config,
  lib,
  pkgs,
  ...
}:

let
  cfg = config.services.walkingpad;

  # This flake's own packages; no host overlay involved. stdenv is used
  # for the system name because pkgs.system is deprecated in nixpkgs.
  system = pkgs.stdenv.hostPlatform.system;
  daemon = self.packages.${system}.walkingpad-daemon;
  client = self.packages.${system}.walkingpad-client;
  gnomeExtension = self.packages.${system}.walkingpad-gnome-extension;

  # Fixed control-socket path. walkingpad-client's DEFAULT_SOCKET is
  # baked to the same value, so the two can never drift apart; the daemon
  # chmods the socket 0o666 so every local user may steer the belt.
  socketPath = "/run/walkingpad/daemon.sock";

  # Session history: one JSON line per finalized pad session. StateDirectory
  # makes it survive service restarts and reboots (unlike the socket, which
  # lives in the tmpfs RuntimeDirectory); the file is created 0644 (UMask
  # below) and the directory is traversable, so any user's
  # `walkingpad-client history` can read the totals.
  historyPath = "/var/lib/walkingpad/history.jsonl";
  proxySocket = "/run/walkingpad-dbus/bus";
in
{
  options.services.walkingpad = {
    enable = lib.mkEnableOption "the WalkingPad control daemon (persistent BLE link with Unix-socket control)";

    deviceAddress = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      description = "BLE address of the pad. When null, the daemon discovers the first device whose advertised name starts with the KS- prefix. Pin the address when several belts are in range.";
    };

    keepalive = lib.mkOption {
      type = lib.types.number;
      default = 30;
      description = "Seconds between silent link checks (vendor property reads) that keep the BLE connection from idling out. 0 disables them.";
    };

    gnomeExtension = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = ''
        Install the GNOME Shell Quick Settings extension (WalkingPad
        start/stop toggle and speed slider) for every user. Installing is
        not enabling: each user turns it on once with
        `gnome-extensions enable walkingpad@ambiso` (and Wayland needs a
        re-login before the shell picks a newly installed extension up).
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    users.groups.walkingpad-dbus = { };

    # Separate UID: compromised Python cannot modify the proxy process.
    systemd.services.walkingpad-dbus = {
      description = "BlueZ-only D-Bus proxy for WalkingPad";
      requires = [ "dbus.service" ];
      after = [ "dbus.service" ];
      partOf = [ "walkingpad.service" ];
      serviceConfig = {
        ExecStart = "${pkgs.xdg-dbus-proxy}/bin/xdg-dbus-proxy unix:path=/run/dbus/system_bus_socket ${proxySocket} --filter --talk=org.bluez";
        ExecStartPost = pkgs.writeShellScript "walkingpad-dbus-ready" ''
          while [ ! -S ${proxySocket} ]; do ${pkgs.coreutils}/bin/sleep 0.01; done
          ${pkgs.coreutils}/bin/chmod 0660 ${proxySocket}
        '';
        TimeoutStartSec = 10;
        Restart = "on-failure";
        RestartSec = 1;
        DynamicUser = true;
        # The dynamic user must not share a name with the static access group.
        User = "walkingpad-proxy";
        Group = "walkingpad-dbus";
        RuntimeDirectory = "walkingpad-dbus";
        RuntimeDirectoryMode = "0750";
        UMask = "0007";
        NoNewPrivileges = true;
        CapabilityBoundingSet = "";
        RestrictAddressFamilies = [ "AF_UNIX" ];
        PrivateNetwork = true;
        PrivateTmp = true;
        PrivateDevices = true;
        ProtectHome = true;
        ProtectSystem = "strict";
        ProtectProc = "invisible";
        RestrictNamespaces = true;
        SystemCallArchitectures = "native";
      };
    };

    systemd.services.walkingpad = {
      description = "WalkingPad BLE control daemon (persistent link, JSON-lines control socket)";

      # BlueZ is reached through the D-Bus system bus, which is up once
      # dbus.service is; bluetooth.target additionally waits for the
      # adapter. The daemon needs no network, only those.
      after = [ "bluetooth.target" "walkingpad-dbus.service" ];
      # Let the daemon retry even if the proxy initially fails to start.
      wants = [ "bluetooth.target" "walkingpad-dbus.service" ];
      wantedBy = [ "multi-user.target" ];

      environment.DBUS_SYSTEM_BUS_ADDRESS = "unix:path=${proxySocket}";

      serviceConfig = {
        ExecStart = lib.concatStringsSep " " (
          [
            "${daemon}/bin/walkingpad-daemon"
            "--socket ${socketPath}"
            "--history ${historyPath}"
            "--keepalive ${toString cfg.keepalive}"
          ]
          ++ lib.optionals (cfg.deviceAddress != null) [
            "--address ${cfg.deviceAddress}"
          ]
        );

        # The daemon reconnects to the pad internally (with delay);
        # systemd only restarts when the process itself dies.
        Restart = "on-failure";
        RestartSec = 5;

        # -- sandbox ------------------------------------------------------
        # No persistent identity: a fresh UID is allocated per boot.
        DynamicUser = true;
        SupplementaryGroups = [ "walkingpad-dbus" ];
        # Hide the real bus, including the /var/run alias. A private network
        # namespace also prevents bypass through host abstract Unix sockets.
        InaccessiblePaths = [ "/run/dbus" ];
        PrivateNetwork = true;
        ProtectProc = "invisible";

        # /run/walkingpad is created for the dynamic user and owned by it;
        # mode 0755 keeps the directory traversable for client users, who
        # reach the 0o666 socket inside it.
        RuntimeDirectory = "walkingpad";
        RuntimeDirectoryMode = "0755";

        # Persistent home for the session history (ProtectSystem=strict
        # leaves /var read-only without this); traversable so every user
        # can read the 0644 records for `walkingpad-client history`.
        StateDirectory = "walkingpad";
        StateDirectoryMode = "0755";

        # AF_UNIX only: the D-Bus system bus and the control socket are
        # the daemon's entire network surface; deny the rest, including IP.
        RestrictAddressFamilies = [ "AF_UNIX" ];
        IPAddressDeny = "any";

        # Read-only view of the system apart from the runtime directory;
        # no device nodes either — Bluetooth goes through D-Bus, not raw HCI.
        ProtectSystem = "strict";
        ProtectHome = true;
        PrivateTmp = true;
        PrivateDevices = true;
        DevicePolicy = "closed";

        # Stock hardening set for a daemon that needs no privileges.
        NoNewPrivileges = true;
        CapabilityBoundingSet = "";
        ProtectKernelTunables = true;
        ProtectKernelModules = true;
        ProtectKernelLogs = true;
        ProtectClock = true;
        ProtectHostname = true;
        ProtectControlGroups = true;
        RestrictNamespaces = true;
        RestrictRealtime = true;
        RestrictSUIDSGID = true;
        LockPersonality = true;
        SystemCallFilter = [ "@system-service" "~@privileged" "~@resources" ];
        SystemCallArchitectures = "native";
        UMask = "0022";
      };
    };

    # The client on PATH for every user: pure standard library, and its
    # only interface is the daemon socket above. The GNOME Shell
    # extension rides along when asked for; it reads the same socket.
    environment.systemPackages =
      [ client ]
      ++ lib.optionals cfg.gnomeExtension [ gnomeExtension ];
  };
}
