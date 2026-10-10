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

// Session history served by the daemon over the control socket
// ({"cmd": "history"}): the state directory belongs to the service's
// dynamic user and is not readable by the user session, so the files
// are never read directly.
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

function aggregateHistory(reply) {
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
    // Finalized sessions; non-"stop" reasons overlap their session and
    // would double-count, so they are skipped.
    for (const rec of (reply && reply.history) || []) {
        if (rec.reason !== 'stop')
            continue;
        const day = byDate.get(rec.date);
        if (day) {
            day.steps += rec.steps || 0;
            day.meters += rec.distance || 0;
        }
    }
    // The open session counts toward today (it is real activity even
    // though the pad has not finalized it).
    const cur = reply && reply.current;
    if (cur) {
        const sess = cur.session || {};
        const day = byDate.get(cur.date);
        if (day) {
            day.steps += sess.steps || 0;
            day.meters += sess.distance || 0;
        }
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
        // GJS removed the read_line_utf8_* overrides; read_line_async
        // yields bytes, decoded here.
        this._watchIn.read_line_async(GLib.PRIORITY_DEFAULT, null, (in_, res) => {
            let line = null;
            try {
                [line] = in_.read_line_finish(res);
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
                const msg = JSON.parse(decoder.decode(line));
                if (msg && msg.ok && msg.state)
                    this._onState(msg.state);
            } catch (e) {
                // malformed line; the stream stays usable
            }
            this._readWatchLine();
        });
    }

    command(request, onReply) {
        // The watch connection never reads again, so commands travel on
        // their own short-lived connection (the daemon serves each client
        // in a loop of readline/dispatch). onReply, when given, receives
        // the full reply object.
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
            din.read_line_async(GLib.PRIORITY_DEFAULT, null, (in_, res) => {
                try {
                    const [line] = in_.read_line_finish(res);
                    if (line !== null) {
                        const msg = JSON.parse(decoder.decode(line));
                        if (msg && msg.state)
                            this._onState(msg.state);
                        if (onReply)
                            onReply(msg);
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
        this._commitTimeout = 0;
        this._reconnectTimeout = 0;
        this._syncing = false;
        this._destroyed = false;
        this._pendingSpeed = null;   // slider value awaiting pad confirmation
        this._pendingSince = 0;
        this._countdown = 0;
        this._countdownTimeout = 0;

        const gicon = Gio.icon_new_for_string(
            extensionDir + '/icons/walkingpad-symbolic.svg');

        this._icon = new St.Icon({
            gicon,
            style_class: 'system-status-icon',
        });
        this.add_child(this._icon);

        this._toggle = new QuickSettings.QuickToggle({
            title: 'WalkingPad',
            subtitle: 'connecting…',
        });
        this._toggle.gicon = gicon;
        this._toggle.connect('clicked', () => this._onToggleClicked());

        // GNOME 50's QuickSlider wraps a normalized (0..1) Slider exposed
        // as .slider; speed is mapped into that range from the pad's
        // reported bounds. Commits happen on drag-end (plus a debounce
        // for scroll/keyboard changes).
        this._lo = 1.0;
        this._hi = 6.0;
        this._dragging = false;
        this._sliderItem = new QuickSettings.QuickSlider();
        // GNOME 50's QuickSlider has no visible text slot (icon-label is
        // the accessible name), so the slider gets a themed icon like the
        // volume slider — media-seek-forward-symbolic, the tape-deck
        // ">>" — and the speed lives in the histogram caption.
        this._sliderItem.iconName = 'media-seek-forward-symbolic';
        this._slider = this._sliderItem.slider;
        this._slider.connect('drag-begin', () => {
            this._dragging = true;
            this._cancelSpeedCommit();
        });
        this._slider.connect('drag-end', () => {
            this._dragging = false;
            this._commitSpeed();
        });
        this._slider.connect('notify::value', () => this._onSliderValue());
        // The slider comes first in the items list: the grid renders the
        // second item in the left column, so this puts the WalkingPad
        // toggle on the left and the speed slider on the right.
        this.quickSettingsItems.push(this._sliderItem);
        this.quickSettingsItems.push(this._toggle);

        this._days = [];
        this._historyReply = null;
        this._historyRevision = null;
        this._historyRequest = 0;
        this._sessionDate = null;
        this._hoveredDay = -1;
        this._historyTimeout = 0;
        this._historyBox = this._buildHistogram();

        Main.panel.statusArea.quickSettings.addExternalIndicator(this);
        // The histogram spans the full 2-column quick settings grid; as a
        // one-column item its fixed width would inflate every column (the
        // grid sizes columns to the widest child), skewing the layout.
        Main.panel.statusArea.quickSettings.menu.addItem(this._historyBox, 2);

        this._link = new DaemonLink(
            state => this._onState(state),
            () => this._onWatchDown());
        this._link.start();
        this._refreshHistory();
    }

    _onToggleClicked() {
        const target = !(this._running() || this._state?.starting || this._countdown > 0);
        this._toggle.checked = target;
        if (target) {
            this._startCountdown();
            this._link.command({cmd: 'start', speed: this._targetSpeed()});
        } else {
            this._clearCountdown();
            this._link.command({cmd: 'stop'});
        }
    }

    // The pad counts down ~3 s after an accepted start before the belt
    // moves. Waiting for the daemon's state to catch up would leave the
    // toggle feeling dead for that window, so the countdown runs locally
    // from the click; the daemon's "starting" flag and the running state
    // take over seamlessly when they arrive.
    _startCountdown() {
        this._clearCountdown();
        this._countdown = 3;
        this._countdownTick();
        this._countdownTimeout = GLib.timeout_add_seconds(
            GLib.PRIORITY_DEFAULT, 1, () => {
                this._countdown -= 1;
                if (this._countdown <= 0) {
                    this._countdownTimeout = 0;
                    this._countdown = 0;
                    if (this._state)
                        this._toggle.subtitle =
                            this._subtitle(this._state, this._running());
                    return GLib.SOURCE_REMOVE;
                }
                this._countdownTick();
                return GLib.SOURCE_CONTINUE;
            });
    }

    _countdownTick() {
        this._toggle.subtitle = `starting in ${this._countdown}…`;
    }

    _clearCountdown() {
        if (this._countdownTimeout) {
            GLib.source_remove(this._countdownTimeout);
            this._countdownTimeout = 0;
        }
        this._countdown = 0;
    }

    _targetSpeed() {
        // Map the normalized slider position into the pad's speed range;
        // the pad takes 0.1 steps, so float noise is rounded away.
        const raw = this._lo + this._slider.value * (this._hi - this._lo);
        return Math.round(raw * 10) / 10;
    }

    _setSliderSpeed(speed) {
        this._syncing = true;
        this._slider.value = (speed - this._lo) / (this._hi - this._lo);
        this._syncing = false;
    }

    _onSliderValue() {
        if (this._syncing)
            return;
        if (this._dragging)
            return;  // one command when the drag ends, not per motion
        this._cancelSpeedCommit();
        this._commitTimeout = GLib.timeout_add(GLib.PRIORITY_DEFAULT, SPEED_COMMIT_MS, () => {
            this._commitTimeout = 0;
            this._commitSpeed();
            return GLib.SOURCE_REMOVE;
        });
    }

    _cancelSpeedCommit() {
        if (this._commitTimeout) {
            GLib.source_remove(this._commitTimeout);
            this._commitTimeout = 0;
        }
    }

    _commitSpeed() {
        // Any queued (debounced) commit is superseded: the last user
        // action wins, never a stale intermediate value.
        this._cancelSpeedCommit();
        // Hold the committed speed optimistically until the pad reports
        // it (or the commit is presumed lost); snapping back to the
        // stale state for ~1 s reads as a rejected drag.
        this._pendingSpeed = this._targetSpeed();
        this._pendingSince = GLib.get_monotonic_time();
        this._link.command({cmd: 'speed', speed: this._pendingSpeed});
    }

    _displaySpeed(state) {
        if (this._pendingSpeed !== null) {
            // Clear the hold the moment the pad confirms the speed (or
            // after a real 5 s — get_monotonic_time is microseconds).
            if (Math.abs(state.speed - this._pendingSpeed) <= 0.05 ||
                GLib.get_monotonic_time() - this._pendingSince
                    > 5 * GLib.USEC_PER_SEC) {
                this._pendingSpeed = null;  // presumed lost; the pad wins
            } else {
                return this._pendingSpeed;
            }
        }
        return state.speed;
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
        const CHART_H = 56;
        const GAP = 5;

        // The chart expands to whatever width the 2-column grid gives it;
        // the painter reads the actual allocation on every repaint.
        // reactive is required for motion/leave events to reach the actor
        // at all — without it the hover hit-test never fires.
        this._chart = new St.DrawingArea({
            x_expand: true,
            height: CHART_H,
            reactive: true,
            track_hover: true,
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
                this._updateCaption(this._state, this._running());
                actor.queue_repaint();
            }
            return true;
        });
        this._chart.connect('leave-event', () => {
            if (this._hoveredDay !== -1) {
                this._hoveredDay = -1;
                this._updateCaption(this._state, this._running());
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
            x_expand: true,
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
        const revision = this._state?.history_revision;
        const request = ++this._historyRequest;
        this._link.command({cmd: 'history'}, reply => {
            if (this._destroyed || request !== this._historyRequest)
                return;
            try {
                this._historyReply = reply?.ok ? reply : null;
                this._historyRevision = reply?.history_revision ?? revision;
                this._sessionDate = reply?.current?.date ?? null;
                this._days = aggregateHistory(this._historyReply);
                this._updateLiveHistory();
            } catch (e) {
                this._days = [];
            }
            this._applyHistory();
        });
        if (this._historyTimeout)
            GLib.source_remove(this._historyTimeout);
        this._historyTimeout = GLib.timeout_add_seconds(
            GLib.PRIORITY_DEFAULT, HISTORY_REFRESH_SECONDS, () => {
                this._historyTimeout = 0;
                this._refreshHistory();
                return GLib.SOURCE_REMOVE;
            });
    }

    _updateLiveHistory() {
        const state = this._state;
        // Keep the last chart until finalization has reached the history reply.
        // Mixing new live counters with an older history would lose a session;
        // mixing old counters with newer history would count it twice.
        if (!this._historyReply || !state ||
            state.history_revision !== this._historyRevision)
            return;
        if ((state.elapsed > 0 || state.steps > 0 || state.distance > 0) && !this._sessionDate)
            this._sessionDate = isoLocalDate(new Date());
        this._days = aggregateHistory({
            history: this._historyReply.history,
            current: this._sessionDate ? {
                date: this._sessionDate,
                session: {steps: state.steps, distance: state.distance},
            } : null,
        });
        this._applyHistory();
    }

    _applyHistory() {
        for (let i = 0; i < HISTORY_DAYS; i++) {
            const day = this._days[i];
            if (day && this._weekdayLabels && this._weekdayLabels[i]) {
                this._weekdayLabels[i].text = day.weekday;
                this._weekdayLabels[i].style =
                    'font-size: 9pt; padding: 1px 0; ' +
                    (day.today ? 'font-weight: bold;' : 'color: rgba(200, 200, 200, 0.75);');
            }
        }
        this._updateCaption(this._state, this._running());
        if (this._chart)
            this._chart.queue_repaint();
    }

    _updateCaption(state, running) {
        // Full-width caption, so long values fit: hovered day > live
        // session details > today's totals.
        const hovered = this._hoveredDay >= 0 ? this._days[this._hoveredDay] : null;
        if (hovered) {
            const km = (hovered.meters / 1000).toFixed(2);
            this._caption.text =
                `${hovered.date}: ${hovered.steps.toLocaleString()} steps · ${km} km`;
        } else if (running && state) {
            const km = (state.distance / 1000).toFixed(2);
            this._caption.text =
                `${this._formatDuration(state.elapsed)} · ${state.steps.toLocaleString()} steps · ${km} km`;
        } else {
            const today = this._days[this._days.length - 1];
            if (today && (today.steps > 0 || today.meters > 0)) {
                const km = (today.meters / 1000).toFixed(2);
                this._caption.text =
                    `today: ${km} km · ${today.steps.toLocaleString()} steps`;
            } else {
                this._caption.text = 'no history yet';
            }
        }
    }

    _onState(state) {
        const previous = this._state;
        this._state = state;
        this._updateLiveHistory();
        const running = this._running();
        if (running)
            this._clearCountdown();
        // Refresh on an actual finalization, including stop after pause or
        // a counter reset while the belt continues running.
        if (!previous || state.history_revision !== previous.history_revision)
            this._refreshHistory();

        // Bounds arrive once the daemon has read them off the pad.
        if (state.min_speed !== null && state.min_speed > 0)
            this._lo = state.min_speed;
        if (state.max_speed !== null && state.max_speed > this._lo)
            this._hi = state.max_speed;

        // Reflect the pad in the slider unless the user is dragging it or
        // a committed speed is still awaiting confirmation.
        if (running && !this._dragging && this._pendingSpeed === null &&
            Math.abs(this._targetSpeed() - state.speed) > 0.05)
            this._setSliderSpeed(state.speed);

        this._toggle.checked = running || state.starting === true || this._countdown > 0;
        this._toggle.subtitle = this._subtitle(state, running);
        // icon-label is the slider's accessible name — GNOME 50's
        // QuickSlider has no visible text slot; the speed shows in the
        // histogram caption line below.
        this._sliderItem.iconLabel =
            `${this._displaySpeed(state).toFixed(1)} km/h`;
        this._updateCaption(state, running);
        this._icon.opacity = running ? 255 : (state.connected ? 160 : 80);
    }

    _subtitle(state, running) {
        // Words + speed fit the ~30-character quick settings column;
        // the long values (duration, steps, distance) stay on the
        // full-width histogram caption.
        if (this._countdown > 0)
            return `starting in ${this._countdown}…`;
        if (state.starting)
            return 'starting…';
        if (!state.connected)
            return 'pad not connected';
        if (running)
            return `walking @ ${this._displaySpeed(state).toFixed(1)} km/h`;
        return 'stopped';
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
        this._clearCountdown();
        this._state = null;
        this._historyReply = null;
        this._historyRequest++;
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
        this._clearCountdown();
        if (this._commitTimeout)
            GLib.source_remove(this._commitTimeout);
        if (this._reconnectTimeout)
            GLib.source_remove(this._reconnectTimeout);
        if (this._historyTimeout)
            GLib.source_remove(this._historyTimeout);
        this._toggle.destroy();
        this._sliderItem.destroy();
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
