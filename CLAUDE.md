# CLAUDE.md — ha-ipmi-control

## Project Overview

Home Assistant custom integration + add-on for IPMI/BMC server management. Single repo serves both:
- **Integration** (`custom_components/ipmi_controller/`) — HA config flow, entities, coordinator
- **Add-on** (`ipmi_control/`) — Docker container running FastAPI + ipmitool

GitHub: https://github.com/metril/ha-ipmi-control

## Architecture

```
HA Core (integration) --HTTP--> Add-on (FastAPI + ipmitool) --IPMI--> BMC
```

- **Integration** communicates with add-on via HTTP on HA's internal Docker network (port 8099)
- **Add-on** is stateless — credentials sent per-request, no persistence
- **No external port mapping** — add-on only reachable internally
- One config entry per IPMI host (4 servers = 4 entries)

## Key Files

| File | Purpose |
|------|---------|
| `custom_components/ipmi_controller/ipmi.py` | Async HTTP client to add-on API |
| `custom_components/ipmi_controller/coordinator.py` | DataUpdateCoordinator, polling-based |
| `custom_components/ipmi_controller/config_flow.py` | Multi-step config flow + options flow |
| `custom_components/ipmi_controller/const.py` | Motherboard profiles, constants |
| `custom_components/ipmi_controller/select.py` | Fan mode select with virtual mode mapping |
| `ipmi_control/app/main.py` | FastAPI endpoints wrapping ipmitool |
| `ipmi_control/app/ipmi.py` | Async subprocess wrapper for ipmitool |

## Entities (per host)

- **Switch** — Power (respects power_control policy: both/on/off/none)
- **Binary Sensor** — Power State
- **Select** — Fan Mode (Supermicro: Standard/Full/Optimum/Heavy IO + virtual modes)
- **Button** — Set Fan Thresholds
- **Sensor** — Fan threshold diagnostic sensors (per configured fan)

## Critical Design Decisions

### Why add-on instead of pyghmi?
pyghmi has a bug in `session.py` where `logoutexpiry` can be `None`, causing `TypeError` during BMC communication with Supermicro boards. The add-on uses proven `ipmitool` binary instead.

### Fan mode commands
Stored as structured dicts with `netfn`, `command`, `data` byte arrays in const.py. The integration converts these to `raw 0xNN 0xNN ...` strings for ipmitool. Multi-command modes (like metis quiet) execute commands sequentially.

### Fan sensor discovery
Add-on queries BMC SDR via `ipmitool sdr type Fan`. Config flow shows discovered fans as multi-select + manual entry fallback. SDR query can fail — always show the fan config step with manual entry.

### Credential model
Two privilege levels per host: Operator (chassis power) and Administrator (fan control, thresholds). Stored in config entry data, sent per-request to add-on.

## Add-on Details

- Base image: `ghcr.io/home-assistant/{arch}-base-python:3.13-alpine3.21`
- Concurrency: configurable `max_concurrent` semaphore (default 8)
- Discovery: `ipmi_control` — integration can auto-detect via Supervisor
- `config.yaml` has `image` field for pre-built Docker images

## CI/CD

- `.github/workflows/addon-build.yml` — builds Docker on `addon-v*` tags
- `.github/workflows/release.yml` — auto-creates releases on `v*` or `addon-v*` tags
- Tags: `v*` for integration, `addon-v*` for add-on

## Common Gotchas

1. **All ipmitool exceptions must propagate** — never swallow with `_LOGGER.error()` + return None. Raise `IpmiConnectionError` so coordinator handles backoff.
2. **Fan config step must always show** — never skip silently if SDR query fails. Show error + manual entry.
3. **Config entry data vs options** — credentials in `data` (immutable), fan/power config in `options` (mutable via options flow).
4. **Response mapping keys** — stored as strings in JSON, must convert back to int when reading.
5. **Docker build** — use `--amd64 --aarch64` not `--all` (deprecated). Base image needs full tag like `3.13-alpine3.21`.

## Git Conventions

- No AI attribution in commits
- GitHub user: metril
- Email: metril@users.noreply.github.com
