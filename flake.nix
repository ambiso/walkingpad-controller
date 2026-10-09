# flake.nix — Nix flake exposing:
#   * overlays.default            the "module": a Nixpkgs overlay that registers
#                                 walkingpad-controller (defined in
#                                 walkingpad-controller.nix) in every Python package set
#   * devShells.<system>.default  a dev shell whose pythonEnv contains the package
#   * packages.<system>.walkingpad-controller
#                                 the package itself, buildable standalone
#
# Keep this file in the same directory as walkingpad-controller.nix; the overlay
# pulls it in via `callPackage ./walkingpad-controller.nix`.
#
# Usage:
#   nix develop                        # enter the dev shell
#   nix build .#walkingpad-controller  # build the package alone
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
      # callPackage on py-final resolves walkingpad-controller.nix's arguments
      # (bleak, bleak-retry-connector, buildPythonPackage, fetchurl, lib)
      # from that same extended Python set.
      # ------------------------------------------------------------------
      walkingpadOverlay = final: prev: {
        pythonPackagesExtensions = prev.pythonPackagesExtensions ++ [
          (py-final: py-prev: {
            walkingpad-controller = py-final.callPackage ./walkingpad-controller.nix { };
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

      # The package in isolation, e.g. for direct installation or CI checks.
      packages = forAllSystems (system:
        let pkgs = pkgsFor system; in {
          walkingpad-controller = pkgs.python3Packages.walkingpad-controller;
          default = pkgs.python3Packages.walkingpad-controller;
        });

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
              echo "walkingpad-controller ready. Try:"
              echo "  python -c 'import asyncio; from bleak import BleakScanner; print(asyncio.run(BleakScanner.discover()))'"
            '';
          };
        });
    };
}
