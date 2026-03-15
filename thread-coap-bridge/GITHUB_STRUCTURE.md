# Repository Structure Guide - v0.6.8

## Current Repository Shape

This project is currently structured as a Home Assistant custom repository with one add-on:

```text
thread-coap-bridge-addon/
  repository.yaml
  thread-coap-bridge/
    config.yaml
    Dockerfile
    build.yaml
    README.md
    DOCS.md
    CURRENT-STATUS.md
    CURRENT-STEPS.md
    INSTALL.md
    CHANGELOG.md
    rootfs/
```

This is the shape Home Assistant expects when the repo is added as a custom repository or copied locally into `/addons`.

## What To Preserve

Do not flatten the repo into a single directory when publishing or copying it.

The following must stay true:

- `repository.yaml` stays at the repository root
- `thread-coap-bridge/config.yaml` stays inside the add-on directory

## Local Add-on Copy

When copying into Home Assistant over Samba, the destination should look like:

```text
<HA addons share>/thread-coap-bridge-addon/
  repository.yaml
  thread-coap-bridge/
    config.yaml
    rootfs/
```

That exact shape matters.

## GitHub Custom Repository

If you publish this repo to GitHub, Home Assistant should be pointed at the repository root:

- `https://github.com/censay/thread-coap-bridge-addon`

For branch testing, append:

- `https://github.com/censay/thread-coap-bridge-addon#branch-name`

## Why This Matters

Past install failures were often not about bridge logic at all. They came from stale or malformed repository copies:

- wrong folder depth
- stale Samba copy
- stale version label in the copied add-on folder
- service scripts copied with Windows line endings before `.gitattributes` fixed that path

The fastest sanity check is always:

1. verify the copied repo root contains `repository.yaml`
2. verify `thread-coap-bridge/config.yaml` contains the expected version
3. then test runtime behavior
