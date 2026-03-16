# Current Steps (2026-03-15)

## Start Here

Read these in order:

1. `CURRENT-STATUS.md`
2. `README.md`
3. `DOCS.md`

This file is the short resume sheet for the next work session.

## Deployed Baseline vs Current Tree

- deployed add-on baseline: `v0.6.8`
- current tree also includes unreleased follow-up fixes for:
  - duplicate announce dedupe
  - observe-timeout cleanup that stays resource-local
  - tracked reobserve task shutdown cleanup

## Before Any New Live Test

1. Verify the deployed add-on copy really matches the tree you intend to test.
2. Watch for:
   - `CoAP announce server listening on /announce ...`
   - `Received CoAP announce from ...`
   - `Handling announce for ...`
3. If the device never announces, check the device first before OTBR or multicast.
4. If the device announces but logs stay noisy, compare against the current tree fixes before assuming a firmware regression.

## Known Good Behavior

- announce-first discovery works
- the bridge can register a `dot-kit` device and publish HA entities
- `/auth` works end to end
- quiet runtime behavior is improved once devices are already known
- button observe traffic still works when the device is awake and attached

## Current Known Issues

- buttons are still stateful `binary_sensor` entities and can look sticky in HA
- `uptime` is still intentionally slow because it is polled every 120 seconds with an 80-second initial delay
- OTBR `/api/devices` remains absent on the validated HA OTBR build
- OTBR `/node` and multicast logs are still noisier than they need to be

## Recommended Next Improvements

1. Trim non-blocking OTBR and multicast log noise further.
2. Decide whether button behavior should stay binary-state based or move toward event-style handling.
3. Keep bridge docs aligned with firmware docs before the next release bump.
4. Revisit `kit-backend` consolidation only after the current baseline is stable and unsurprising.
