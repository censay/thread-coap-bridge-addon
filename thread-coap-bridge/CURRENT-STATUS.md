# Current Status - v0.6.8

## Working Baseline

This repository now has a working announce-first baseline with the current `dot-kit` firmware.

Confirmed end-to-end behavior:

- the device attaches to Thread
- the device sends a CoAP announce to `/announce`
- the bridge receives the announce and queries `/.well-known/core`
- the bridge registers the device and publishes MQTT discovery
- HA entities appear for:
  - `auth_tier`
  - `auth_request`
  - `battery`
  - `led`
  - `sw0`
  - `sw1`
  - `uptime`
  - `voltage`
- bridge-side auth verification works and `auth_tier` has been observed changing from `1` to `2`

## Important Context

- OTBR `/api/devices` is not available on the validated HA OTBR build.
- OTBR `/node` responds, but only with border-router self-description.
- aiocoap multicast on `wpan0` remains unreliable and should be treated as fallback or diagnostic only.
- once announce works, OTBR inventory failure is no longer a bring-up blocker.

## Known Current Limitations

### Buttons

Buttons currently feel somewhat sticky because the system is stateful:

- `/sw` becomes `binary_sensor` entities in HA
- notifications are NON-confirmable CoAP observe messages
- the device is a sleepy child after its grace period

That means HA can temporarily hold the last state until the next button update arrives.

### Uptime

`uptime` is intentionally slow:

- 120-second poll interval
- 80-second initial delay

It is useful for reboot detection and liveness, not for second-by-second display.

### LED feedback

The HA light entity confirms `/led` behavior, but visible on-board LED feedback is not guaranteed by the bridge. The current firmware also uses the same logical light action to pulse external alarm output `P1.06`.

## Sources Of Truth

- `README.md`
- `DOCS.md`
- `CURRENT-STEPS.md`
- bridge add-on logs
- `devices.db`
- MQTT topics under `thread/...`
- firmware docs in `C:\myfw\dot-kit`

## Next Recommended Work

1. Preserve the announce-first baseline and stop re-litigating OTBR inventory as the main path.
2. Decide whether buttons should remain `binary_sensor` entities or move toward event-style behavior.
3. Trim non-blocking OTBR and multicast noise from logs.
4. Verify and document the intended hardware effect of `/led`.
5. Evaluate consolidating this bridge logic into `C:\github\kit-backend\` once the behavior is stable.
