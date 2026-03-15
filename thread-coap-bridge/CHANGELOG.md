# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.6.5] - 2026-03-15

### Added
- OTBR `/node` fallback when `/api/devices` is absent on the installed HA OTBR build
- Logging of OTBR `/node` payload shape and preview to support live discovery debugging

### Changed
- OTBR discovery now keeps working after `/api/devices` returns `404` by probing `/node` on subsequent cycles
- Add-on version updated to `0.6.5`

## [0.6.4] - 2026-03-12

### Added
- Supervisor-based OTBR add-on resolution using runtime add-on metadata instead of guessed hostnames
- OTBR web surface diagnostics when the configured HA OTBR build does not expose `/api/devices`
- Documentation sections for validated sources of truth and the current HAOS + OTBR discovery path

### Changed
- `otbr_rest_url` is now an optional override instead of a hardcoded localhost default
- OTBR inventory discovery now uses the resolved OTBR web base and disables itself for the current run when `/api/devices` returns `404`
- OTBR resolution now tries direct Supervisor `info` probes for known OTBR add-on slugs before broader add-on enumeration
- Add-on version updated to `0.6.4`

### Fixed
- Removed stale OTBR endpoint guesses from discovery logic and documentation
- Stopped treating OTBR inventory absence as if it were the same as “no Thread devices exist”

## [0.6.1] - 2026-03-12

### Added
- Seed IPv6 bootstrap configuration for first-contact unicast discovery from an empty registry
- Documentation for backend consumption of bridge-published MQTT topics

### Changed
- Discovery cycles now probe configured seed IPv6 addresses before multicast discovery
- Add-on version updated to `0.6.1`

## [0.6.0] - 2026-03-09

### Added
- Capability reconciliation driven by successful `/.well-known/core` responses
- Resource-handler registry for explicit entity and monitor mapping
- First-class `/auth` bridge support with `auth_request` and `auth_tier` entities
- Bridge-side ECDSA P-256 signature verification for `/auth`
- Regression tests for discovery parsing, registry reconciliation, handler mapping, and auth verification

### Changed
- Re-discovered offline devices now requeue runtime reconciliation even when their capability set is unchanged
- MQTT discovery is republished from reconciled entity state instead of sticky cached assumptions
- Repository and add-on metadata now point to the maintained `censay` fork
- Add-on version updated to `0.6.0`

### Fixed
- Removed the need to manually clear `devices.db` or retained MQTT discovery when resources are added or removed during development
- Fixed per-resource `obs` parsing in `/.well-known/core` payloads
- Finished monitor tasks now schedule runtime reconcile so observers and pollers are restarted cleanly

## [0.5.0] - 2025-01-25

### Investigated
- **Battery/voltage not updating in Home Assistant UI**
  - HA's `state_class: measurement` expects frequently changing values
  - Slow-changing sensors (battery %, voltage) may be discarded by statistics engine
  - This is part of HA's design philosophy, not a bug

### Changed
- Battery sensor: `device_class: battery` only (removed `state_class`)
- Voltage sensor: Removed `device_class: voltage` (preserves decimal display: 4.08V vs 4V)
- Uptime sensor: Keeps `state_class: total_increasing` (correct for always-increasing value)

### Tested
- Out-of-range test: Sensors show "Unavailable" after 240s Thread child timeout
- Return online: Unicast re-discovery works correctly
- Device reset: Observe re-registered automatically on uptime decrease

## [0.4.0] - 2025-01-20

### Added
- **Full SED (Sleepy End Device) support**
  - 65-second CoAP timeouts (accommodates 60s SED poll period)
  - 120-second polling interval for battery/voltage/uptime
  - Staggered polling: battery (0s), voltage (40s), uptime (80s)
- Per-sensor availability tracking
- Retry logic with 10s delay on poll failure
- Sensor failure threshold (3 failures → offline)

### Changed
- Polling interval increased from 60s to 120s for SED efficiency
- CoAP GET/PUT/Observe timeouts increased to 65s

## [0.3.0] - 2025-01-15

### Fixed
- **Critical: Devices not re-discovered after extended disconnection**
  - Root cause: aiocoap multicast unreliable on Thread/wpan0
  - Solution: Added unicast re-discovery probing last-known IPv6

### Added
- `rediscover_offline_devices()` - unicast probing for offline devices
- `get_offline_devices()` in device registry
- Device IP removed from cache when marked offline (enables re-discovery)

## [0.2.0] - 2025-01-12

### Fixed
- **Critical: LED state not displaying in Home Assistant**
  - Switched from JSON schema to basic schema with `state_value_template`
  - Handle nested response format: `{"leds": [{"led_id": 0, "state": 1}]}`
  - Added state retention (`retain=True`)

### Added
- Optimistic updates with command suppression (prevents UI flickering)
- `_translate_mqtt_to_coap()` for HA "ON"/"OFF" to device format
- Consecutive failure tracking per device
- Device cleanup after 24h offline

### Changed
- MQTT state publishing now uses `retain=True`
- Offline threshold: 5 failures before marking unavailable

## [0.1.0] - 2025-01-08

### Added
- Initial release
- CoAP multicast discovery for Thread devices
- Automatic device registration via `/.well-known/core`
- MQTT Discovery integration
- CoAP Observe support for real-time state updates
- Support for lights, switches, and sensors
- Battery level monitoring
- SQLite device registry
- Configuration via Home Assistant UI
- Multi-architecture support (amd64, aarch64, armv7)

### Known Issues
- DTLS encryption not yet supported (Thread provides MAC-layer encryption)
- Resource Directory (RD) mode not implemented
- No OTA firmware update support

## [Unreleased]

### Planned
- CoAP DTLS support for application-layer encryption
- Resource Directory mode for large deployments
- Custom resource type mapping
- Device grouping and bulk operations
- Metrics and statistics dashboard
