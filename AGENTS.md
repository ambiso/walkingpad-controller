# Contributor guidance

- Keep code small and readable, with correctness taking priority over brevity.
- Keep the README focused on what users can do and how to get started. Put
  implementation details in `docs/` and keep documentation consistent with code.
- Distinguish behavior observed on hardware from protocol assumptions and
  behavior exercised only by mocked tests. Parser support alone does not prove
  that a pad emits a particular notification.
- Preserve existing extension features when refactoring. Removing or changing
  user-visible behavior should be part of the requested scope.
- Keep promo hooks, sample history, and accelerated telemetry isolated from
  production code. Locate promo controls through GNOME actors and their geometry
  rather than hardcoded screen coordinates.

## Verification

Run the checks relevant to your changes:

```sh
nix develop -c python -m unittest discover -s tests
nix shell nixpkgs#nodejs -c node --test tests/test_extension.js tests/test_promo_motion.mjs
```

For promo changes, also exercise the recorder and inspect the resulting
animation. Mocked tests do not establish real hardware behavior.

## References

- [Development and test coverage](docs/development.md)
- [Daemon and session accounting](docs/architecture.md)
- [Recording and tuning the demo](docs/promo.md)
