# Thread CoAP Bridge - v0.6.4

Home Assistant add-on for bridging Thread-attached CoAP devices into MQTT Discovery and stable `thread/...` topics.

Repository: `https://github.com/censay/thread-coap-bridge-addon`

## Current Status

This version keeps the important forked functionality intact:

- capability reconciliation from `/.well-known/core`
- `/auth` entities and bridge-side signature verification
- SED-friendly timeouts and offline recovery
- MQTT-first backend contract

It also removes stale OTBR assumptions from the previous iteration:

- the bridge no longer guesses OTBR web addresses
- the bridge no longer assumes `/api/devices` exists on every HA OTBR build
- OTBR resolution now prefers low-privilege Supervisor add-on info probes before broader add-on enumeration
- when OTBR web is reachable but inventory is absent, the bridge logs that fact explicitly and continues with the other discovery paths

The immediate success target for this path is one visible device with `/uptime`. If the device also exposes additional endpoints, the bridge will reconcile those automatically.

## Discovery Order

Each discovery cycle now works in this order:

1. Optional OTBR inventory, if the installed OTBR build exposes a usable inventory endpoint
2. Optional seed IPv6 bootstrap from `seed_ipv6_addresses`
3. Interface-derived IPv6 candidates from `wpan0`
4. Multicast `/.well-known/core`
5. Unicast re-discovery of previously known offline devices

This preserves the fork’s useful fallback behavior without pretending that any one path is universally available on every HAOS installation.

## Sources Of Truth

Use these in order when diagnosing the system:

### Firmware / device truth

- `C:\myfw\dot-kit\README.md`
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

- Discovers CoAP-enabled devices on a Thread mesh
- Parses `/.well-known/core` and maps resources to Home Assistant entities
- Publishes Home Assistant MQTT Discovery config
- Publishes stable backend-facing `thread/...` topics
- Polls and observes resources according to resource type
- Reconciles runtime changes when the firmware adds or removes endpoints
- Exposes `/auth` as:
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
| `multicast_address` | Multicast group used for discovery attempts | `ff03::fd` |
| `otbr_rest_url` | Optional OTBR web base URL override. Accepts either `http://host:8081` or `http://host:8081/api` | `""` |
| `seed_ipv6_addresses` | Optional seed addresses for first-contact unicast bootstrap | `[]` |
| `offline_threshold_polls` | Failures before the bridge marks a device offline | `5` |
| `cleanup_after_hours` | Offline age before cleanup | `24` |
| `cleanup_check_interval` | Cleanup loop interval in seconds | `3600` |

`otbr_rest_url` is an override, not a hardcoded default. On Home Assistant OS the bridge now prefers Supervisor metadata to discover OTBR dynamically.

## Device Requirements

Devices should expose:

- CoAP on UDP/5683
- `/.well-known/core`
- resources with useful `rt`, `if`, and `obs` attributes

Recommended minimum for this project path:

- `/uptime`
- `/auth`
- observable button or switch resource if user interaction matters

Example CoRE Link Format:

```text
</sw>;rt="button";if="sensor";obs,
</uptime>;rt="uptime";if="sensor",
</auth>;rt="auth";if="rw"
```

## MQTT Contract

The bridge’s stable backend-facing contract is MQTT, not raw Thread IPv6:

- `thread/{device_id}/availability`
- `thread/{device_id}/{resource}/state`
- `thread/{device_id}/{resource}/availability`
- `thread/{device_id}/auth_tier/state`

Home Assistant discovery topics under `homeassistant/...` are for HA entity registration only.

## Discovery Notes For HAOS + OTBR

The maintained bridge now distinguishes these cases explicitly:

- OTBR add-on not found via Supervisor API
- Supervisor add-on listing denied while direct OTBR `info` probes remain available
- OTBR add-on found but web bind unreachable
- OTBR web bind reachable but `/api/devices` missing on that build
- OTBR inventory reachable but empty

That distinction matters because the same HAOS setup can have:

- a healthy Thread mesh
- a reachable OTBR web service
- no OTBR inventory endpoint for network-wide device enumeration

In that case the bridge must fall back cleanly instead of reporting “no devices” as if the whole mesh were empty.

## Preserved Fork Behavior

These behaviors remain part of the maintained fork and are not being discarded:

- runtime capability reconciliation
- `/auth` bootstrap and signature verification
- SED-aware 65-second CoAP timeouts
- unicast re-discovery of offline devices
- retained MQTT state publishing
- per-resource observe parsing from `/.well-known/core`

Historical detail stays in `CHANGELOG.md`.

## Troubleshooting

### Device-side checks

Use the device shell:

```text
ot state
ot ipaddr
ot ping <thread-side-target-ipv6> 16 3
```

Expected:

- `ot state` reaches `child`
- `ot ipaddr` includes a Thread-routed address
- `ot ping` to the OTBR Thread-side IPv6 succeeds

### OTBR checks

From the OTBR add-on:

```sh
ot-ctl neighbor table
ot-ctl child table
cat /tmp/otbr-agent-rest-api
```

Interpretation:

- empty `child table` on OTBR does not prove the device is absent from the mesh
- the device may be attached under another router
- `/tmp/otbr-agent-rest-api` is the runtime OTBR web bind, not a public constant

### Bridge checks

Watch for logs that say:

- `Resolved OTBR web base via Supervisor API: ...`
- `OTBR web is reachable ... but /api/devices returned HTTP 404`
- `OTBR inventory returned ... candidate(s)`
- `Probing seed device via unicast: ...`
- `Discovered ... interface-derived candidate(s) on wpan0`

## Security Notes

- Thread still provides MAC-layer encryption
- MQTT still uses the configured broker credentials
- this add-on now requests `hassio_api` access so it can read OTBR add-on metadata from Supervisor
- Supervisor access is used to resolve OTBR runtime location, not to mutate OTBR state

## Matter Hub Note

`RiDDiX/home-assistant-matter-hub` is relevant as a future integration layer after this bridge works, because it can expose Home Assistant entities outward over Matter. It does not replace Thread-child discovery into HA or the CoAP normalization work in this add-on.

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
