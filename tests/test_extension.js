// Run with node --test tests/test_extension.js. Exercise the real extension
// methods with actor/socket stubs; rendering is covered by the promo tour.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../gnome-extension/extension.js'), 'utf8')
    .replace(/^import .*;$/gm, '')
    .replace('export default class', 'class');
const timers = new Map();
let nextTimer = 1;
const context = vm.createContext({
    TextDecoder, Date,
    Gio: {}, Clutter: {}, St: {}, Main: {notifyError() {}}, Extension: class {},
    GLib: {
        PRIORITY_DEFAULT: 0, SOURCE_REMOVE: false,
        source_remove(id) { timers.delete(id); },
        timeout_add_seconds() { return 1; },
        timeout_add(priority, milliseconds, callback) {
            const id = nextTimer++;
            timers.set(id, {milliseconds, callback});
            return id;
        },
    },
    GObject: {registerClass: cls => cls},
    QuickSettings: {SystemIndicator: class {}},
});
vm.runInContext(`${source}\nglobalThis.Indicator = WalkingPadIndicator; globalThis.DaemonLink = DaemonLink;`, context);

function date(offset = 0) {
    const value = new Date();
    value.setDate(value.getDate() + offset);
    return [value.getFullYear(), String(value.getMonth() + 1).padStart(2, '0'),
        String(value.getDate()).padStart(2, '0')].join('-');
}
function state(changes = {}) {
    return {connected: true, belt: 'started', speed: 3, min_speed: 1, max_speed: 6,
        elapsed: 10, steps: 16, distance: 10, history_revision: 0, ...changes};
}
function setup() {
    const requests = [];
    const commands = [];
    const indicator = Object.create(context.Indicator.prototype);
    Object.assign(indicator, {
        _state: null, _days: [], _historyReply: null, _historyRevision: null,
        _historyRequest: 0, _sessionDate: null, _hoveredDay: -1,
        _countdown: 0, _countdownTimeout: 0, _historyTimeout: 0, _toggleRequest: 0,
        _pendingSpeed: null, _speedTimeout: 0, _sendingSpeed: false,
        _speedRequest: null, _queuedSpeed: null, _lo: 1, _hi: 6, _slider: {value: 0.4},
        _chart: {queue_repaint() {}}, _caption: {}, _toggle: {}, _sliderItem: {}, _icon: {},
        _weekdayLabels: Array.from({length: 7}, () => ({})),
        _link: {command(request, callback) {
            requests.push(callback); commands.push({request, callback});
        }},
    });
    return {indicator, requests, commands};
}
function history(revision = 0, currentDate = date(), steps = 16) {
    return {ok: true, history_revision: revision,
        history: [{reason: 'stop', date: date(), steps: 2300, distance: 1564, duration: 1400}],
        current: currentDate ? {date: currentDate, session: {steps, distance: 10, duration: 10}} : null};
}
function today(indicator) { return indicator._days.at(-1); }

test('rejected start clears the countdown and reports the daemon error', () => {
    const {indicator, commands} = setup();
    indicator._onState(state({belt: 'stopped', speed: 0}));
    indicator._startCountdown = () => { indicator._countdown = 3; };
    const notifications = [];
    const notify = context.Main.notifyError;
    context.Main.notifyError = (...args) => notifications.push(args);
    try {
        indicator._onToggleClicked();
        const start = commands.find(item => item.request.cmd === 'start');
        start.callback({ok: false, result_text: 'failed'});
        assert.equal(indicator._countdown, 0);
        assert.equal(indicator._toggle.checked, false);
        assert.deepEqual(notifications, [['WalkingPad could not start', 'failed']]);
    } finally {
        context.Main.notifyError = notify;
    }
});

test('live counters update the bar and hovered caption without accumulating twice', () => {
    const {indicator, requests} = setup();
    indicator._onState(state());
    requests.at(-1)(history());
    indicator._hoveredDay = 6;
    indicator._onState(state({steps: 32, distance: 25, elapsed: 20}));
    assert.equal(today(indicator).steps, 2332);
    assert.equal(today(indicator).meters, 1589);
    assert.equal(today(indicator).duration, 1420);
    assert.equal(indicator._caption.text, `${date()}: 23:40 · 2,332 steps · 1.59 km`);
    assert.equal(indicator._hoveredDay, 6);
    assert.match(indicator._caption.text, /2,332/);
    indicator._onState(state({steps: 32, distance: 25, elapsed: 20}));
    assert.equal(today(indicator).steps, 2332);
    assert.equal(requests.length, 1);
    indicator._onState(state({belt: 'paused', steps: 32, distance: 25, elapsed: 20}));
    assert.equal(today(indicator).steps, 2332);
});

test('finalization waits for matching history, then a new session adds to it', () => {
    const {indicator, requests} = setup();
    indicator._onState(state());
    requests.at(-1)(history());
    indicator._onState(state({history_revision: 1, belt: 'stopped', steps: 0, elapsed: 0, distance: 0}));
    assert.equal(today(indicator).steps, 2316);
    const reply = history(1, null);
    reply.history.push({reason: 'stop', date: date(), steps: 16, distance: 10, duration: 10});
    requests.at(-1)(reply);
    assert.equal(today(indicator).steps, 2316);
    indicator._onState(state({history_revision: 1, steps: 2, elapsed: 1}));
    assert.equal(today(indicator).steps, 2318);
    assert.equal(today(indicator).duration, 1411);
});

test('out-of-order history responses cannot overwrite newer finalized history', () => {
    const {indicator, requests} = setup();
    indicator._onState(state());
    const older = requests.at(-1);
    indicator._onState(state({history_revision: 1}));
    requests.at(-1)(history(1));
    older(history(0, date(), 999));
    assert.equal(indicator._historyRevision, 1);
    assert.equal(today(indicator).steps, 2316);
});

test('history ahead of the watch stream never adds the previous session again', () => {
    const {indicator, requests} = setup();
    indicator._onState(state({steps: 100}));
    const reply = history(1, date(), 2);
    reply.history.push({reason: 'stop', date: date(), steps: 100, distance: 60});
    requests.at(-1)(reply);
    assert.equal(today(indicator).steps, 2402);
    indicator._onState(state({history_revision: 1, steps: 4, elapsed: 2}));
    assert.equal(today(indicator).steps, 2404);
});

test('a recovered session stays on its start date across midnight', () => {
    const {indicator, requests} = setup();
    indicator._onState(state());
    requests.at(-1)(history(0, date(-1)));
    indicator._onState(state({steps: 80}));
    assert.equal(today(indicator).steps, 2300);
    assert.equal(indicator._days.at(-2).steps, 80);
    assert.equal(today(indicator).duration, 1400);
    assert.equal(indicator._days.at(-2).duration, 10);
});

test('steps-only telemetry creates a live bar before duration is reported', () => {
    const {indicator, requests} = setup();
    indicator._onState(state({steps: 0, elapsed: 0, distance: 0}));
    requests.at(-1)(history(0, null));
    indicator._onState(state({steps: 2, elapsed: 0, distance: 0}));
    assert.equal(today(indicator).steps, 2302);
});


test('scroll sends immediately and newer targets survive older telemetry and replies', () => {
    const {indicator, commands} = setup();
    indicator._onState(state());
    indicator._slider.value = 0.6; // 4 km/h
    indicator._onSliderValue();
    const first = commands.at(-1);
    assert.equal(first.request.speed, 4);
    const oldTimer = indicator._speedTimeout;
    indicator._slider.value = 0.8; // 5 km/h while first command is in flight
    indicator._onSliderValue();
    assert.equal(commands.at(-1), first);
    assert.equal(timers.has(oldTimer), false);
    indicator._onState(state({speed: 4}));
    assert.equal(indicator._targetSpeed(), 5);
    assert.equal(indicator._displaySpeed(indicator._state), 5);
    first.callback({ok: false}); // even failure of an older request cannot release the hold
    const latest = commands.at(-1);
    assert.equal(latest.request.speed, 5);
    latest.callback({ok: true});
    indicator._onState(state({speed: 4.5}));
    assert.equal(indicator._targetSpeed(), 5);
    indicator._onState(state({speed: 5}));
    assert.equal(indicator._pendingSpeed, null);
    assert.equal(indicator._speedTimeout, 0);
});

test('unconfirmed speed falls back after three seconds even without new telemetry', () => {
    const {indicator} = setup();
    indicator._onState(state());
    indicator._slider.value = 0.8;
    indicator._onSliderValue();
    const timer = timers.get(indicator._speedTimeout);
    assert.equal(timer.milliseconds, 3000);
    timer.callback();
    assert.equal(indicator._pendingSpeed, null);
    assert.equal(indicator._targetSpeed(), 3);
    assert.match(indicator._toggle.subtitle, /3.0 km\/h/);
});

test('a failed latest speed request releases the hold immediately', () => {
    const {indicator, commands} = setup();
    indicator._onState(state());
    indicator._slider.value = 0.8;
    indicator._onSliderValue();
    commands.at(-1).callback(null);
    assert.equal(indicator._pendingSpeed, null);
    assert.equal(indicator._targetSpeed(), 3);
});

test('drag remains user-controlled and commits on release', () => {
    const {indicator, commands} = setup();
    indicator._onState(state());
    const before = commands.length;
    indicator._dragging = true;
    indicator._slider.value = 0.8;
    indicator._onSliderValue();
    indicator._onState(state({speed: 4}));
    assert.equal(commands.length, before);
    assert.equal(indicator._targetSpeed(), 5);
    indicator._dragging = false;
    indicator._commitSpeed();
    assert.equal(commands.at(-1).request.speed, 5);
});


test('command failures complete the callback so the speed queue can recover', () => {
    const originalGio = context.Gio;
    try {
        for (const failure of ['connect', 'write', 'read', 'eof', 'malformed', null]) {
            const replies = [];
            let closed = false;
            const connection = {
                get_output_stream() { return {write_all() {
                    if (failure === 'write') throw new Error('write failed');
                }}; },
                get_input_stream() { return {}; },
                close() { closed = true; },
            };
            context.Gio = {
                UnixSocketAddress: {new() { return {}; }},
                SocketClient: class {
                    connect_async(address, cancellable, callback) { callback(this, {}); }
                    connect_finish() {
                        if (failure === 'connect') throw new Error('connect failed');
                        return connection;
                    }
                },
                DataInputStream: class {
                    read_line_async(priority, cancellable, callback) { callback(this, {}); }
                    read_line_finish() {
                        if (failure === 'read') throw new Error('read failed');
                        const value = failure === 'eof' ? null : Buffer.from(
                            failure === 'malformed' ? '{' : '{"ok":true}');
                        return [value];
                    }
                },
            };
            const link = new context.DaemonLink(() => {}, () => {});
            link.command({cmd: 'speed', speed: 4}, reply => replies.push(reply));
            assert.equal(replies.length, 1, failure);
            if (failure) assert.equal(replies[0], null, failure);
            else assert.equal(replies[0].ok, true);
            assert.equal(closed, failure !== 'connect');
        }
    } finally {
        context.Gio = originalGio;
    }
});


test('display timeout does not discard the latest unsent speed change', () => {
    const {indicator, commands} = setup();
    indicator._onState(state());
    indicator._slider.value = 0.6;
    indicator._onSliderValue();
    const first = commands.at(-1);
    indicator._slider.value = 0.8;
    indicator._onSliderValue();
    timers.get(indicator._speedTimeout).callback();
    assert.equal(indicator._targetSpeed(), 3);
    first.callback({ok: true});
    assert.equal(commands.at(-1).request.speed, 5);
    assert.equal(indicator._pendingSpeed, null);
});


test('a rejected older Start cannot cancel a newer countdown', () => {
    const {indicator, commands} = setup();
    indicator._onState(state({belt: 'stopped', speed: 0}));
    indicator._startCountdown = () => { indicator._countdown = 3; };
    indicator._onToggleClicked();
    const first = commands.find(item => item.request.cmd === 'start');
    indicator._onToggleClicked(); // Stop
    indicator._onToggleClicked(); // a new Start
    first.callback({ok: false, error: 'superseded'});
    assert.equal(indicator._countdown, 3);
    assert.equal(indicator._toggle.checked, true);
});

test('command reply state cannot overwrite a newer watch update', () => {
    const originalGio = context.Gio;
    const states = [], replies = [];
    let complete;
    try {
        context.Gio = {
            UnixSocketAddress: {new() {}},
            SocketClient: class {
                connect_async(address, cancellable, callback) { callback(this, {}); }
                connect_finish() {
                    return {
                        get_output_stream() { return {write_all() {}}; },
                        get_input_stream() {}, close() {},
                    };
                }
            },
            DataInputStream: class {
                read_line_async(priority, cancellable, callback) { complete = () => callback(this, {}); }
                read_line_finish() { return [Buffer.from('{"ok":true,"state":{"speed":1}}')]; }
            },
        };
        const link = new context.DaemonLink(state => states.push(state), () => {});
        link.command({cmd: 'speed', speed: 3}, reply => replies.push(reply));
        link._onState({speed: 3}); // newer watch notification
        complete();
        assert.deepEqual(states, [{speed: 3}]);
        assert.equal(replies.length, 1);
        assert.equal(replies[0].ok, true);
    } finally {
        context.Gio = originalGio;
    }
});
