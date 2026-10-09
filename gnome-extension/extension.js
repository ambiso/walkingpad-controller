// extension.js — WalkingPad Quick Settings indicator.
//
// Talks straight to walkingpad-daemon's JSON-lines control socket
// (/run/walkingpad/daemon.sock): one persistent "watch" connection streams
// state updates, one short-lived connection per command (start/speed/stop)
// keeps the daemon's request/reply flow intact. All UI lives in GNOME's
// Quick Settings (top-right panel dropdown): a panel status icon, a
// start/stop toggle, and a speed slider.
//
// Kept single-file and GSettings-free on purpose: the extension has no
// configuration of its own; the socket path is fixed by the NixOS module
// and baked into walkingpad-client as well.

import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import Clutter from 'gi://Clutter';
import St from 'gi://St';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as QuickSettings from 'resource:///org/gnome/shell/ui/quickSettings.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const SOCKET_PATH = '/run/walkingpad/daemon.sock';
const RECONNECT_SECONDS = 3;
// The pad enforces ~400 ms pacing between BLE writes; committing on a
// short delay also coalesces a drag into one command.
const SPEED_COMMIT_MS = 500;

// Session history written by the daemon (one JSON line per finalized
// session) plus the live snapshot of the open session. Both are 0644 in a
// 0755 StateDirectory, so the user-session shell can read them directly.
const HISTORY_PATH = '/var/lib/walkingpad/history.jsonl';
const CURRENT_PATH = '/var/lib/walkingpad/current.json';
const HISTORY_DAYS = 7;
const HISTORY_REFRESH_SECONDS = 300;

// GJS binds Cairo's color setters under either casing depending on the
// gir version; take whichever exists.
function cairoSetColor(cr, r, g, b, a) {
    if (typeof cr.setSourceRGBA === 'function')
        cr.setSourceRGBA(r, g, b, a);
    else
        cr.setSourceRgba(r, g, b, a);
}

// Local calendar date as YYYY-MM-DD — the daemon dates records in local
// time, and Date.toISOString() would shift everything to UTC.
function isoLocalDate(d) {
    const pad = n => String(n).padStart(2, '0');
    return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
}

function readDayTotals() {
    const days = [];
    const byDate = new Map();
    const now = new Date();
    for (let i = HISTORY_DAYS - 1; i >= 0; i--) {
        const d = new Date(now.getFullYear(), now.getMonth(), now.getDate() - i);
        const day = {
            date: isoLocalDate(d),
            steps: 0,
            meters: 0,
            weekday: d.toLocaleDateString(undefined, {weekday: 'narrow'}),
            today: i === 0,
        };
        days.push(day);
        byDate.set(day.date, day);
    }
    // Finalized sessions; "pause" records overlap their session's stop
    // record and would double-count, so they are skipped.
    try {
        const [, text] = GLib.file_get_contents(HISTORY_PATH);
        for (const line of decoder.decode(text).split('\n')) {
            if (!line.trim())
                continue;
            try {
                const rec = JSON.parse(line);
                if (rec.reason !== 'stop')
                    continue;
                const day = byDate.get(rec.date);
                if (day) {
                    day.steps += rec.steps || 0;
                    day.meters += rec.distance || 0;
                }
            } catch (e) {
                // skip malformed line
            }
        }
    } catch (e) {
        // no history yet
    }
    // The open session counts toward today (it is real activity even
    // though the pad has not finalized it).
    try {
        const [, text] = GLib.file_get_contents(CURRENT_PATH);
        const cur = JSON.parse(decoder.decode(text));
        const day = byDate.get(cur.date);
        const sess = cur.session || {};
        if (day) {
            day.steps += sess.steps || 0;
            day.meters += sess.distance || 0;
        }
    } catch (e) {
        // no open session
    }
    return days;
}

const decoder = new TextDecoder();

// Daemon link: watch stream + one-shot commands. Not a GObject; it only
// needs the two callbacks the indicator provides.
class DaemonLink {
    constructor(onState, onDown) {
        this._onState = onState;
        this._onDown = onDown;
        this._watchConn = null;
        this._watchIn = null;
        this._stopped = false;
    }

    start() {
        this._stopped = false;
        this._openWatch();
    }

    stop() {
        this._stopped = true;
        this._closeWatch();
    }

    _closeWatch() {
        if (this._watchConn) {
            try {
                this._watchConn.close(null);
            } catch (e) {
                // already dead
            }
            this._watchConn = null;
            this._watchIn = null;
        }
    }

    _openWatch() {
        if (this._stopped)
            return;
        const client = new Gio.SocketClient();
        client.connect_async(Gio.UnixSocketAddress.new(SOCKET_PATH), null, (c, res) => {
            let conn = null;
            try {
                conn = c.connect_finish(res);
            } catch (e) {
                this._onDown();
                return;
            }
            if (this._stopped) {
                conn.close(null);
                return;
            }
            this._watchConn = conn;
            this._watchIn = new Gio.DataInputStream({
                base_stream: conn.get_input_stream(),
            });
            this._sendWatchRequest(conn);
            this._readWatchLine();
        });
    }

    _sendWatchRequest(conn) {
        // The watch reply carries the current state and the daemon then
        // streams every subsequent change on this connection.
        try {
            conn.get_output_stream().write_all(
                JSON.stringify({cmd: 'watch'}) + '\n', null);
        } catch (e) {
            this._closeWatch();
            this._onDown();
        }
    }

    _readWatchLine() {
        if (!this._watchIn || this._stopped)
            return;
        this._watchIn.read_line_utf8_async(GLib.PRIORITY_DEFAULT, null, (in_, res) => {
            let line = null;
            try {
                [line] = in_.read_line_utf8_finish(res);
            } catch (e) {
                this._closeWatch();
                this._onDown();
                return;
            }
            if (line === null) {
                this._closeWatch();
                this._onDown();
                return;
            }
            try {
                const msg = JSON.parse(line);
                if (msg && msg.ok && msg.state)
                    this._onState(msg.state);
            } catch (e) {
                // malformed line; the stream stays usable
            }
            this._readWatchLine();
        });
    }

    command(request) {
        // The watch connection never reads again, so commands travel on
        // their own short-lived connection (the daemon serves each client
        // in a loop of readline/dispatch).
        const client = new Gio.SocketClient();
        client.connect_async(Gio.UnixSocketAddress.new(SOCKET_PATH), null, (c, res) => {
            let conn = null;
            try {
                conn = c.connect_finish(res);
            } catch (e) {
                this._onDown();
                return;
            }
            const out = conn.get_output_stream();
            try {
                out.write_all(JSON.stringify(request) + '\n', null);
            } catch (e) {
                conn.close(null);
                return;
            }
            const din = new Gio.DataInputStream({
                base_stream: conn.get_input_stream(),
            });
            din.read_line_utf8_async(GLib.PRIORITY_DEFAULT, null, (in_, res) => {
                try {
                    const [line] = in_.read_line_utf8_finish(res);
                    if (line !== null) {
                        const msg = JSON.parse(line);
                        if (msg && msg.state)
                            this._onState(msg.state);
                    }
                } catch (e) {
                    // Reply lost; the watch stream resyncs the UI anyway.
                } finally {
                    try {
                        conn.close(null);
                    } catch (e) {
                        // already dead
                    }
                }
            });
        });
    }
}

const WalkingPadIndicator = GObject.registerClass(
class WalkingPadIndicator extends QuickSettings.SystemIndicator {
    _init(extensionDir) {
        super._init();

        this._state = null;
        this._speed = null;
        this._commitTimeout = 0;
        this._reconnectTimeout = 0;
        this._syncing = false;
        this._destroyed = false;

        const gicon = Gio.icon_new_for_string(
            extensionDir + '/icons/walkingpad-symbolic.svg');

        this._icon = new St.Icon({
            gicon,
            style_class: 'system-status-icon',
        });
        this.add_child(this._icon);

        this._toggle = new QuickSettings.QuickSettingsToggle({
            title: 'WalkingPad',
            subtitle: 'connecting…',
        });
        this._toggle.gicon = gicon;
        this._toggle.connect('clicked', () => this._onToggleClicked());
        this.quickSettingsItems.push(this._toggle);

        this._adjustment = new St.Adjustment({
            lower: 1.0,
            upper: 6.0,
            step_increment: 0.1,
            value: 3.0,
        });
        this._adjustment.connect('notify::value', () => this._onSliderValue());
        this._slider = new QuickSettings.QuickSettingsSlider(this._adjustment);
        this.quickSettingsItems.push(this._slider);

        this._days = [];
        this._hoveredDay = -1;
        this._historyTimeout = 0;
        this._historyBox = this._buildHistogram();
        this.quickSettingsItems.push(this._historyBox);

        Main.panel.statusArea.quickSettings.addExternalIndicator(this);

        this._link = new DaemonLink(
            state => this._onState(state),
            () => this._onWatchDown());
        this._link.start();
        this._refreshHistory();
    }

    _onToggleClicked() {
        const running = this._running();
        const target = !running;
        this._toggle.checked = target;
        if (target)
            this._link.command({cmd: 'start', speed: this._targetSpeed()});
        else
            this._link.command({cmd: 'stop'});
    }

    _targetSpeed() {
        // Slider values arrive as float noise; the pad takes 0.1 steps.
        return Math.round(this._adjustment.value * 10) / 10;
    }

    _onSliderValue() {
        if (this._syncing)
            return;
        this._speed = this._targetSpeed();
        if (this._commitTimeout)
            GLib.source_remove(this._commitTimeout);
        this._commitTimeout = GLib.timeout_add(GLib.PRIORITY_DEFAULT, SPEED_COMMIT_MS, () => {
            this._commitTimeout = 0;
            // "speed" starts the belt if it is stopped, matching the CLI.
            this._link.command({cmd: 'speed', speed: this._speed});
            return GLib.SOURCE_REMOVE;
        });
    }

    _running() {
        return this._state !== null &&
            this._state.connected &&
            this._state.belt === 'started';
    }

    // -- steps histogram --------------------------------------------------
    //
    // Seven bars (oldest left, today right) drawn with Cairo on an
    // St.DrawingArea, plus a caption line and a weekday label row. Shell
    // actors have no native tooltips, so hover precision is a hit-test on
    // motion events: the hovered bar is highlighted and the caption shows
    // that day's exact steps and distance.

    _buildHistogram() {
        const CHART_W = 200;
        const CHART_H = 56;
        const GAP = 5;

        this._chart = new St.DrawingArea({
            width: CHART_W,
            height: CHART_H,
        });
        this._chart._barLayout = w => {
            // Shared by the painter and the hover hit-test so they can
            // never disagree about where a bar starts.
            const barW = (w - GAP * (HISTORY_DAYS + 1)) / HISTORY_DAYS;
            return i => [GAP + i * (barW + GAP), barW];
        };
        this._chart.connect('repaint', area => this._paintChart(area));

        this._chart.connect('motion-event', (actor, event) => {
            const [sx, sy] = event.get_coords();
            const [ok, ax, ay] = actor.transform_stage_point(sx, sy);
            if (!ok)
                return false;
            const [x0, barW] = actor._barLayout(actor.get_width())(0);
            const idx = Math.floor((ax - x0) / (barW + GAP));
            const hovered = idx >= 0 && idx < HISTORY_DAYS ? idx : -1;
            if (hovered !== this._hoveredDay) {
                this._hoveredDay = hovered;
                this._updateHistoryCaption();
                actor.queue_repaint();
            }
            return true;
        });
        this._chart.connect('leave-event', () => {
            if (this._hoveredDay !== -1) {
                this._hoveredDay = -1;
                this._updateHistoryCaption();
                this._chart.queue_repaint();
            }
            return true;
        });

        this._caption = new St.Label({
            text: 'no history yet',
            x_align: Clutter.ActorAlign.CENTER,
            x_expand: true,
            style: 'font-size: 10pt; padding: 2px 0;',
        });

        const weekdayRow = new St.BoxLayout({x_expand: true});
        this._weekdayLabels = this._days.map(() => null); // resized on refresh
        for (let i = 0; i < HISTORY_DAYS; i++) {
            const label = new St.Label({
                text: '',
                x_align: Clutter.ActorAlign.CENTER,
                x_expand: true,
                style: 'font-size: 9pt; padding: 1px 0; color: rgba(200, 200, 200, 0.75);',
            });
            this._weekdayLabels[i] = label;
            weekdayRow.add_child(label);
        }

        const box = new St.BoxLayout({
            vertical: true,
            style: 'padding: 10px 12px 8px 12px;',
        });
        box.add_child(this._chart);
        box.add_child(this._caption);
        box.add_child(weekdayRow);
        return box;
    }

    _paintChart(area) {
        const cr = area.get_context();
        const w = area.get_width();
        const h = area.get_height();
        const barAt = area._barLayout(w);
        const maxSteps = Math.max(1, ...this._days.map(d => d.steps));
        for (let i = 0; i < HISTORY_DAYS; i++) {
            const day = this._days[i];
            if (!day)
                continue;
            const [x, barW] = barAt(i);
            const barH = day.steps > 0
                ? Math.max(3, Math.round((h - 2) * day.steps / maxSteps))
                : 2;  // zero-day stub keeps the week visually aligned
            if (i === this._hoveredDay) {
                cairoSetColor(cr, 0.58, 0.78, 1.0, 1.0);
            } else if (day.today) {
                cairoSetColor(cr, 0.95, 0.95, 0.95, 0.95);
            } else {
                cairoSetColor(cr, 0.75, 0.75, 0.75, 0.65);
            }
            cr.rectangle(x, h - barH, barW, barH);
            cr.fill();
        }
    }

    _refreshHistory() {
        if (this._destroyed)
            return;
        try {
            this._days = readDayTotals();
        } catch (e) {
            this._days = [];
        }
        for (let i = 0; i < HISTORY_DAYS; i++) {
            const day = this._days[i];
            if (day && this._weekdayLabels && this._weekdayLabels[i]) {
                this._weekdayLabels[i].text = day.weekday;
                this._weekdayLabels[i].style =
                    'font-size: 9pt; padding: 1px 0; ' +
                    (day.today ? 'font-weight: bold;' : 'color: rgba(200, 200, 200, 0.75);');
            }
        }
        this._hoveredDay = -1;
        this._updateHistoryCaption();
        if (this._chart)
            this._chart.queue_repaint();
        if (this._historyTimeout)
            GLib.source_remove(this._historyTimeout);
        this._historyTimeout = GLib.timeout_add_seconds(
            GLib.PRIORITY_DEFAULT, HISTORY_REFRESH_SECONDS, () => {
                this._historyTimeout = 0;
                this._refreshHistory();
                return GLib.SOURCE_REMOVE;
            });
    }

    _updateHistoryCaption() {
        const hovered = this._hoveredDay >= 0 ? this._days[this._hoveredDay] : null;
        if (hovered) {
            const km = (hovered.meters / 1000).toFixed(2);
            this._caption.text =
                `${hovered.date}: ${hovered.steps.toLocaleString()} steps · ${km} km`;
        } else {
            const total = this._days.reduce((sum, d) => sum + (d.steps || 0), 0);
            this._caption.text = total > 0
                ? `steps · last ${HISTORY_DAYS} days: ${total.toLocaleString()}`
                : 'no history yet';
        }
    }

    _onState(state) {
        this._state = state;
        const running = this._running();
        // A running->stopped transition finalized a session: the history
        // gained a record, so the histogram is stale.
        if (!running && this._wasRunning)
            this._refreshHistory();
        this._wasRunning = running;

        // Bounds arrive once the daemon has read them off the pad.
        if (state.min_speed !== null)
            this._setBound('lower', state.min_speed);
        if (state.max_speed !== null)
            this._setBound('upper', state.max_speed);

        // Reflect the pad in the slider unless the user is dragging it.
        if (running && Math.abs(this._adjustment.value - state.speed) > 0.05) {
            this._syncing = true;
            this._adjustment.value = state.speed;
            this._syncing = false;
        }

        this._toggle.checked = running;
        this._toggle.subtitle = this._subtitle(state, running);
        this._icon.opacity = running ? 255 : (state.connected ? 160 : 80);
    }

    _setBound(prop, value) {
        if (Math.abs(this._adjustment[prop] - value) > 1e-9)
            this._adjustment[prop] = value;
    }

    _subtitle(state, running) {
        if (!state.connected)
            return 'pad not connected';
        const time = this._formatDuration(state.elapsed);
        const steps = `${state.steps} steps`;
        const km = `${(state.distance / 1000).toFixed(2)} km`;
        if (!running)
            return `stopped · ${time} · ${steps} · ${km}`;
        const speed = `${state.speed.toFixed(1)} km/h`;
        return `${speed} · ${time} · ${steps} · ${km}`;
    }

    // Session seconds as h:mm:ss, or m:ss under an hour. Matches how the
    // daemon reports elapsed time: seconds of the pad's open session.
    _formatDuration(totalSeconds) {
        const s = Math.floor(totalSeconds || 0);
        const h = Math.floor(s / 3600);
        const m = Math.floor((s % 3600) / 60);
        const ss = String(s % 60).padStart(2, '0');
        if (h > 0)
            return `${h}:${String(m).padStart(2, '0')}:${ss}`;
        return `${m}:${ss}`;
    }

    _onWatchDown() {
        if (this._destroyed)
            return;
        this._state = null;
        this._toggle.checked = false;
        this._toggle.subtitle = 'daemon offline';
        this._icon.opacity = 40;
        if (this._reconnectTimeout)
            GLib.source_remove(this._reconnectTimeout);
        this._reconnectTimeout = GLib.timeout_add_seconds(
            GLib.PRIORITY_DEFAULT, RECONNECT_SECONDS, () => {
                this._reconnectTimeout = 0;
                this._link.start();
                return GLib.SOURCE_REMOVE;
            });
    }

    destroy() {
        this._destroyed = true;
        this._link.stop();
        if (this._commitTimeout)
            GLib.source_remove(this._commitTimeout);
        if (this._reconnectTimeout)
            GLib.source_remove(this._reconnectTimeout);
        if (this._historyTimeout)
            GLib.source_remove(this._historyTimeout);
        this._toggle.destroy();
        this._slider.destroy();
        this._historyBox.destroy();
        super.destroy();
    }
});

export default class WalkingPadExtension extends Extension {
    enable() {
        this._indicator = new WalkingPadIndicator(this.path);
    }

    disable() {
        this._indicator.destroy();
        this._indicator = null;
    }
}
