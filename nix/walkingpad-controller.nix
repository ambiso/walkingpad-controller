# walkingpad-module.nix — NixOS module for the WalkingPad daemon.
#
# Imported with the flake's `self` so option defaults can point at this
# flake's own packages without requiring the overlay on the host:
#
#   nixosModules.default = import ./nix/walkingpad-module.nix self;
#
# The module adds:
#   * options under services.walkingpad
#   * a systemd service running walkingpad-daemon under a dynamic user with
#     tight sandboxing. The daemon talks to the pad through bluetoothd on
#     the D-Bus system bus — the only interface it needs is one Unix socket,
#     so the unit gets AF_UNIX only, no devices, no network, read-only
#     everything. This supersedes the bubblewrap wrapper for the service.
#   * the client CLI in environment.systemPackages
#
# Usage:
#   imports = [ walkingpad.nixosModules.default ];
#   services.walkingpad.enable = true;
#   # services.walkingpad.deviceAddress = "XX:XX:XX:XX:XX:XX";  # pin one belt
#   # then: walkingpad-client status | start --speed 3.0 | stop

self:
{ config, lib, pkgs, ... }:

let
  cfg = config.services.walkingpad;
in
{
  options.services.walkingpad = {
    enable = lib.mkEnableOption "WalkingPad BLE daemon (persistent, vendor-unlocked FTMS connection served over a Unix socket)";

    package = lib.mkOption {
      type = lib.types.package;
      default = self.packages.${pkgs.system}.walkingpad-daemon;
      description = "The daemon package.";
    };

    clientPackage = lib.mkOption {
      type = lib.types.package;
      default = self.packages.${pkgs.system}.walkingpad-client;
      description = "The client CLI package, added to environment.systemPackages.";
    };

    deviceAddress = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      description = "BLE address of the pad. null = discover by name prefix.";
    };

    devicePrefix = lib.mkOption {
      type = lib.types.str;
      default = "KS-";
      description = "BLE name prefix used during discovery.";
    };

    socketPath = lib.mkOption {
      type = lib.types.str;
      default = "/run/walkingpad/daemon.sock";
      description = "Path of the daemon's JSON-lines control socket.";
    };

    keepalive = lib.mkOption {
      type = lib.types.ints.unsigned;
      default = 30;
      description = "Seconds between silent link checks (0 disables).";
    };
  };

  config = lib.mkIf cfg.enable {
    systemd.services.walkingpad = {
      description = "WalkingPad Z1-series BLE control daemon";
      wantedBy = [ "multi-user.target" ];
      # bluetoothd must be up before discovery can succeed; dbus is the
      # transport to it.
      after = [ "bluetooth.service" "dbus.service" ];
      wants = [ "bluetooth.service" ];

      serviceConfig = {
        ExecStart = lib.concatStringsSep " " ([
          "${cfg.package}/bin/walkingpad-daemon"
          "--socket ${cfg.socketPath}"
          "--prefix ${cfg.devicePrefix}"
          "--keepalive ${toString cfg.keepalive}"
        ] ++ lib.optionals (cfg.deviceAddress != null) [
          "--address ${cfg.deviceAddress}"
        ]);

        # bleak reaches the pad only through bluetoothd on the system bus.
        Environment = [ "DBUS_SYSTEM_BUS_ADDRESS=unix:path=/run/dbus/system_bus_socket" ];

        Restart = "always";
        RestartSec = "5";

        # /run/walkingpad is created for the dynamic user; the socket inside
        # is world-writable (0600 would lock out the calling user), which is
        # acceptable for a treadmill on a single-user machine.
        RuntimeDirectory = "walkingpad";
        RuntimeDirectoryMode = "0755";
        Umask = "0000";

        # --- isolation ---------------------------------------------------
        DynamicUser = true;
        NoNewPrivileges = true;
        CapabilityBoundingSet = [ "" ];
        DevicePolicy = "closed";
        IPAddressDeny = "any";
        LockPersonality = true;
        MemoryDenyWriteExecute = true;
        PrivateDevices = true;
        PrivateTmp = true;
        ProtectClock = true;
        ProtectControlGroups = true;
        ProtectHome = true;
        ProtectHostname = true;
        ProtectKernelLogs = true;
        ProtectKernelModules = true;
        ProtectKernelTunables = true;
        ProtectProc = "invisible";
        ProtectSystem = "strict";
        RestrictAddressFamilies = [ "AF_UNIX" ];
        RestrictNamespaces = true;
        RestrictRealtime = true;
        RestrictSUIDSGID = true;
        SystemCallArchitectures = "native";
        SystemCallFilter = [ "@system-service" ];
      };
    };

    environment.systemPackages = [ cfg.clientPackage ];
  };
}
