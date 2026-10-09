# walkingpad-controller.nix — derivation for walkingpad-controller.
# Keep this file next to flake.nix: the overlay there calls it via
# `callPackage ./walkingpad-controller.nix`.
#
# The argument list is what callPackage resolves from the Python package set:
# buildPythonPackage and fetchurl come from nixpkgs, while bleak and
# bleak-retry-connector are taken from the Python set the overlay extends,
# guaranteeing they are built against the same interpreter.

{ lib
, buildPythonPackage
, fetchurl
, bleak
, bleak-retry-connector
}:

buildPythonPackage rec {
  pname = "walkingpad-controller";
  version = "0.5.1";

  # Pure-Python wheel (py3-none-any) straight from PyPI: buildPythonPackage
  # unpacks and installs it directly, so no build backend (hatchling,
  # setuptools, ...) is needed at build time.
  format = "wheel";

  # The exact wheel URL from PyPI's file index (files.pythonhosted.org).
  src = fetchurl {
    url = "https://files.pythonhosted.org/packages/d5/77/621d35172e7e620ae38e22db5ac9d023c77981176f27ca437ec55ffd88c6/walkingpad_controller-0.5.1-py3-none-any.whl";
    # Placeholder — on the first build Nix prints the correct SRI hash; paste
    # it here and rebuild. Alternative:
    #   nix hash to-sri --type sha256 "$(nix-prefetch-url <wheel-url>)"
    hash = lib.fakeHash;
  };

  # Runtime dependencies from the wheel's requires_dist metadata:
  #   bleak >= 0.20.0, bleak-retry-connector >= 3.0.0
  # Both already exist in nixpkgs; callPackage injects the variants built
  # against this very interpreter.
  propagatedBuildInputs = [
    bleak
    bleak-retry-connector
  ];

  # The wheel ships no test suite; skip the check phase.
  doCheck = false;

  meta = {
    description = "Python library for controlling KingSmith WalkingPad treadmills over BLE (FTMS and legacy WiLink protocols)";
    homepage = "https://pypi.org/project/walkingpad-controller/";
    license = lib.licenses.mit;
  };
}
