# Thread CoAP Bridge - v0.6.6

Home Assistant add-on for bridging Thread-attached CoAP devices into MQTT Discovery and stable `thread/...` topics.

Repository: `https://github.com/censay/thread-coap-bridge-addon`

## Current Status

This is the first working announce-first baseline for the maintained fork.

Validated with the current `dot-kit` firmware:

- the device attaches to Thread and multicasts a small announce payload to `/announce`
- the bridge receives that announce, queries `/.well-known/core`, and reconciles entities
- MQTT discovery is published for `auth_tier`, `auth_request`, `battery`, `led`, `sw0`, `sw1`, `uptime`, and `voltage`
- bridge-side `/auth` verification works and `auth_tier` has been observed moving from `1` to `2`
- observe registration works for `led` and `sw`

Important context:

- OTBR inventory is not the source of first contact on the validated HAOS system
- `/api/devices` is missing on the installed HA OTBR build
- OTBR `/node` only describes the border router itself on that build
- aiocoap multicast on `wpan0` remains unreliable and should be treated as fallback noise, not as the primary path

Start with `CURRENT-STATUS.md` if you are picking the project up midstream.

## Primary Discovery Path

The working first-contact path is now:

1. the device attaches to Thread
2. the device multicasts a JSON announce to `ff03::1:5685/announce`
3. the bridge records the source IPv6 and announced `device_id`
4. the bridge fetches `/.well-known/core` from that source IPv6
5. the bridge reconciles entities and starts observe/poll runtime monitoring

This is the path that is currently proven end to end.

## Fallback And Diagnostic Paths

The bridge still keeps the useful fallback logic from the fork:

1. OTBR inventory, if the installed OTBR build exposes it
2. OTBR `/node` diagnostics
3. optional seed IPv6 bootstrap from `seed_ipv6_addresses`
4. interface-derived IPv6 candidates from `wpan0`
5. multicast `/.well-known/core`
6. unicast re-discovery of previously known offline devices

Those paths are still useful for resilience and diagnosis, but they are no longer documented as the main discovery contract.

## Preserved Behavior From 0.5.0 Forward

The documentation cleanup does not discard the important forked behavior:

- runtime capability reconciliation from `/.well-known/core`
- MQTT-first backend contract under `thread/...`
- staggered polling cadence:
  - battery every 120 seconds starting at 0 seconds
  - voltage every 120 seconds starting at 40 seconds
  - uptime every 120 seconds starting at 80 seconds
- SED-friendly CoAP behavior and offline recovery
- unicast re-discovery of known devices
- `/auth` bootstrap, signature verification, and `auth_tier` state management

Historical detail stays in `CHANGELOG.md`.

## Known Current Behavior

### Buttons can feel sticky

That is currently expected in some cases:

- the bridge maps `/sw` to `binary_sensor` entities, not stateless HA events
- the firmware sends button observe notifications as NON-confirmable CoAP messages
- after the 120-second grace window the device is a sleepy child again
- if a release notification is delayed or missed, HA keeps the last binary state until the next update

The current system is functionally usable, but the UX is stateful rather than event-like.

### Uptime updates slowly

`uptime` is not broken. It is intentionally polled slowly:

- poll interval: 120 seconds
- initial delay: 80 seconds

That makes it useful for reboot detection and liveness, not as a high-frequency live counter.

### Light entity does not guarantee visible on-board LED feedback

The bridge only knows that the device exposes `/led` and that the firmware accepts light commands. The current `dot-kit` firmware maps `/led` to its configured GPIO LEDs and also pulses alarm output `P1.06` for `led_id == 0`. HA light success does not necessarily imply that the board LED the user expects will visibly change.

## Sources Of Truth

Use these in order when diagnosing the live system.

### Firmware / device truth

- `C:\myfw\dot-kit\README.md`
- `C:\myfw\dot-kit\CURRENT-STATUS.md`
- `C:\myfw\dot-kit\prj.conf`
- `C:\myfw\dot-kit\prj_uart.conf`
- serial shell commands:
  - `ot state`
  - `ot ipaddr`
  - `ot ping <thread-side-target-ipv6> 16 3`

### OTBR truth

- `ot-ctl neighbor table`
- `ot-ctl child table`
- `/tmp/otbr-agent-rest-api` inside the OTBR add-on
- actual HTTP status codes from the OTBR web bind

### Bridge truth

- add-on logs from this bridge
- `devices.db`
- MQTT topics under:
  - `thread/{device_id}/...`
  - `homeassistant/...`

Do not treat guessed URLs, guessed container names, or LAN hostnames as sources of truth.

## What This Add-on Does

- listens for device-initiated first contact on `/announce`
- discovers CoAP-enabled devices on a Thread mesh
- parses `/.well-known/core` and maps resources to Home Assistant entities
- publishes Home Assistant MQTT Discovery config
- publishes stable backend-facing `thread/...` topics
- polls and observes resources according to resource type
- reconciles runtime changes when the firmware adds or removes endpoints
- exposes `/auth` as:
  - `auth_tier` sensor
  - `auth_request` button

## Configuration

### Example

```yaml
mqtt_host: core-mosquitto
mqtt_port: 1883
mqtt_user: homeassistant
mqtt_password: your_secure_password
discovery_interval: 60
log_level: info
thread_interface: wpan0
multicast_address: ff03::fd
otbr_rest_url: ""
seed_ipv6_addresses: []
offline_threshold_polls: 5
cleanup_after_hours: 24
cleanup_check_interval: 3600
```

### Options

| Option | Description | Default |
|--------|-------------|---------|
| `mqtt_host` | MQTT broker hostname | `core-mosquitto` |
| `mqtt_port` | MQTT broker port | `1883` |
| `mqtt_user` | MQTT username | `homeassistant` |
| `mqtt_password` | MQTT password | `""` |
| `discovery_interval` | Discovery interval in seconds | `60` |
| `log_level` | `debug`, `info`, `warning`, `error` | `info` |
| `thread_interface` | Thread interface name | `wpan0` |
| `multicast_address` | Multicast group used for diagnostic discovery attempts | `ff03::fd` |
| `otbr_rest_url` | Optional OTBR web base URL override. Accepts either `http://host:8081` or `http://host:8081/api` | `""` |
| `seed_ipv6_addresses` | Optional seed addresses for unicast bootstrap or testing | `[]` |
| `offline_threshold_polls` | Failures before the bridge marks a device offline | `5` |
| `cleanup_after_hours` | Offline age before cleanup | `24` |
| `cleanup_check_interval` | Cleanup loop interval in seconds | `3600` |

`otbr_rest_url` is an override, not a hardcoded default. On Home Assistant OS the bridge prefers Supervisor metadata to resolve OTBR dynamically.

## Device Expectations

Devices should expose:

- CoAP on UDP/5683
- `/.well-known/core`
- resource types (`rt`) that can be mapped cleanly into HA

Recommended minimum for this project:

- `/uptime`
- `/auth`
- observable button or switch resource if user interaction matters

Example CoRE Link Format that works well with this bridge:

```text
</led>;rt="led";obs,</sw>;rt="button";obs,</battery>;rt="battery",</voltage>;rt="voltage",</uptime>;rt="uptime",</auth>;rt="auth"
```

## MQTT Contract

The bridge's stable backend-facing contract is MQTT, not raw Thread IPv6:

- `thread/{device_id}/availability`
- `thread/{device_id}/{resource}/state`
- `thread/{device_id}/{resource}/availability`
- `thread/{device_id}/auth_tier/state`

Home Assistant discovery topics under `homeassistant/...` are for HA entity registration only.

## Lessons Learned

- OTBR `/api/devices` cannot be assumed on Home Assistant OTBR builds.
- OTBR `/node` may only describe the border router, not the mesh.
- aiocoap multicast on `wpan0` is not reliable enough to be the primary first-contact mechanism here.
- device-initiated announce solved first contact cleanly without depending on OTBR inventory
- `/.well-known/core` parsing and reconciliation are still the correct capability handshake once first contact is established
- the system is much easier to reason about when OTBR is treated as transport/commissioning infrastructure and not as the application-level source of truth for every child

## Security Notes

- Thread still provides MAC-layer encryption
- MQTT still uses the configured broker credentials
- this add-on requests `hassio_api` access so it can read OTBR add-on metadata from Supervisor
- Supervisor access is used to resolve OTBR runtime location, not to mutate OTBR state

## Future Direction

Two future directions remain explicitly on the table:

- clean up the remaining OTBR and multicast log noise now that announce-first discovery works
- eventually fold this bridge behavior into `C:\github\kit-backend\` so MQTT, discovery normalization, and higher-level backend behavior live in one place

`RiDDiX/home-assistant-matter-hub` is still relevant after normalization if the goal becomes exposing these HA entities outward over Matter. It does not replace Thread-child discovery into HA.

## Development

### Local Build

```bash
git clone https://github.com/censay/thread-coap-bridge-addon.git
cd thread-coap-bridge-addon
docker build -t thread-coap-bridge .
```

### Python Verification

```bash
cd thread-coap-bridge/rootfs/app
python -m compileall .
```

## License

MIT License
