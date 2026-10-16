# Pad settings: no-load stop and idle sleep

[Back to the README](../README.md)

## No-load auto-stop

The client can query and configure the stop triggered when nobody is on the belt:

```sh
walkingpad-client auto-stop
walkingpad-client auto-stop on
walkingpad-client auto-stop on 30
walkingpad-client auto-stop off
```

`auto-stop on` without a timeout preserves the existing duration settings.
Disabling also preserves them. A write is followed by a read; success requires
that the requested enable flag and any requested custom timeout are reported
back. This confirms the property, not the physical stopping behavior.

The query returns `enabled`, `supports_seconds`, `custom_seconds`, `seconds`,
and the `raw` property value. `seconds` is the stored low-bit value; when
`custom_seconds` is false it is not evidence that this duration is active.
The protocol field accommodates 1–8191 seconds. This is an encoding limit,
not a verified firmware range; the pad may reject or constrain values.

## Evidence and limitations

Static inspection of KS Fit 6.6.0 (version code 5304) identifies vendor property
2 as `autoStop`. Its parser (`WilinkDeviceSupplementExt._setPropertyToBlueModel`,
address `0x1ccd4b4`) decodes:

| Bits | Meaning in the app |
| --- | --- |
| 15 | Auto-stop enabled |
| 14 | Supports setting duration (`couldSetTime`) |
| 13 | Custom duration enabled (`setTimeEnable`) |
| 0–12 | Duration in seconds |

`WilinkDeviceActionExt.setAutoStop` at `0xf44b64` builds the custom-duration
write as `0xE000 + seconds`. The app's settings dialog is explicitly labelled
“No-load stop” (`SettingNoloadView`), reached through `setupAutoStopPreference`.
The daemon uses the same encoding when enabling a custom duration, and preserves
unrelated fields when toggling the enable flag.

A user-supplied KS-HD-Z1E Bluetooth capture confirms that writing `0xe01e`
(enabled, custom timeout 30 seconds) received `72 81 02 02 00 f7`: property 2,
error 0. The pad then emitted a `72 50` property-change notification and returned
`0xe01e` in subsequent property reads. This verifies acceptance and read-back;
actual no-load stopping has not been verified. The captured exchange is also
replayed in a regression test, with unsolicited change events interleaved to
ensure they cannot satisfy pending property reads or writes.

The property value previously read from the user's KS-HD-Z1E was `0x403c`:
auto-stop disabled, duration setting supported, custom duration disabled,
stored duration 60 seconds, according to this parser.

## Idle sleep (Ruhemodus)

No configurable idle sleep duration was found in the app's device preference
construction, FTMS property enumeration/parser, or sleep-button control path.
Property 10 describes mode and capabilities, including sleep support; its mode
setter changes the mode rather than setting a timer. Property 12 (`zoneTime`)
is time-zone configuration, not a sleep timeout.

The [Z1F-family German manual, printed page 26](https://www.axdiaservice.com/wp-content/uploads/Anleitung/Kingsmith%20Z1F%20Manual_DE-IT-ES_20240318.pdf#page=8)
distinguishes no-load stop (8–60 seconds) from display-off/sleep after 10 idle
minutes. It says the app connection keeps the display on and describes remote
sleep/wake controls, but gives no adjustable idle timeout. This family manual
does not establish every behavior of the user's Z1E firmware.

The evidence supports configuring no-load stop, and explicitly switching modes,
but does not establish a command for changing the idle sleep delay. An
undocumented firmware setting remains possible. No speculative property writes
were sent to the pad during this investigation.

Research input: KS Fit XAPK SHA-256
`11c7eb0aaea861b2ca0856128c9e8bdfbeed1538a013059151d6645c2bec4d82`.
The APK and Blutter disassembly are retained outside Git under
`~/.cache/walkingpad-research/`.
