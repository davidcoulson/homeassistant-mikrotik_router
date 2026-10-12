# mikrotik_router (fork)

A Home Assistant custom integration (HACS) for MikroTik RouterOS. It talks to the router over the RouterOS API using librouteros.

## Repo and remotes

- `origin` = fork `davidcoulson/homeassistant-mikrotik_router`, `upstream` = `tomaae/homeassistant-mikrotik_router`.
- Never open PRs against upstream unless asked. PRs go branch → fork `master`.
- Commit as `23066302+davidcoulson@users.noreply.github.com`. GitHub's email privacy setting rejects pushes of commits authored with the private address.
- Work branch: `child-devices` (sub-device registration plus coordinator fixes). Review notes: `REVIEW-coordinator.md` (coordinator/API layer) and `REVIEW-codex.md` (Codex security, blocking and general review).

## Layout (`custom_components/mikrotik_router/`)

- `mikrotikapi.py`: sync librouteros wrapper (`MikrotikAPI`), with one lock per connection. `query()` disconnects on most errors.
- `apiparser.py`: `parse_api()` turns API rows into `ds[...]` dicts.
- `coordinator.py`: `MikrotikCoordinator` (main poll, runs `get_*` in the executor) and `MikrotikTrackerCoordinator` (10 s ping/presence). Shared `self.ds` state.
- `entity.py`: base entity, `unique_id`, `device_info`, platform setup helper.
- `*_types.py`: entity descriptions per platform (`ha_group`, `data_path`, `data_reference`, ...).
- `__init__.py`: entry setup. It registers the router device first and stores `runtime_data.router_device_id`.

## Device model

- Entities with `ha_group == "System"` go on the router device (identifier = RouterBOARD serial).
- Network clients (a `mac-address` reference) are separate devices with `via_device_id` = router.
- Everything else (interfaces, NAT/mangle/filter groups, queues, scripts, etc.) is a `ChildDeviceInfo` with `parent_device_id` = router. Identifiers come from `child_device_identifier(entry_id, ...)`. Keep the identifiers stable, because changing them duplicates devices on existing installs.
- At setup, `_async_migrate_child_devices` adds the child identifiers to legacy standalone devices, so HA converts them in place (keeping id, area and name). After the platforms load, `_async_remove_orphaned_devices` removes any non-router device or child device of the entry that has no entities.
- `async_remove_config_entry_device` refuses to remove the router device, since that would remove all of its children.

## Rules of thumb

- librouteros is blocking. Every API call must go through `hass.async_add_executor_job`, never run on the event loop.
- Don't mutate `ds` dicts in place from executor threads while the event loop iterates them. Build new dicts and swap them.
- `query()` returning `None` means an error, not "empty". Don't wipe state on `None`.

## Testing and lint

- `pytest` (config in `pytest.ini`, asyncio auto mode). It needs `pytest-homeassistant-custom-component` (tested with `==0.13.372`, HA 2026.10.1, Python 3.14) plus `librouteros` and `mac-vendor-lookup`. Use a venv in the scratchpad, not the repo.
- `flake8` (config in `setup.cfg`, max line 220) and `bandit` run in CI (`.github/workflows/ci.yml`).
- Tests: `tests/test_devices.py` (child devices, legacy migration, orphan cleanup, and a check that every entity description links to the router), `test_mikrotikapi.py`, `test_fixes.py`, `test_capabilities.py`.

## Tooling

- Use Codex (`codex-task <dir> "<task>"`) for chores and deep reviews, and the `local-llm` MCP for summarizing logs and Codex output.
- Use Sonnet/medium for side sessions unless the work is design or hard debugging.
