// Quintic Hermite segments share position, velocity, and acceleration at joins.
// Velocities use pixels/ms; acceleration is zero at each endpoint.
function segment(from, to, duration, startVelocity, endVelocity) {
    const coefficients = from.map((origin, axis) => {
        const delta = to[axis] - origin;
        const a = startVelocity[axis] * duration;
        const b = endVelocity[axis] * duration;
        return [origin, a, 10 * delta - 6 * a - 4 * b,
            -15 * delta + 8 * a + 7 * b, 6 * delta - 3 * a - 3 * b];
    });
    return t => coefficients.map(([origin, a, c, d, e]) =>
        origin + a * t + c * t ** 3 + d * t ** 4 + e * t ** 5);
}

export function historyPath(start, first, last, finish) {
    const approach = 650;
    const sweep = 1800;
    const departure = 550;
    const velocity = [(last[0] - first[0]) / sweep, 0];
    const enter = segment(start, first, approach, [0, 0], velocity);
    const leave = segment(last, finish, departure, velocity, [0, 0]);
    const duration = approach + sweep + departure;
    return {
        duration,
        pointAt(fraction) {
            const time = Math.max(0, Math.min(1, fraction)) * duration;
            if (time < approach)
                return enter(time / approach);
            if (time <= approach + sweep)
                return [first[0] + velocity[0] * (time - approach), first[1]];
            return leave((time - approach - sweep) / departure);
        },
    };
}
