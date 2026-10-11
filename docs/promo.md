# Recording the demo

[Back to the README](../README.md)


On Linux, record the actual extension in a private headless GNOME Shell:

```sh
nix run .#walkingpad-promo -- /tmp/walkingpad-promo.avif
# Optional: another monitor size and the original WebM alongside the AVIF
nix run .#walkingpad-promo -- /tmp/walkingpad-promo-small.avif --resolution 960x600 --keep-source
```

The default uses a private 4K desktop with 4× UI scaling and produces a
looping 60 fps animated AVIF, 1080 pixels high. Its width follows the menu, with balanced
padding on opposite edges. The cursor and camera move immediately. A
diagnostic `.log` is saved next to the video.
Existing videos are never overwritten. Capture and encoding can take
several minutes. The mock daemon
provides seven days of sample history, starting with real-time telemetry
once a second before a three-second ease-in/out brings today’s steps to
six times the largest sample day, with 60 Hz updates during the rise.
Session duration and distance rise
with the steps.
The script opens the top-right menu, zooms into the controls, sweeps all
seven history bars in a straight line, shows the full 3–2–1 countdown,
picks up the slider at its current handle and drags it twice, and shows the histogram growing live before
stopping to display the updated daily total.
It checks the resulting UI state as it goes and fails if an action stops
working.

Controls are located through the extension's actors and their current
geometry, rather than screen coordinates. A large software cursor follows
cubic Bézier paths with distance-based, minimum-jerk timing between
controls, grows slightly during movement, and
pulses briefly on clicks. The history tour uses a joined quintic Hermite spline: it flows into
a straight sweep and out to the toggle without stopping at the joins,
with a constant cursor size; slider drags also stay straight. Startup
notifications are suppressed in the demo shell. The backdrop is the default
dark wallpaper from the pinned GNOME release; the black cursor is an SVG.
The scripting helper is injected into a temporary extension copy; no demo
hooks are added to the installed extension. GNOME Shell, D-Bus, PipeWire,
settings, and the mock socket use a private temporary session.
The desktop runs at twice the output frame rate (120 Hz by default), and
Mutter’s monitor stream is captured with rate-limit headroom. This avoids
the dropped frames observed through GNOME’s 60 fps area-capture path.
Capture defaults to software VP8; `--capture-codec av1-vaapi` enables
experimental hardware AV1 on supported GPUs. It feeds CPU-converted frames
directly from GStreamer to FFmpeg's VA-API encoder, which can use AMD's faster
encoding presets. Both were tested on an RX 7900 XTX: hardware encoding
reduces CPU load, but VP8 delivered more consistent frame pacing at 4K,
so it remains the default. GPU color conversion and DMA-BUF capture were
also tested; they introduced stalls and cursor artifacts in this setup.
The cropped asset is encoded separately for a small file.
The log records animation and capture frame intervals during the history tour. No WalkingPad
or running desktop session is needed. The Nix package supplies the tools;
it has been exercised with GNOME Shell 50.

The driver is `scripts/record-promo.py`; cursor choreography and mock
telemetry live in `scripts/promo/`. Use `--fps` to change the output frame
rate, `--output-resolution` for encoded dimensions, and `--codec h264`
with an `.mp4` output for H.264 instead of the default AV1. AVIF displays
inline in the GitHub README as an animated image. A `.webm` output selects VP9 by
default. Edit the driver's action sequence to change the tour.
`--codec av1-vaapi` offloads final scaling and encoding to the GPU;
the animated camera transform still runs on the CPU, limited to four threads.
Rendering starts after capture finishes, and concurrent promo runs are rejected.
Software AV1 remains the default for the final asset. Earlier comparisons
on the same footage produced smaller files than the configured VA-API
encoder; those sizes are not measurements of the current demo.


## Tuning the animation

- `scripts/record-promo.py`: action order, pauses, and camera framing. The camera
  uses smoothstep easing over 1.8 seconds.
- `scripts/promo/mock_daemon.py`: sample history, the step target, and the
  three-second rise. Its easing is `t * t * (3 - 2 * t)`; steps, duration,
  and distance follow the same progress.
- `scripts/promo/motion.mjs`: the history sweep's approach, sweep, and departure
  durations (650, 1,800, and 550 ms).
- `scripts/promo/choreography.js`: actor targeting, cursor movement, and the
  120 ms click pulse.

To replace the README animation, record to a new path first, inspect the result,
then copy it to `docs/promo.avif`. The script refuses to overwrite existing files.
