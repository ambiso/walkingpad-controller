"""Capture the private monitor with frame-rate headroom, using Mutter and Gst."""
from contextlib import suppress
import subprocess
import threading

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gio, GLib, Gst


class MonitorCapture:
    def __init__(self, bus, call, path, framerate, codec="vp8", output_fps=60,
                 device="/dev/dri/renderD128", resolution="3840x2160"):
        Gst.init(None)
        self.bus, self.call, self.path = bus, call, path
        self.pipeline = None
        self.node = None
        self.first_frame = threading.Event()
        self.encoder = None
        self.frame_times = []
        self.output_fps, self.device = output_fps, device
        self.resolution = resolution.split("@")[0]
        self.session, = call("org.gnome.Mutter.ScreenCast", "/org/gnome/Mutter/ScreenCast",
                             "org.gnome.Mutter.ScreenCast", "CreateSession", GLib.Variant("(a{sv})", ({},)))
        _, monitors, _, _ = call("org.gnome.Mutter.DisplayConfig", "/org/gnome/Mutter/DisplayConfig",
                                "org.gnome.Mutter.DisplayConfig", "GetCurrentState")
        if len(monitors) != 1:
            raise RuntimeError("The private demo requires exactly one monitor")
        connector = monitors[0][0][0]
        stream, = self.session_call("RecordMonitor", GLib.Variant("(sa{sv})", (connector, {
            "cursor-mode": GLib.Variant("u", 0), "is-recording": GLib.Variant("b", True),
        })))
        self.signal = bus.signal_subscribe("org.gnome.Mutter.ScreenCast",
            "org.gnome.Mutter.ScreenCast.Stream", "PipeWireStreamAdded", stream, None,
            Gio.DBusSignalFlags.NONE, self._on_node)
        self.framerate = framerate
        self.codec = codec

    def session_call(self, method, params=None):
        return self.call("org.gnome.Mutter.ScreenCast", self.session,
                         "org.gnome.Mutter.ScreenCast.Session", method, params)

    def _on_node(self, _bus, _sender, _path, _interface, _signal, params):
        self.node, = params.unpack()

    def start(self, wait):
        self.session_call("Start")
        wait(lambda: self.node is not None)
        hardware = self.codec == "av1-vaapi"
        # VA factories are registered only when the driver supports the codec.
        if hardware and Gst.ElementFactory.find("vaav1enc") is None:
            raise RuntimeError("VA-API AV1 capture is unavailable on this GPU/driver")
        if hardware:
            # FFmpeg supports AMD's full speed-setting range; vaav1enc
            # currently restricts target-usage to the 1–7 range.
            self.encoder = subprocess.Popen([
                "ffmpeg", "-v", "warning", "-n", "-vaapi_device", str(self.device),
                "-f", "rawvideo", "-pixel_format", "nv12", "-video_size", self.resolution,
                "-framerate", str(self.output_fps), "-probesize", "32", "-analyzeduration", "0",
                "-i", "pipe:0", "-vf", "hwupload",
                "-c:v", "av1_vaapi", "-rc_mode", "CQP", "-global_quality", "80",
                "-async_depth", "8", "-f", "webm", str(self.path)], stdin=subprocess.PIPE)
        destination = (
            "videoconvert n-threads=4 chroma-mode=none dither=none ! "
            "video/x-raw,format=NV12 ! identity name=frames ! "
            f"videorate ! video/x-raw,framerate={self.output_fps}/1 ! "
            f"fdsink name=output fd={self.encoder.stdin.fileno()} sync=false" if hardware else
            "videoconvert n-threads=8 chroma-mode=none dither=none ! queue ! "
            "vp8enc deadline=1 cpu-used=16 threads=8 target-bitrate=36000000 ! "
            "identity name=frames ! webmmux ! filesink name=output")
        print(f"Capture encoder: {'VA-API AV1' if hardware else 'software VP8'}", flush=True)
        self.pipeline = Gst.parse_launch(
            f"pipewiresrc path={self.node} do-timestamp=true keepalive-time=1000 resend-last=true "
            f"! video/x-raw,max-framerate={self.framerate}/1 ! queue ! "
            + destination)
        output = self.pipeline.get_by_name("output")
        if hardware:
            self.pipeline.get_by_name("frames").connect("handoff",
                lambda _actor, buffer: self.frame_times.append(buffer.pts / Gst.SECOND))
        else:
            output.set_property("location", str(self.path))
        self.pipeline.get_by_name("frames").connect("handoff", lambda *_: self.first_frame.set())
        self.pipeline.set_state(Gst.State.PLAYING)
        wait(self.ready)
        if hardware:
            # Finish driver/encoder startup before the first filmed movement.
            wait(lambda: self.running_time() >= 2)

    def ready(self):
        if self.encoder and self.encoder.poll() is not None:
            raise RuntimeError("Hardware capture encoder exited")
        message = self.pipeline.get_bus().pop_filtered(Gst.MessageType.ERROR)
        if message:
            error, details = message.parse_error()
            raise RuntimeError(f"Capture failed: {error}: {details}")
        return self.first_frame.is_set()

    def running_time(self):
        return (self.pipeline.get_clock().get_time() - self.pipeline.get_base_time()) / Gst.SECOND

    def stop(self):
        self.pipeline.send_event(Gst.Event.new_eos())
        message = self.pipeline.get_bus().timed_pop_filtered(
            30 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
        if message is None or message.type == Gst.MessageType.ERROR:
            raise RuntimeError("Capture failed to flush")
        if self.encoder:
            self.encoder.stdin.close()
            if self.encoder.wait(timeout=15):
                raise RuntimeError("Hardware capture encoder failed")
            self.encoder = None
        self.close()

    def close(self):
        if self.encoder:
            self.encoder.kill()
            self.encoder.wait(timeout=5)
            self.encoder = None
        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None
        if self.session:
            with suppress(GLib.Error):  # The shell may already have closed the session.
                self.session_call("Stop")
            self.session = None
        if self.signal:
            self.bus.signal_unsubscribe(self.signal)
            self.signal = None
