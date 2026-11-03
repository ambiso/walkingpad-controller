#!/usr/bin/env bash
# Run on the host; the report is saved beside this script, not in $PWD.
set -u

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P) || exit 1
report="$script_dir/walkingpad-gnome-diagnostics.txt"
uuid=walkingpad@ambiso

run() {
    printf '\n$ %s\n' "$*"
    "$@" || printf 'Command exited with status %s\n' "$?"
}

if ! {
    date -Is
    run gnome-shell --version
    run gnome-extensions info "$uuid"
    run gsettings get org.gnome.shell disable-user-extensions
    run gsettings get org.gnome.shell enabled-extensions
    run gsettings get org.gnome.shell disabled-extensions

    printf '\nExtension installation directories:\n'
    data_dirs="${XDG_DATA_HOME:-$HOME/.local/share}:${XDG_DATA_DIRS:-/usr/local/share:/usr/share}:/run/current-system/sw/share"
    IFS=: read -r -a directories <<< "$data_dirs"
    for directory in "${directories[@]}"; do
        metadata="$directory/gnome-shell/extensions/$uuid/metadata.json"
        if [[ -f "$metadata" ]]; then
            printf '\n%s\n' "$metadata"
            cat -- "$metadata"
        fi
    done

    printf '\nRelevant GNOME session logs (this boot, last 120 matching/context lines):\n'
    if command -v journalctl >/dev/null 2>&1; then
        journalctl --user -b --no-pager -o cat |
            grep -i -A8 -B3 -E 'walkingpad@ambiso|JS ERROR|TypeError|ReferenceError' |
            tail -120
    else
        printf 'journalctl is unavailable.\n'
    fi
} > "$report" 2>&1; then
    printf 'Could not finish writing report: %s\n' "$report" >&2
    exit 1
fi

printf 'Report saved to: %s\n' "$report"
