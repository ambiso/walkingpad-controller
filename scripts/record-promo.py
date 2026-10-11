#!/usr/bin/env python3
"""Record the real extension in a private headless GNOME session."""
import argparse
import fcntl
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

from promo.mock_daemon import DemoDaemon

ROOT = Path(__file__).resolve().parent.parent
UUID = "walkingpad@ambiso"


def encode(capture, output, args, frame, clip_start):
    # Begin moving the camera immediately, preserving the menu's aspect ratio.
    progress = f"min(1,max(0,on/{args.fps}/1.8))"
    easing = f"({progress})*({progress})*(3-2*({progress}))"
    if args.output_resolution == "auto":
        output_height = 1080
        alignment = 8 if args.codec == "av1-vaapi" else 2
        output_width = round(output_height * frame["width"] / frame["height"] / alignment) * alignment
    else:
        output_width, output_height = map(int, args.output_resolution.split("x"))
    aspect = output_width / output_height
    initial_width = min(frame["screenWidth"], frame["screenHeight"] * aspect)
    initial_height = initial_width / aspect
    initial = {"x": frame["screenWidth"] - initial_width, "y": 0,
               "width": initial_width, "height": initial_height}
    viewport = {
        key: f"({initial[key]}+({frame[key] - initial[key]})*({easing}))"
        for key in initial
    }
    left = f"W*{viewport['x']}/{frame['screenWidth']}"
    top = f"H*{viewport['y']}/{frame['screenHeight']}"
    width = f"W*{viewport['width']}/{frame['screenWidth']}"
    height = f"H*{viewport['height']}/{frame['screenHeight']}"
    corners = {"x0": left, "y0": top, "x1": f"{left}+{width}", "y1": top,
               "x2": left, "y2": f"{top}+{height}",
               "x3": f"{left}+{width}", "y3": f"{top}+{height}"}
    hardware = args.codec == "av1-vaapi"
    scale = (f"format=nv12,hwupload,scale_vaapi=w={output_width}:h={output_height}" if hardware else
             f"scale={output_width}:{output_height}:flags=lanczos")
    video_filter = f"trim=start={clip_start},setpts=PTS-STARTPTS,fps={args.fps},perspective=" + ":".join(
        f"{key}='{value}'" for key, value in corners.items()
    ) + f":sense=source:eval=frame:interpolation=cubic,{scale},setsar=1"
    encoders = {
        "h264": ["-c:v", "libx264", "-preset", "slow", "-crf", "23"],
        "av1": ["-c:v", "libsvtav1", "-preset", "6", "-crf", "32",
                "-svtav1-params", "lp=4"],
        "av1-vaapi": ["-c:v", "av1_vaapi", "-rc_mode", "CQP", "-global_quality", "80"],
        "vp9": ["-c:v", "libvpx-vp9", "-deadline", "good", "-cpu-used", "3",
                "-row-mt", "1", "-threads", "8", "-crf", "32", "-b:v", "0"],
    }
    container = (["-movflags", "+faststart"] if output.suffix == ".mp4" else
                 ["-loop", "0"] if output.suffix == ".avif" else [])
    device = ["-vaapi_device", str(args.vaapi_device)] if hardware else []
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "warning", "-n",
                    "-filter_threads", "4", *device, "-threads", "4",
                    "-i", str(capture), "-vf", video_filter, *encoders[args.codec],
                    *([] if hardware else ["-pix_fmt", "yuv420p"]),
                    "-r", str(args.fps), "-fps_mode", "cfr",
                    *container, str(output)], check=True)
    stream = json.loads(subprocess.check_output([
        "ffprobe", "-v", "error", "-select_streams",
        "v:1" if output.suffix == ".avif" else "v:0", "-show_entries",
        "stream=width,height,sample_aspect_ratio", "-of", "json", str(output)],
        text=True))["streams"][0]
    if stream != {"width": output_width, "height": output_height,
                  "sample_aspect_ratio": "1:1"}:
        raise RuntimeError(f"Unexpected video geometry: {stream}")


def run(args):
    # Re-exec under a private session bus. Never evaluate code in the host shell.
    if os.environ.get("_WALKINGPAD_DEMO_CHILD") != "1":
        # An earlier render must finish before another capture can start.
        lock_path = Path(tempfile.gettempdir()) / f"walkingpad-promo-{os.getuid()}.lock"
        with lock_path.open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("Another promo capture/render is still running") from None
            return subprocess.call(["dbus-run-session", "--config-file", os.environ["WALKINGPAD_DEMO_DBUS_CONFIG"],
                                    "--", sys.executable, __file__, *sys.argv[1:]],
                                   env=dict(os.environ, _WALKINGPAD_DEMO_CHILD="1"))
    from gi.repository import Gio, GLib
    from promo.capture import MonitorCapture

    output = args.output.resolve()
    if args.keep_source and output.with_suffix(".webm").exists():
        raise RuntimeError(f"Source recording already exists: {output.with_suffix(".webm")}")
    if output.exists():
        raise RuntimeError(f"Output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="walkingpad-promo-") as directory:
        base = Path(directory)
        for kind in ("runtime", "data", "config", "cache"):
            (base / kind).mkdir(mode=0o700)
            os.environ[f"XDG_{kind.upper()}_DIR" if kind == "runtime" else
                       f"XDG_{kind.upper()}_HOME"] = str(base / kind)
        os.environ["GSETTINGS_BACKEND"] = "keyfile"
        os.environ["DBUS_SYSTEM_BUS_ADDRESS"] = os.environ["DBUS_SESSION_BUS_ADDRESS"]
        os.environ.pop("WAYLAND_DISPLAY", None)
        os.environ.pop("DISPLAY", None)
        extension = base / "data/gnome-shell/extensions" / UUID
        shutil.copytree(ROOT / "gnome-extension", extension)
        for entry in [extension, *extension.rglob("*")]:
            entry.chmod(0o755 if entry.is_dir() else 0o644)
        shutil.copy(ROOT / "scripts/promo/cursor.svg", extension)
        shutil.copy(ROOT / "scripts/promo/motion.mjs", extension)
        for asset in Path(os.environ["WALKINGPAD_DEMO_WALLPAPER"]).iterdir():
            shutil.copy(asset, extension / asset.name)
        source = (extension / "extension.js").read_text()
        socket = base / "daemon.sock"
        source = source.replace("'/run/walkingpad/daemon.sock'", json.dumps(str(socket)))
        source = source.replace("this._indicator = new WalkingPadIndicator(this.path);",
                                "this._indicator = new WalkingPadIndicator(this.path);\n"
                                "        installPromo(this._indicator, this.path);")
        source = "import {historyPath} from './motion.mjs';\n" + source
        source = "import Rsvg from 'gi://Rsvg?version=2.0';\n" + source
        source += "\n" + (ROOT / "scripts/promo/choreography.js").read_text()
        (extension / "extension.js").write_text(source)
        subprocess.run(["gsettings", "set", "org.gnome.shell", "enabled-extensions",
                        f"['{UUID}']"], check=True)
        subprocess.run(["gsettings", "set", "org.gnome.shell", "disable-user-extensions",
                        "false"], check=True)
        subprocess.run(["gsettings", "set", "org.gnome.desktop.interface", "color-scheme",
                        "prefer-dark"], check=True)
        # D-Bus activation must inherit the private runtime, including PipeWire.
        subprocess.run(["dbus-update-activation-environment", "--all"], check=True)
        # Only this private PipeWire server grants access on its private socket.
        # This also works when the enclosing development environment is Flatpak.
        pipewire_config = base / "config/pipewire/pipewire.conf.d"
        pipewire_config.mkdir(parents=True)
        (pipewire_config / "demo.conf").write_text(
            'module.access.args = { access.socket = { pipewire-0 = "unrestricted", '
            'pipewire-0-manager = "unrestricted" } }\n')
        for schema, key, value in (
            ("org.gnome.desktop.interface", "scaling-factor", "4" if int(args.resolution.split("x")[0]) >= 3000 else "1"),
            ("org.gnome.desktop.notifications", "show-banners", "false"),
            ("org.gnome.desktop.background", "picture-options", "none"),
            ("org.gnome.desktop.background", "primary-color", "#172235"),
            ("org.gnome.desktop.background", "secondary-color", "#304567"),
            ("org.gnome.desktop.background", "color-shading-type", "vertical"),
        ):
            subprocess.run(["gsettings", "set", schema, key, value], check=True)
        monitor_mode = args.resolution if "@" in args.resolution else f"{args.resolution}@{args.fps * 2}"
        monitor_hz = float(monitor_mode.split("@")[1])
        capture_fps = min(round(args.fps * 1.5), int(monitor_hz))
        processes = []
        recording = None
        demo = DemoDaemon(socket)
        log_path = output.with_suffix(".log")
        try:
            with log_path.open("w") as log:
                for command in (["pipewire"], ["wireplumber"],
                                ["gnome-shell", "--wayland", "--headless", "--no-x11",
                                 "--unsafe-mode", "--virtual-monitor", monitor_mode]):
                    processes.append(subprocess.Popen(command, stdout=log, stderr=log))
                    if command[0] == "pipewire":
                        deadline = time.monotonic() + 5
                        while not (base / "runtime/pipewire-0").exists():
                            if time.monotonic() > deadline or processes[-1].poll() is not None:
                                raise RuntimeError(f"PipeWire failed to start; see {log_path}")
                            time.sleep(0.05)
                bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)

                def call(name, path, interface, method, params=None, timeout=10000):
                    return bus.call_sync(name, path, interface, method, params,
                                         None, Gio.DBusCallFlags.NONE, timeout, None).unpack()

                def evaluate(expression):
                    ok, result = call("org.gnome.Shell", "/org/gnome/Shell",
                                      "org.gnome.Shell", "Eval",
                                      GLib.Variant("(s)", (expression,)))
                    if not ok:
                        raise RuntimeError(f"Shell evaluation failed: {result}")
                    return json.loads(result) if result else None

                def wait(predicate, timeout=30):
                    deadline = time.monotonic() + timeout
                    last_error = None
                    while time.monotonic() < deadline:
                        while GLib.MainContext.default().pending():
                            GLib.MainContext.default().iteration(False)
                        if processes[-1].poll() is not None:
                            raise RuntimeError(f"GNOME Shell exited; see {log_path}")
                        try:
                            if predicate():
                                return
                        except GLib.Error as error:
                            last_error = error
                        time.sleep(0.1)
                    status = evaluate("typeof walkingpadPromo === 'undefined' ? null : walkingpadPromo.status()")
                    if status:
                        status.pop("motionIntervals", None)
                    raise TimeoutError(f"Demo readiness timed out: {last_error}; status={status}; see {log_path}")

                wait(lambda: evaluate("typeof walkingpadPromo !== 'undefined' && walkingpadPromo.status().connected"))
                # Leave overview through the shell's normal transition before filming.
                evaluate("(walkingpadPromo.prepare(), true)")
                time.sleep(3)
                # Populate font/shader caches before capturing the visible tour.
                evaluate("(walkingpadPromo.setMenuOpen(true), true)")
                time.sleep(0.5)
                evaluate("(walkingpadPromo.historyTour(), true)")
                wait(lambda: evaluate("!walkingpadPromo.status().busy"), 5)
                evaluate("(walkingpadPromo.setMenuOpen(false), true)")
                time.sleep(0.5)
                wait(lambda: evaluate("!walkingpadPromo.status().bannerVisible"), 5)
                capture = base / "capture.webm"
                recording = MonitorCapture(bus, call, capture, capture_fps, args.capture_codec,
                                           args.fps, args.vaapi_device, args.resolution)
                recording.start(wait)
                clip_start = recording.running_time()

                def action(method, *arguments):
                    print(f"Demo action: {method} {arguments}", file=log, flush=True)
                    evaluate(f"(walkingpadPromo.{method}({','.join(json.dumps(a) for a in arguments)}), true)")
                    def finished():
                        status = evaluate("walkingpadPromo.status()")
                        if status["error"]:
                            raise RuntimeError(status["error"])
                        return not status["busy"]
                    wait(finished, 5)

                action("move", "menu")
                action("click")
                time.sleep(0.8)
                frame = evaluate("walkingpadPromo.frame()")
                sweep_start = recording.running_time()
                action("historyTour")
                sweep_end = recording.running_time()
                motion_intervals = evaluate("walkingpadPromo.status().motionIntervals")
                wait(lambda: evaluate("walkingpadPromo.status().visitedDays.length === 7"), 2)
                action("click")
                wait(lambda: evaluate("walkingpadPromo.status().countdown > 0"), 2)
                action("move", "slider", None, 400)
                for countdown in (2, 1):
                    wait(lambda: evaluate(f"walkingpadPromo.status().countdown === {countdown}"), 3)
                wait(lambda: evaluate("walkingpadPromo.status().running"), 8)
                time.sleep(0.8)
                action("drag", 0.65)
                wait(lambda: evaluate("walkingpadPromo.status().speed > 3"), 5)
                time.sleep(0.9)
                action("drag", 0.35)
                wait(lambda: evaluate("walkingpadPromo.status().speed < 3"), 5)
                time.sleep(0.9)
                demo.grow_session()
                action("move", "chart", 6)
                wait(lambda: evaluate(f"walkingpadPromo.status().todaySteps >= {demo.target_today_steps} && "
                                      "walkingpadPromo.status().elapsed > 3600 && "
                                      "walkingpadPromo.status().todaySteps === 2300 + walkingpadPromo.status().steps"), 30)
                time.sleep(0.8)
                action("move", "toggle")
                action("click")
                wait(lambda: evaluate("!walkingpadPromo.status().running"), 5)
                wait(lambda: evaluate("walkingpadPromo.status().historyRevision > 0 && walkingpadPromo.status().todaySteps > 2300"), 5)
                time.sleep(1)
                action("move", "chart", 6, 550, False)
                time.sleep(0.9)
                recording.stop()
                if args.keep_source:
                    shutil.copyfile(capture, output.with_suffix(".webm"))
                timestamps = subprocess.check_output([
                    "ffprobe", "-v", "error", "-select_streams", "v:0",
                    "-show_entries", "packet=pts_time", "-of", "csv=p=0", str(capture)], text=True)
                frames = recording.frame_times or [float(value) for value in timestamps.splitlines() if value.strip()]
                gaps = [(b - a) * 1000 for a, b in zip(frames, frames[1:])
                        if sweep_start <= a < b <= sweep_end]
                sweep_gaps = [(b - a) * 1000 for a, b in zip(frames, frames[1:])
                              if sweep_start + 0.65 <= a < b <= sweep_start + 2.45]
                print("Motion diagnostics: " + json.dumps({
                    "sweep_start": sweep_start, "sweep_end": sweep_end,
                    "animation_ms": motion_intervals, "capture_ms": gaps,
                    "sweep_ms": sweep_gaps,
                    "frame": frame, "clip_start": clip_start}), file=log, flush=True)
                encode(capture, output, args, frame, clip_start)
                print(f"Recorded {output}")
        finally:
            if recording:
                recording.close()
            demo.close()
            for process in reversed(processes):
                process.terminate()
            for process in reversed(processes):
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, nargs="?", default=Path("walkingpad-promo.avif"))
    parser.add_argument("--resolution", default="3840x2160", help="virtual monitor WxH[@Hz] (default: 3840x2160 at twice the output fps)")
    parser.add_argument("--output-resolution", default="auto", help="encoded WxH, or auto for balanced menu crop (default)")
    parser.add_argument("--codec", choices=("av1", "av1-vaapi", "h264", "vp9"),
                        help="encoder (default: AV1 for AVIF/MP4, VP9 for WebM)")
    parser.add_argument("--fps", type=int, default=60)
    parser.add_argument("--vaapi-device", type=Path, default=Path("/dev/dri/renderD128"),
                        help="render node for VA-API output encoding")
    parser.add_argument("--capture-codec", choices=("vp8", "av1-vaapi"), default="vp8",
                        help="intermediate capture encoder (default: VP8 for smooth pacing)")
    parser.add_argument("--keep-source", action="store_true", help="also keep the original WebM recording")
    args = parser.parse_args()
    if args.output.suffix not in (".avif", ".mp4", ".webm"):
        parser.error("output must have an .avif, .mp4 or .webm extension")
    args.codec = args.codec or ("vp9" if args.output.suffix == ".webm" else "av1")
    if args.output.suffix == ".avif" and args.codec not in ("av1", "av1-vaapi"):
        parser.error("AVIF requires an AV1 encoder")
    if args.output.suffix == ".webm" and args.codec == "h264":
        parser.error("H.264 requires an .mp4 output")
    if args.keep_source and args.output.suffix == ".webm":
        parser.error("--keep-source requires an .avif or .mp4 output to avoid conflicting WebM paths")
    match = re.fullmatch(r"(\d+)x(\d+)(?:@\d+(?:\.\d+)?)?", args.resolution)
    if not match or any(int(n) < 480 or int(n) % 2 for n in match.groups()):
        parser.error("resolution needs even dimensions of at least 480 pixels, e.g. 1280x720")
    if args.output_resolution != "auto" and (not re.fullmatch(r"\d+x\d+", args.output_resolution) or any(
            int(n) < 480 or int(n) % 2 for n in args.output_resolution.split("x"))):
        parser.error("output resolution needs even dimensions of at least 480 pixels")
    if not 1 <= args.fps <= 120:
        parser.error("fps must be between 1 and 120")
    try:
        return run(args)
    except Exception as error:
        print(f"Promo recording failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
