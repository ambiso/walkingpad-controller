// Injected into a temporary extension copy, never installed in the real session.
function installPromo(indicator, directory) {
    // Render the packaged wallpaper directly. GNOME's image-loader
    // subprocess requires flatpak-spawn in some development environments.
    const wallpaper = Rsvg.Handle.new_from_file(`${directory}/wallpaper.svg`);
    const background = new St.DrawingArea({width: global.stage.width, height: global.stage.height});
    background.connect('repaint', area => {
        const cr = area.get_context();
        wallpaper.render_document(cr, new Rsvg.Rectangle({
            x: 0, y: 0, width: area.width, height: area.height,
        }));
        cr.$dispose();
    });
    Main.layoutManager._backgroundGroup.add_child(background);
    const pointer = Clutter.get_default_backend().get_default_seat()
        .create_virtual_device(Clutter.InputDeviceType.POINTER_DEVICE);
    const cursor = new St.Icon({
        gicon: new Gio.FileIcon({file: Gio.File.new_for_path(`${directory}/cursor.svg`)}),
        icon_size: 48, width: 48, height: 48, reactive: false,
    });
    cursor.set_pivot_point(6 / 48, 4 / 48);
    Main.uiGroup.add_child(cursor);
    let position = [global.stage.width * 0.6, global.stage.height * 0.5];
    let busy = false;
    let activeTimeline = null;
    let error = null;
    const visitedDays = new Set();
    let motionTimes = [];
    function place(x, y, scale = 1) {
        position = [x, y];
        pointer.notify_absolute_motion(GLib.get_monotonic_time(), x, y);
        // Align the SVG's tip, rather than its bounding box, with the pointer.
        cursor.set_position(x - 6, y - 4);
        cursor.set_scale(scale, scale);
        Main.uiGroup.set_child_above_sibling(cursor, null);
    }
    function target(name, fraction) {
        const actor = {
            menu: Main.panel.statusArea.quickSettings,
            toggle: indicator._toggle,
            slider: indicator._slider,
            chart: indicator._chart,
        }[name];
        if (!actor?.mapped)
            throw new Error(`Demo target is not visible: ${name}`);
        const [x, y] = actor.get_transformed_position();
        const [w, h] = actor.get_transformed_size();
        if (name === 'chart') {
            const [left, width] = actor._barLayout(w)(fraction);
            return [x + left + width / 2, y + h * 0.2];
        }
        if (name === 'slider') {
            const radius = actor._handleRadius * w / actor._barLevelWidth;
            let value = fraction ?? actor.value;
            if (actor.get_text_direction() === Clutter.TextDirection.RTL)
                value = 1 - value;
            return [x + radius + (w - 2 * radius) * value, y + h / 2];
        }
        return [x + w * (fraction ?? 0.5), y + h / 2];
    }
    function animate(destination, milliseconds, clicking = false, curved = true, dynamic = true, path = null) {
        const start = [...position];
        const dx = destination[0] - start[0];
        const dy = destination[1] - start[1];
        const length = Math.hypot(dx, dy);
        const bend = curved ? Math.min(length * 0.22, global.stage.height * 0.13) : 0;
        const nx = length ? -dy / length : 0;
        const ny = length ? dx / length : 0;
        const c1 = [start[0] + dx * 0.3 + nx * bend, start[1] + dy * 0.3 + ny * bend];
        const c2 = [start[0] + dx * 0.7 + nx * bend, start[1] + dy * 0.7 + ny * bend];
        const pointAt = u => {
            const v = 1 - u;
            return [0, 1].map(axis => v ** 3 * start[axis] +
                3 * v ** 2 * u * c1[axis] + 3 * v * u ** 2 * c2[axis] +
                u ** 3 * destination[axis]);
        };
        // Parameterize by distance so the curve itself does not speed up or
        // slow down the cursor. Only the time easing controls its motion.
        const distances = [0];
        let previous = start;
        for (let i = 1; i <= 128; i++) {
            const point = pointAt(i / 128);
            distances.push(distances.at(-1) + Math.hypot(
                point[0] - previous[0], point[1] - previous[1]));
            previous = point;
        }
        const atDistance = fraction => {
            const distance = Math.max(0, Math.min(1, fraction)) * distances.at(-1);
            const i = Math.max(1, distances.findIndex(value => value >= distance));
            const span = distances[i] - distances[i - 1];
            return pointAt((i - 1 + (span ? (distance - distances[i - 1]) / span : 0)) / 128);
        };
        const began = GLib.get_monotonic_time();
        busy = true;
        const timeline = Clutter.Timeline.new_for_actor(cursor, milliseconds);
        activeTimeline = timeline;
        timeline.connect('new-frame', (_, elapsed) => {
            try {
                const t = Math.min(1, elapsed / milliseconds);
                if (path)
                    motionTimes.push(GLib.get_monotonic_time() / 1000);
                // Minimum-jerk easing: velocity and acceleration are both
                // zero at each end, including joins to clicks and drags.
                const eased = t ** 3 * (10 - 15 * t + 6 * t * t);
                const point = path ? path(t) : atDistance(eased);
                if (indicator._hoveredDay >= 0)
                    visitedDays.add(indicator._hoveredDay);
                place(...point, clicking ? 1 - 0.22 * Math.sin(Math.PI * Math.min(1,
                    (GLib.get_monotonic_time() - began) / 120000)) :
                    dynamic ? 1 + 0.1 * Math.sin(Math.PI * eased) : 1);
            } catch (e) {
                error = String(e);
                busy = false;
                timeline.stop();
                activeTimeline = null;
            }
        });
        timeline.connect('completed', () => {
            place(...destination);
            if (clicking)
                pointer.notify_button(GLib.get_monotonic_time(), 1, Clutter.ButtonState.RELEASED);
            busy = false;
            activeTimeline = null;
        });
        timeline.start();
    }

    place(...position);
    globalThis.walkingpadPromo = {
        setMenuOpen(open) {
            const menu = Main.panel.statusArea.quickSettings.menu;
            if (open)
                menu.open();
            else {
                menu.close();
                place(global.stage.width * 0.6, global.stage.height * 0.5);
            }
        },
        move(name, fraction, duration = 650, curved = true) {
            animate(target(name, fraction), duration, false, curved, name !== 'chart');
        },
        historyTour() {
            visitedDays.clear();
            motionTimes = [];
            const finish = target('toggle');
            const route = historyPath(position, target('chart', 0), target('chart', 6), finish);
            animate(finish, route.duration, false, false, false, route.pointAt);
        },
        click() {
            pointer.notify_button(GLib.get_monotonic_time(), 1, Clutter.ButtonState.PRESSED);
            animate(position, 120, true);
        },
        drag(fraction) {
            pointer.notify_button(GLib.get_monotonic_time(), 1, Clutter.ButtonState.PRESSED);
            animate(target('slider', fraction), 900, true, false);
        },
        prepare() {
            Main.overview.hide();
            // Keep the panel clock from being cut in half by the camera crop.
            Main.panel.statusArea.dateMenu.opacity = 0;
            // Eval is enabled only in this private shell; hide its warning icon.
            Main.panel.statusArea.quickSettings._unsafeMode.hide();
            for (const source of Main.messageTray.getSources())
                source.destroy();
            // Critical startup banners bypass the notification preference.
            // Suppress their actor only in this disposable demo shell.
            if (Main.messageTray._bannerBin)
                Main.messageTray._bannerBin.opacity = 0;
        },
        frame() {
            const menu = Main.panel.statusArea.quickSettings.menu.box;
            const [x, y] = menu.get_transformed_position();
            const [width, height] = menu.get_transformed_size();
            const horizontalPadding = global.stage.width - x - width;
            return {x: x - horizontalPadding, y: 0,
                width: width + 2 * horizontalPadding, height: height + 2 * y,
                screenWidth: global.stage.width, screenHeight: global.stage.height};
        },

        status() {
            return {busy, error, timelinePlaying: activeTimeline?.is_playing(),
                timelineElapsed: activeTimeline?.get_elapsed_time(), motionIntervals: motionTimes.slice(1).map((time, i) => time - motionTimes[i]),
                sliderValue: indicator._slider.value, visitedDays: Array.from(visitedDays), bannerVisible: Boolean(Main.messageTray._bannerBin?.visible &&
                Main.messageTray._bannerBin?.opacity > 0), connected: indicator._state?.connected,
                running: indicator._running(), starting: indicator._state?.starting,
                speed: indicator._state?.speed, elapsed: indicator._state?.elapsed,
                steps: indicator._state?.steps, hoveredDay: indicator._hoveredDay,
                countdown: indicator._countdown, historyRevision: indicator._state?.history_revision,
                todaySteps: indicator._days.at(-1)?.steps, caption: indicator._caption.text};
        },
    };
}
