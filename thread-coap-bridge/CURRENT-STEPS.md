# Current Steps - v0.6.7

## Start Here

The project now has a working baseline. Read these in order:

1. `CURRENT-STATUS.md`
2. `README.md`
3. `DOCS.md`

This file is the short resume sheet for the next work session.

## Current Baseline

- announce-first discovery is working
- the bridge can register a `dot-kit` device and publish HA entities
- `/auth` is working end to end
- buttons, battery, voltage, uptime, and light entities all appear in HA

## Before Any New Live Test

1. Verify the deployed add-on copy really matches this tree:
   - `thread-coap-bridge/config.yaml` must say `version: "0.6.7"`
2. Watch for:
   - `CoAP announce server listening on /announce ...`
   - `Received CoAP announce from ...`
   - `Handling announce for ...`
3. If announce does not happen, check the device first before OTBR or multicast.

## Current Known Issues

- button entities can feel sticky because they are stateful `binary_sensor` entities backed by NON-confirmable observe notifications from a sleepy device
- `uptime` updates slowly by design because it is polled every 120 seconds with an 80-second initial delay
- OTBR `/api/devices` is unavailable on the validated HA OTBR build and should not be treated as a blocker once announce works
- OTBR `/node` and multicast logs are still noisy and should be treated as secondary diagnostics

## Recommended Next Improvements

1. Trim non-blocking OTBR and multicast noise from logs.
2. Decide whether button behavior should remain binary-state based or move toward event-style handling.
3. Verify and document the intended hardware effect of `/led`.
4. Evaluate eventual migration of bridge responsibilities into `C:\github\kit-backend\`.
