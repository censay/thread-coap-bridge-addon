# Current Status (2026-03-15)

## Start Here

Read these in order:

1. `README.md`
2. `CURRENT-STEPS.md`
3. `DOCS.md`

This file is the short handoff for what is true in the current tree.

## Working Baseline

Confirmed end-to-end behavior:

- the device attaches to Thread
- the device sends announce to `/announce`
- the bridge receives announce and queries `/.well-known/core`
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
- observe notifications flow for `/sw` and `/led` when the device is awake and attached

## Current Progress

- startup no longer deletes `/data/devices.db`
- known devices are restored as offline on startup instead of being hammered immediately
- once devices are already known, discovery stays on the quieter announce-first path and skips repeated OTBR/interface/multicast scans
- duplicate attach announce bursts are deduped within a short time window
- reobserve tasks are tracked and cancelled cleanly during shutdown
- observe registration timeout no longer implies that the whole device is offline

## Immutable Facts

- announce-first is the validated first-contact path
- OTBR `/api/devices` is not available on the validated HA OTBR build
- OTBR `/node` only describes the border router on that build
- aiocoap multicast on `wpan0` remains fallback/diagnostic only
- the deployed add-on baseline is still `v0.6.8`
- this repository contains additional post-`0.6.8` tree fixes that are documented in `CHANGELOG.md`

## Known Limitations

### Buttons

Buttons still feel somewhat sticky because the system is stateful:

- `/sw` is represented as `binary_sensor` entities
- notifications are NON-confirmable observe messages
- the device is sleepy after its grace window

### Uptime

`uptime` is intentionally slow:

- 120-second poll interval
- 80-second initial delay

### LED feedback

The bridge can confirm that `/led` works as a CoAP resource, but it cannot guarantee a specific visible board LED effect. The current XIAO expansion-board firmware no longer claims `D2/P1.06` for the old alarm-pulse path, so the hardware-visible behavior should be treated as a firmware/devicetree question rather than a bridge fact.

## Lessons Learned

- treat OTBR as transport and commissioning infrastructure, not as the app-layer source of truth for every child device
- when announce works, OTBR inventory failure is no longer a bring-up blocker
- noisy observe or multicast behavior is easier to reason about when resource-local failures do not automatically become whole-device offline
- duplicate attach announces are normal enough that the bridge should absorb them instead of re-running full reconcile every time

## Guard Rails

- preserve the announce-first baseline before adding more discovery complexity
- keep OTBR/interface/multicast scans as bootstrap and diagnostic tools, not the primary steady-state path
- do not mark the whole device offline just because an observe registration times out once
- prefer explicit runtime ownership and cleanup for tasks so shutdown does not leave dangling asyncio tasks behind

## Sources Of Truth

- `README.md`
- `CURRENT-STEPS.md`
- `DOCS.md`
- add-on logs
- `devices.db`
- MQTT topics under `thread/...`
- firmware docs in `C:\myfw\dot-kit`

## Next Recommended Work

1. Trim the remaining non-blocking OTBR and multicast noise from logs.
2. Decide whether button behavior should remain `binary_sensor`-style or move toward an event-like HA surface.
3. Keep firmware and bridge docs aligned before the next add-on version bump.
4. Revisit `kit-backend` consolidation only after the current announce-first behavior is stable and boring.
