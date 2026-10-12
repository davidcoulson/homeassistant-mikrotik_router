# mikrotik_router (fork)

A Home Assistant custom integration (HACS) for MikroTik RouterOS. It talks to the router over the RouterOS API using librouteros.

## Repo and remotes

- `origin` = fork `davidcoulson/homeassistant-mikrotik_router`, `upstream` = `tomaae/homeassistant-mikrotik_router`.
- Never open PRs against upstream unless asked. PRs go branch → fork `master`.
- Commit as `23066302+davidcoulson@users.noreply.github.com`. GitHub's email privacy setting rejects pushes of commits authored with the private address.
- Work branch: `child-devices` (sub-device registration plus coordinator fixes). Review notes: `REVIEW-coordinator.md` (coordinator/API layer), `REVIEW-codex.md` (Codex security, blocking and general review) and `PROFILE.md` (API calls and recorder load). `claude/fix-findings` fixes them on top of `child-devices`.

## Layout (`custom_components/mikrotik_router/`)

- `mikrotikapi.py`: sync librouteros wrapper (`MikrotikAPI`), with one lock per connection. `query()` disconnects on most errors. 15 s socket timeout; `close()` makes the API refuse to reconnect (called on unload and after config-flow validation). With verify SSL on, the hostname is checked too.
- `apiparser.py`: `parse_api()` turns API rows into `ds[...]` dicts.
- `coordinator.py`: `MikrotikCoordinator` (main poll, runs `get_*` in the executor) and `MikrotikTrackerCoordinator` (10 s ping/presence). Shared `self.ds` state.
  - Tiered polling: `self._slow_poll` is true every `SLOW_POLL_INTERVAL` (300 s), after a lost connection, or after `async_request_full_refresh()` (switches call it). Only then are dhcp-client, dhcp-server, `/interface/ethernet` config, wireless config, nat/mangle/filter and the TX/RX byte totals read. The hw-info block (access, capabilities, routerboard, dhcp network, DNS) still runs every 4 h or on reconnect. The firmware check has its own 4 h cadence and never runs during the first refresh.
  - CPU load and temperatures go through `_apply_hysteresis` (publish on a move of 2, or after 5 min). Throughput is rounded to 3 significant figures (`_round_rate`).
  - Ethernet monitor is one call per group (`numbers=*1,*2,...`), falling back to per-port calls if the router traps.
  - Tracker: pings only with the "test" policy, in one executor job, skipping hosts seen within half the timeout. A failed ping (`None`) keeps the previous state. API commands from entities and the tracker share `coordinator.command_lock`.
  - `async_process_host` holds `_host_lock`; `get_arp`/`get_dhcp`/`get_bridge` parse into a copy and swap it in.
- `vendor_lookup.py`: wraps `mac_vendor_lookup` with a 1 h back-off after a failed OUI list load.
- `entity.py`: base entity, `unique_id`, `device_info`, platform setup helper.
- `*_types.py`: entity descriptions per platform (`ha_group`, `data_path`, `data_reference`, ...).
- `__init__.py`: entry setup. It registers the router device first and stores `runtime_data.router_device_id`.

## Device model

- Entities with `ha_group == "System"` go on the router device. Its identifier (`runtime_data.router_identifier`) is `(DOMAIN, serial)`, or `(DOMAIN, entry_id)` when there is no valid serial (CHR/x86 report "N/A"); a placeholder device is re-keyed in place.
- Network clients (a `mac-address` reference) are separate devices with `via_device_id` = router.
- Everything else (interfaces, NAT/mangle/filter groups, queues, scripts, etc.) is a `ChildDeviceInfo` with `parent_device_id` = router. Identifiers come from `child_device_identifier(entry_id, ...)`. Keep the identifiers stable, because changing them duplicates devices on existing installs.
- At setup, `_async_migrate_child_devices` adds the child identifiers to legacy standalone devices, so HA converts them in place (keeping id, area and name). After the platforms load, `_async_remove_orphaned_devices` removes any non-router device or child device of the entry that has no entities.
- `async_remove_config_entry_device` refuses to remove the router device, since that would remove all of its children, and any device that still has an available entity.
- NAT/mangle/filter switches are keyed by the rule `.id` (unique id `{name}-{key}-{slug(.id)}`). `_async_migrate_firewall_unique_ids` renames the old content-based unique ids at setup, keeping entity_id and history.
- SFP DDM readings and wireless client signal/CCQ/rates are disabled-by-default diagnostic sensors (`MikrotikDiagnosticSensor`), not attributes.

## Rules of thumb

- librouteros is blocking. Every API call must go through `hass.async_add_executor_job`, never run on the event loop.
- Don't mutate `ds` dicts in place from executor threads while the event loop iterates them. Build new dicts and swap them.
- `query()` returning `None` means an error, not "empty". Don't wipe state on `None`.
- New entries default to SSL with verification (`DEFAULT_SSL`, `DEFAULT_VERIFY_SSL`). Entries without them get a repair issue (`insecure_connection_*`, `unverified_connection_*`); never flip their settings silently.
- No `pytz`; use `datetime.fromtimestamp(ts, tz=UTC)`.

## Testing and lint

- `pytest` (config in `pytest.ini`, asyncio auto mode). It needs `pytest-homeassistant-custom-component` (tested with `==0.13.372`, HA 2026.10.1, Python 3.14) plus `librouteros` and `mac-vendor-lookup`. Use a venv in the scratchpad, not the repo.
- `flake8` (config in `setup.cfg`, max line 220) and `bandit` run in CI (`.github/workflows/ci.yml`).
- Tests: `tests/test_devices.py` (child devices, legacy migration, orphan cleanup, and a check that every entity description links to the router), `test_mikrotikapi.py`, `test_fixes.py`, `test_capabilities.py`, plus one file per fix area (`test_tracker.py`, `test_security.py`, `test_router_identity.py`, `test_firewall_ids.py`, `test_commands.py`, `test_coordinator_robustness.py`, `test_diagnostic_sensors.py`, ...). Coordinator methods are tested on `object.__new__(MikrotikCoordinator)` with a mocked `api.query`.

## Tooling

- Use Codex (`codex-task <dir> "<task>"`) for chores and deep reviews, and the `local-llm` MCP for summarizing logs and Codex output.
- Use Sonnet/medium for side sessions unless the work is design or hard debugging.
