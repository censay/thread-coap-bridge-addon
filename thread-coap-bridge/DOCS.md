# Thread CoAP Bridge Docs - v0.6.8

## Purpose

This document is the operational companion to `README.md`. It captures the behavior that has actually been proven on HAOS + OTBR with the current `dot-kit` firmware.

## Operational Model

### First contact

The primary first-contact path is now device initiated:

1. the device attaches to Thread
2. the device sends a CoAP POST to `/announce`
3. the bridge uses the source IPv6 and announced `device_id`
4. the bridge fetches `/.well-known/core`
5. the bridge reconciles entities and begins normal monitoring

### Normal runtime

After registration:

- observe is used where the resource and device behavior make sense
- polling is used for slow-changing resources such as battery, voltage, and uptime
- runtime reconcile restarts watchers when the device reappears or its resource set changes
- auth state is maintained in the bridge and expires back to tier `1` after the configured TTL

## Fallback Model

Fallbacks still exist, but they are now clearly secondary:

1. OTBR inventory, if the installed OTBR build exposes it
2. OTBR `/node` diagnostics
3. seed IPv6 bootstrap
4. interface-derived candidates from `wpan0`
5. multicast `/.well-known/core`
6. unicast re-discovery of offline devices already known to the registry

These are helpful for diagnosis and recovery, but they are not the main success path anymore.

Operationally, the bridge now treats those scans as bootstrap tools:

- when the registry is empty, fallback discovery still runs to help find an initial device
- once devices are already known, the bridge prefers announce-first behavior and offline unicast recovery instead of repeating OTBR and multicast scans every discovery interval

## Proven Facts On The Validated HAOS Setup

- the device can join Thread and announce itself
- OTBR web can be resolved via Supervisor metadata
- `/tmp/otbr-agent-rest-api` is the runtime OTBR bind source
- OTBR `/api/devices` returns `404` on the installed HA OTBR build
- OTBR `/node` returns `200`, but only describes the border router itself
- aiocoap multicast on `wpan0` remains unreliable for first contact

## Sources Of Truth

### Firmware

- `C:\myfw\dot-kit\README.md`
- `C:\myfw\dot-kit\CURRENT-STATUS.md`
- `C:\myfw\dot-kit\prj.conf`
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

## Known Current Behavior

### Buttons

Buttons are currently represented as `binary_sensor` entities. Combined with NON-confirmable observe notifications from a sleepy device, this can make release events look delayed or sticky in HA.

### Uptime

`uptime` is intentionally slow:

- 120-second poll interval
- 80-second initial delay

### LED behavior

The bridge can confirm that `/led` is working as a CoAP resource, but it cannot promise that the user will see a specific on-board LED. That hardware mapping lives in the firmware and devicetree.

## What Not To Reinvestigate First

Do not restart from these assumptions unless the announce path regresses:

- OTBR inventory should list all mesh children
- OTBR `/node` should expose mesh-wide device inventory
- aiocoap multicast on `wpan0` is good enough as the main discovery source

Those were all investigated already and are not the current best path.

## Resume Checklist

If someone picks this project up later, verify in this order:

1. bridge log shows `CoAP announce server listening on /announce ...`
2. device log shows `Announce sent to ff03::1:5685/announce`
3. bridge log shows `Received CoAP announce from ...`
4. bridge log shows capability reconcile and MQTT discovery publish
5. HA shows `uptime` and the rest of the expected entities

## Matter Hub Reference

`RiDDiX/home-assistant-matter-hub` is relevant after normalization, not before.

Use it when:

- the bridge has already turned Thread/CoAP devices into HA entities
- you want those HA entities exposed outward over Matter

Do not use it as a substitute for first-contact discovery.
