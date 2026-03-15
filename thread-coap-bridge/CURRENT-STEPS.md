# Current Steps - v0.6.5

## Current State

- Firmware is not the active blocker.
- The nRF54L15 can join Thread as `child` when commissioned with the documented Method 2 flow from `C:\myfw\dot-kit\README.md`.
- The device can ping the OTBR Thread-side IPv6, which proves mesh reachability.
- The bridge cleanup is complete in this tree and versioned as `0.6.5`.

## Proven Facts

### Device side

- `ot state` reached `child`
- `ot ipaddr` showed routed Thread addresses
- `ot ping fd35:5807:223f:1:426e:bdbf:1ec3:ee80 16 3` succeeded

### OTBR side

- `ot-ctl neighbor table` showed routers
- `ot-ctl child table` was empty on OTBR, which only means the device is not a direct child of the border router
- `/tmp/otbr-agent-rest-api` reported the OTBR web bind as `172.30.32.1:8081`
- `http://172.30.32.1:8081/api/devices` returned `404`
- `http://172.30.32.1:8081/node` returned `200`, but only described the border router itself

### Bridge side

- Multicast on `wpan0` is still unreliable in this HAOS environment
- Supervisor `/addons` enumeration returned `403`
- The bridge now probes OTBR add-on `info` endpoints directly before trying broader Supervisor enumeration
- The latest `v0.6.5` OTBR `/node` fallback produced `0` candidates because the payload only contained border-router metadata (`BaId`, `ExtAddress`, `ExtPanId`, `LeaderData`, `NetworkName`, `NumOfRouter`, `Rloc16`, `RlocAddress`, `State`)

## What Changed In v0.6.4

- Removed guessed OTBR defaults from runtime discovery
- Added Supervisor-based OTBR resolution
- Added OTBR `/node` fallback when `/api/devices` is missing
- Added explicit logging for:
  - OTBR web unreachable
  - OTBR inventory endpoint missing
  - OTBR `/node` payload shape and preview
  - OTBR inventory reachable but empty
- Preserved:
  - capability reconciliation
  - `/auth`
  - seed bootstrap
  - interface-derived candidate probing
  - multicast fallback
  - offline unicast re-discovery

## Current Live-Test Steps

1. Redeploy this exact bridge tree as add-on version `0.6.5`.
2. Keep the device attached as `child`. Do not vary firmware during this test.
3. Restart only the bridge add-on.
4. Watch for one of these log paths:
   - `Resolved OTBR web base via Supervisor API: ...`
   - `OTBR web is reachable at ... but /api/devices returned HTTP 404`
   - `OTBR /node payload ...`
   - `OTBR /node fallback produced ... candidate(s)`
   - `OTBR inventory returned ... candidate(s)`
5. OTBR `/node` is now proven to be a border-router self-description endpoint on this HA OTBR build, not a mesh inventory endpoint.
6. The next architecture step should not be an older bridge rollback. It should be a new discovery source:
   - device-initiated announce/register to the bridge, or
   - a helper with direct OTBR CLI/topology access, or
   - a separate OTBR deployment with its own radio if we want independent control

## Success Target

Minimum success:

- one visible device with `/uptime`

Bonus success:

- full resource reconciliation from `/.well-known/core`

## Sources Of Truth

- Firmware docs:
  - `C:\myfw\dot-kit\README.md`
  - `C:\myfw\dot-kit\prj_uart.conf`
- Device shell:
  - `ot state`
  - `ot ipaddr`
  - `ot ping`
- OTBR:
  - `ot-ctl neighbor table`
  - `ot-ctl child table`
  - `/tmp/otbr-agent-rest-api`
- real HTTP status codes from OTBR web
- OTBR `/node` payload shape from bridge logs
- Bridge:
  - add-on logs
  - `devices.db`
  - MQTT topics under `thread/...`
