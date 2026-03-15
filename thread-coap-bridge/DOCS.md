# Thread CoAP Bridge Docs - v0.6.5

## Purpose

This document is the operational companion to `README.md`. It keeps the discovery and troubleshooting path aligned with what has actually been proven on HAOS + OTBR.

## Discovery Model

The bridge now uses a layered model:

1. OTBR inventory when the installed OTBR build exposes it
2. OTBR `/node` fallback when inventory is absent on that HA OTBR build
3. Seed IPv6 bootstrap for explicitly supplied devices
4. Interface-derived IPv6 candidates from `wpan0`
5. Multicast CoAP discovery
6. Unicast re-discovery for devices already known to the registry

This is deliberate:

- OTBR inventory is the cleanest source when available
- seeds preserve first-contact recovery when multicast is unreliable
- interface heuristics and multicast remain fallback paths
- registry-based re-discovery preserves returning devices

## Sources Of Truth

### Firmware

- `C:\myfw\dot-kit\README.md`
- `C:\myfw\dot-kit\prj_uart.conf`

Key fact:

- the UART/debug build uses manual Thread start, so attach state after reset must be established with the documented Method 2 shell flow

### Device shell

- `ot state`
- `ot ipaddr`
- `ot ping <target> 16 3`

### OTBR

- `ot-ctl neighbor table`
- `ot-ctl child table`
- `/tmp/otbr-agent-rest-api`
- actual HTTP status codes from the OTBR web bind

### Bridge

- add-on logs
- `devices.db`
- MQTT state under `thread/...`

## OTBR Notes

The bridge no longer assumes these are valid:

- `http://127.0.0.1:8081/api`
- `http://localhost:8081/api`
- `http://core-openthread-border-router:8081/api`

Those were stale guesses.

The current logic is:

- if `otbr_rest_url` is set, use it as a direct override
- otherwise, probe known OTBR add-on `info` endpoints via Supervisor first
- only fall back to broader Supervisor add-on enumeration when direct OTBR probes miss
- extract the runtime OTBR add-on IP or hostname
- probe `http://<resolved-bind>:8081/api/devices`

If that inventory endpoint returns `404`, the bridge logs the OTBR web surface and disables OTBR inventory for that process run instead of retrying a stale path forever.

## Why This Matters

On the validated HAOS system used for this path:

- the device could join Thread and ping OTBR
- the OTBR web bind existed
- `/api/devices` returned `404`

That means these are different failure classes:

- mesh is down
- device is detached
- OTBR web is unreachable
- OTBR web exists but does not expose inventory

The bridge now keeps those classes separate in code and logs.

## Runtime Expectations

### Minimum success case

One visible device with:

- availability
- `/uptime`

### Bonus success case

All exposed resources from `/.well-known/core` reconciled into HA and MQTT.

## MQTT Contract

Consumers outside Home Assistant should use:

- `thread/{device_id}/availability`
- `thread/{device_id}/{resource}/state`
- `thread/{device_id}/{resource}/availability`
- `thread/{device_id}/auth_tier/state`

## Matter Hub Reference

`RiDDiX/home-assistant-matter-hub` is relevant after normalization, not before.

Use it when:

- the bridge has already turned Thread/CoAP devices into HA entities
- you want those HA entities exposed outward over Matter

Do not use it as a substitute for OTBR discovery or CoAP inventory.

## Cleanup Rules Applied In v0.6.4

- removed guessed OTBR default endpoints from code
- stopped documenting `/api/devices` as if it were universally available
- preserved seed bootstrap and offline re-discovery as useful fallback behavior
- kept `/auth`, capability reconciliation, and SED support
- updated version references across config and docs

## Next Approval Gate

After this cleanup lands, the next live test should be:

1. install the add-on version built from this tree
2. keep the chip attached as `child`
3. confirm whether bridge logs show:
   - Supervisor OTBR resolution
   - OTBR inventory unsupported or supported
   - fallback discovery progressing
4. stop only after either `/uptime` appears or the next single blocker is isolated
