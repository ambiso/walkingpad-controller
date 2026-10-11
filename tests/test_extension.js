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
const context = vm.createContext({
    TextDecoder, Date,
    Gio: {}, Clutter: {}, St: {}, Main: {}, Extension: class {},
    GLib: {PRIORITY_DEFAULT: 0, source_remove() {}, timeout_add_seconds() { return 1; }},
    GObject: {registerClass: cls => cls},
    QuickSettings: {SystemIndicator: class {}},
});
vm.runInContext(`${source}\nglobalThis.Indicator = WalkingPadIndicator;`, context);

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
    const indicator = Object.create(context.Indicator.prototype);
    Object.assign(indicator, {
        _state: null, _days: [], _historyReply: null, _historyRevision: null,
        _historyRequest: 0, _sessionDate: null, _hoveredDay: -1,
        _countdown: 0, _countdownTimeout: 0, _historyTimeout: 0,
        _pendingSpeed: null, _lo: 1, _hi: 6, _slider: {value: 0.4},
        _chart: {queue_repaint() {}}, _caption: {}, _toggle: {}, _sliderItem: {}, _icon: {},
        _weekdayLabels: Array.from({length: 7}, () => ({})),
        _link: {command(request, callback) { requests.push(callback); }},
    });
    return {indicator, requests};
}
function history(revision = 0, currentDate = date(), steps = 16) {
    return {ok: true, history_revision: revision,
        history: [{reason: 'stop', date: date(), steps: 2300, distance: 1564, duration: 1400}],
        current: currentDate ? {date: currentDate, session: {steps, distance: 10, duration: 10}} : null};
}
function today(indicator) { return indicator._days.at(-1); }

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
