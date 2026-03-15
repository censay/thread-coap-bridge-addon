# Installation Guide - v0.6.8

## What You Are Installing

This repository is a Home Assistant custom add-on repository. The add-on itself lives in:

- `thread-coap-bridge/`

The repository root contains:

- `repository.yaml`

Do not flatten that structure when copying or publishing it.

## Prerequisites

1. Home Assistant with add-on support
2. OpenThread Border Router add-on installed and running
3. Mosquitto broker add-on installed and running
4. A commissioned Thread device running the matching `dot-kit` firmware

## Option 1: Local Installation Over Samba

This is the best path for development and A/B testing.

### Copy layout

Copy the repository root into the Home Assistant add-ons share so the result looks like:

```text
<HA addons share>/thread-coap-bridge-addon/
  repository.yaml
  thread-coap-bridge/
    config.yaml
    Dockerfile
    rootfs/
```

The important point is that `repository.yaml` stays at the copied repo root and `config.yaml` stays inside `thread-coap-bridge/`.

### Install steps

1. Delete any stale local copy of the repo from the HA add-ons share.
2. Copy the fresh repo root into the add-ons share.
3. In Home Assistant, open the Add-on Store and refresh it.
4. Open the local repository entry.
5. Install or rebuild the `Thread CoAP Bridge` add-on.

## Option 2: GitHub Custom Repository

If you want Home Assistant to pull from GitHub instead of Samba:

1. Push this repo to GitHub.
2. In Home Assistant, open:
   - Settings -> Add-ons -> Add-on Store -> menu -> Repositories
3. Add:
   - `https://github.com/censay/thread-coap-bridge-addon`
4. For branch-specific testing, append `#branch-name`.

This repository already has the correct custom repository shape.

## Configuration

Minimum useful configuration:

```yaml
mqtt_host: core-mosquitto
mqtt_port: 1883
mqtt_user: homeassistant
mqtt_password: your_mqtt_password
discovery_interval: 60
log_level: info
thread_interface: wpan0
multicast_address: ff03::fd
```

Notes:

- `otbr_rest_url` is optional and should only be set as an override
- OTBR inventory is diagnostic or fallback on the validated HA build, not the primary first-contact path
- first contact is now expected to come from the device announce flow

## Verification

After starting the add-on, look for these logs:

- `Initializing CoAP announce server...`
- `CoAP announce server listening on /announce ...`
- `Received CoAP announce from ...`
- `Handling announce for ...`

After that, expect MQTT discovery publish and HA entity creation.

## Troubleshooting

### Add-on starts but devices do not appear

Check these in order:

1. Bridge log shows the announce server listening.
2. Device serial log shows `Announce sent to ff03::1:5685/announce`.
3. Bridge log shows `Received CoAP announce from ...`.
4. Device responds to `/.well-known/core`.

Do not start with OTBR inventory assumptions. On the validated HA build:

- OTBR `/api/devices` returned `404`
- OTBR `/node` only described the border router

### Local add-on copy looks wrong

Verify:

- `repository.yaml` exists at the copied repo root
- `thread-coap-bridge/config.yaml` exists inside that repo copy
- `thread-coap-bridge/config.yaml` shows the expected version

If in doubt, delete the copied repo folder from the add-ons share and recopy the whole repo root.

### MQTT entities do not appear

Check:

1. Mosquitto is running.
2. MQTT integration in HA is connected.
3. The bridge log shows MQTT discovery publishing.

## Development Notes

- `README.md` describes the current working architecture.
- `CURRENT-STATUS.md` is the shortest handoff document.
- `DOCS.md` explains why announce-first discovery replaced OTBR inventory as the primary path.
