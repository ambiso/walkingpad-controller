import assert from 'node:assert/strict';
import test from 'node:test';
import {historyPath} from '../scripts/promo/motion.mjs';

const start = [940, 15];
const first = [420, 350];
const last = [840, 350];
const finish = [780, 230];
const path = historyPath(start, first, last, finish);
const at = milliseconds => path.pointAt(milliseconds / path.duration);
const difference = (a, b) => a.map((value, axis) => value - b[axis]);
const near = (a, b, tolerance) => a.forEach((value, axis) =>
    assert.ok(Math.abs(value - b[axis]) < tolerance, `${a} differs from ${b}`));

test('the tour passes through both sweep endpoints and finishes on the toggle', () => {
    near(at(0), start, 1e-9);
    near(at(650), first, 1e-9);
    near(at(2450), last, 1e-9);
    near(at(path.duration), finish, 1e-9);
});

test('both joins preserve nonzero velocity and continuous acceleration', () => {
    const h = 0.1;
    for (const time of [650, 2450]) {
        const left = difference(at(time), at(time - h)).map(value => value / h);
        const right = difference(at(time + h), at(time)).map(value => value / h);
        near(left, [420 / 1800, 0], 1e-6);
        near(right, left, 1e-6);
        const acceleration = offset => at(time + offset).map((value, axis) =>
            (value - 2 * at(time + offset - h)[axis] + at(time + offset - 2 * h)[axis]) / h ** 2);
        near(acceleration(0), acceleration(2 * h), 1e-4);
    }
});

test('the entire sweep stays straight at constant speed above the caption', () => {
    for (let time = 650; time <= 2450; time += 25)
        near(at(time), [420 + (time - 650) * 420 / 1800, 350], 1e-9);
    for (let time = 0; time <= path.duration; time += 5)
        assert.ok(at(time)[1] <= 350 + 1e-9);
});
