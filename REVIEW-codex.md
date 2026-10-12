# Review: mikrotik_router security, blocking and platform defects (child-devices f82e022)

Method: three read-only Codex deep scans (security, blocking/concurrency, general/platform) plus my own read of `__init__.py`, `entity.py`, `diagnostics.py`, `config_flow.py`, `switch.py`, `button.py`. Every item below was re-checked against the source at f82e022; items Codex raised that did not hold up are listed at the end. Paths are relative to `custom_components/mikrotik_router`. Codex's own output is in the session scratchpad only.

**Tags:** `[SEC]` security, `[BLK]` blocking/concurrency/resource, `[OTH]` other. `*` = file touched by the sub-devices work (entity.py, `__init__.py`, device registration).

No CRITICAL findings.

## HIGH

**H1 `[OTH]` * Router device identity collapses to `N/A` / `unknown`.**
`__init__.py:140,145` (`identifiers`/`connections` = `(DOMAIN, serial-number)`), `entity.py:304-308` (System entities), `coordinator.py:1479` (`serial-number = "N/A"` for CHR/x86; `"unknown"` default at 1487 for hardware that hides the serial).
Scenario: two CHR/x86 routers are added (config_flow distinguishes them by system-id/software-id, `config_flow.py:90-110`). Both register device `(mikrotik_router, N/A)`, so they share one device: name/model/sw_version overwrite each other and all child devices (`parent_device_id=router_device_id`) and System entities hang off one parent. Same for two physical routers whose serial is unreadable.
Fix: build the router device identifier from the validated identity already computed in config_flow (`router_unique_id`, i.e. serial / system-id / software-id / endpoint fallback), or `config_entry.entry_id`, and use the same value in `entity.py`'s System `DeviceInfo`. Never register `N/A`/`unknown`. Migrate existing devices by adding the new identifier (as `_async_migrate_child_devices` already does for children). Coordinate with the sub-devices session: this is exactly the code it is editing.

**H2 `[SEC]` TLS certificate chain is verified but the hostname is not.**
`mikrotikapi.py:134` sets `check_hostname = False` unconditionally; `ssl_context.wrap_socket` is passed bare (140-141), and librouteros calls it without `server_hostname`.
Scenario: "Verify SSL" ticked; an on-path attacker presents any certificate chained to a CA HA trusts for a domain they own. It is accepted, and the router username/password is sent to them and API traffic can be altered.
Fix: when `ssl_verify`, keep `check_hostname=True` and wrap with `lambda sock: ctx.wrap_socket(sock, server_hostname=self._host)`. Offer a CA-bundle / pinned-fingerprint option so self-signed router certs still work without turning verification off. Apply to config_flow validation and runtime (same class).

## MEDIUM

**M1 `[SEC]` Insecure defaults: cleartext login, no verification.**
`const.py:19,25,26` (`login_method=plain`, `DEFAULT_SSL=False`, `DEFAULT_VERIFY_SSL=False`); `config_flow.py:~210` uses them for the form; `mikrotikapi.py:131-141`.
Scenario: setup with defaults sends the password in cleartext on port 8728 at every connect; a user who only ticks "SSL" still gets `CERT_NONE` (139) and is open to an active MITM.
Fix: default new entries to SSL with verification on (or at least verification on once SSL is on), make plaintext/unverified an explicit opt-in with a warning in the form description; leave existing entries as they are on migration.

**M2 `[SEC]` RouterOS environment values leak via diagnostics and debug logs.**
`const.py:66-99` (`TO_REDACT` has no `value`), `coordinator.py:1802-1814` (`get_environment` stores `name`+`value`), `diagnostics.py:~194` (`async_redact_data` is key-based), `apiparser.py:118,138` (debug logging uses the same key set).
Scenario: a script keeps a DDNS/API token in a global; environment sensors are on; the user posts diagnostics or a debug log. Variable `password` still leaks because the secret is under key `value`.
Fix: in diagnostics drop/redact every `environment[*].value`; in `parse_api` debug logging skip or redact the environment path (pass a per-call redaction set). Optionally redact `comment`, `name` fields of scripts/queues too (see coordinator item 26).

**M3 `[BLK]` Unload/reload never closes RouterOS API sessions.**
`__init__.py:262-266` (`async_unload_entry` only unloads platforms); no `.close()`/`disconnect` call exists anywhere (`grep` confirmed; `mikrotikapi.disconnect` just drops the reference, `mikrotikapi.py:92-113`). Extends coordinator item 9, which only mentions config_flow/disconnect internals.
Scenario: every options change triggers `async_reload_entry` (`__init__.py:126`) and leaves two TCP API sessions (main + tracker coordinator) open until GC; repeated reloads accumulate sessions on the router (RouterOS caps concurrent API sessions) and, for SSL, sockets.
Fix: add a `close()` to `MikrotikAPI` (acquire lock, `connection.close()`, clear state) and call it for both coordinators from `async_unload_entry` via the executor, after `async_unload_platforms` succeeds; also close in `validate_input` and on setup failure after the first coordinator refresh succeeded but the tracker's failed (`__init__.py:112-117`).

**M4 `[OTH]` No device_tracker entities at all for accounts without the `test` policy.**
`coordinator.py:153-154` (tracker `_async_update_data` returns `None` without `"test"` access; also `None` when host tracking is off at 150), `entity.py:119` (`coordinator.data is None` -> discovery returns).
Scenario: read-only RouterOS user (common) with "track network hosts" on: ARP ping is impossible but wireless/CAPsMAN/DHCP hosts are known from the main poll. No tracker entity is ever created.
Fix: always return the `{"host", "routerboard"}` snapshot from the tracker coordinator and skip only the ping loop when `test` is missing (or discover hosts from the main coordinator).

**M5 `[OTH]` * Firewall entity unique IDs are content-derived and mutable.**
`entity.py:287` (`unique_id` from `data_reference`), `switch_types.py:214,236,255` (`data_reference="uniq-id"`), `coordinator.py` nat/mangle/filter `uniq-id` combine; data itself is keyed by `.id`. Related to PR 516 note in REVIEW-coordinator.md.
Scenario: edit the dst-port of an existing NAT rule. Its `.id` is unchanged but `uniq-id` is new, so discovery (`entity.py:145-149`) adds a second switch while the old registry entity (whose property now returns the new uid) also stays available; both control the one rule. Each edit adds another.
Fix: key unique ids on the stable RouterOS `.id` (+ entry id), migrating registry unique ids once; keep content only in the name.

**M6 `[OTH]` * Recreated rules stay unavailable until reload.**
`entity.py:145-148` (`known_unique_ids` never pruned), `entity.py:251-252` (`available` = `self._uid in data`, `_uid` is the old `.id`).
Scenario: NAT rule `.id=*1` deleted and re-added (same content, new `.id=*2`): same content-derived unique id, so the new object is skipped, and the old entity looks for `*1` forever -> unavailable. Fixed together with M5 (stable ids), or by rebinding entities to the new key.

**M7 `[OTH]` Netwatch "unknown" status shows as connected.**
`coordinator.py:1458` (`type: bool, default: "unknown"`), `apiparser.py:77-78` (non-bool falls back to the string default), `binary_sensor.py:69` (`is_on` returns it; non-empty string is truthy), icon at 72-77 likewise.
Scenario: disabled/new Netwatch probe -> `status=unknown` -> binary sensor `on` ("connected").
Fix: default `None`, return `bool | None` from `is_on`, treat only up/down as known.

**M8 `[OTH]` Lossy unique-id normalisation drops distinct entities / routers.**
`entity.py:287,289` lowercases the entry name and slugifies the reference; `config_flow.py:151,183` compares names case-sensitively.
Scenario: routers `Office` and `office` both pass the flow but get identical entity unique ids; within a router, scripts/queues `backup-a` and `backup_a` slugify equal and the second is silently dropped.
Fix: use `config_entry.entry_id` (or router unique id) instead of the name in unique ids, and an injective encoding of the reference; migrate. Short-term: case-insensitive name check.

**M9 `[OTH]` Host presence timeout accepts 0, negatives and huge values.**
`config_flow.py:421-427` (`vol.Optional(..): int`), `device_tracker.py:113-117,132,147`.
Scenario: timeout `0`/`-5` -> every wired host is permanently not_home; very large value -> `timedelta` `OverflowError` in the state property, tracker states stop writing.
Fix: `NumberSelector(min=1, max=…)` (like the interval fields at 410ff) and guard stored bad values.

**M10 `[BLK]` Concurrent entity commands can saturate the shared HA executor.**
`entity.py:196`, `mikrotikapi.py:397,435,487`, `button.py:60-63`. Downgraded from Codex HIGH: needs a mass service call, each job is short.
Scenario: a script/automation toggles 30 switches at once: 30 executor jobs, one runs, 29 block on the router `RLock` while holding HA worker threads, delaying other integrations.
Fix: coordinator-owned `asyncio.Lock`/semaphore acquired before `async_add_executor_job` in `async_run_routeros`.

## LOW

**L1 `[OTH]` * `async_remove_config_entry_device` always returns True** (`__init__.py:272-276`). The router device and its children can be deleted from the UI while their entities are live; they are recreated on reload, losing user-assigned areas/names. Fix: refuse for the router device and any device whose entities still exist in the coordinator data; allow only stale ones.

**L2 `[BLK]` Active poll keeps submitting router I/O after unload** (`__init__.py:266`, `coordinator.py:633-719,156-197`). An in-flight refresh continues through the remaining executor jobs after unload; a reload can overlap old and new API sessions. Fix: a `_stopping` flag checked between jobs, set on unload (pairs with M3).

**L3 `[BLK]` PoE/interface toggle can interleave** (`switch.py:166-179,199-206`). Two overlapping on/off commands can end with the port enabled and PoE off because the PoE decision uses cached state and the two API calls are separate executor jobs. Fix: per-port `asyncio.Lock` around the whole operation.

**L4 `[OTH]` Prerelease changelogs skipped or wrong** (`helper.py:50-56`, `update.py:246-258`). `7.20rc2` and `7.20rc3` both normalise to `7.20`, so no notes; stable -> rc fetches the stable CHANGELOG URL. Fix: keep the rc/beta suffix for URL building.

**L5 `[OTH]` Stale extra attributes persist** (`entity.py:343-346`, `sensor.py:99,109`). `_attr_extra_state_attributes` is mutated in place and never cleared, so SFP/wireless attributes that disappear from the data remain. Fix: build a fresh dict per property read.

## Security vs blocking vs other

| Tag | Items |
|---|---|
| Security | H2, M1, M2 |
| Blocking / concurrency / resource | M3, M10, L2, L3 |
| Other | H1, M4, M5, M6, M7, M8, M9, L1, L4, L5 |

Checked, no finding: command injection (script/switch/button values go through structured API args and exact `.id` lookup; `services.yaml` is empty); mac-vendor-lookup uses `https://standards-oui.ieee.org` with normal TLS and a fixed cache path; config-entry password storage is HA standard and diagnostics redact `username`/`password`/`host`; no sync librouteros call, file I/O or sleep on the event loop beyond what REVIEW-coordinator.md items 6/16 already list; no lock-order deadlock found.

## Files touched by the sub-devices change

H1, M3 (the `__init__.py` setup/unload path), M5, M6, M8 (entity.py unique ids) and L1 sit in `entity.py` / `__init__.py` / device registration. Everything else is in coordinator.py, mikrotikapi.py, config_flow.py, binary_sensor.py, diagnostics.py, switch.py, helper.py, update.py.

## Status of REVIEW-coordinator.md items on this branch (checked by grep at f82e022)

| Item | Status |
|---|---|
| 1 stale rows never pruned | fixed: `prune=True` on all path parsers (coordinator.py, 18 sites); `[]` vs `None` not re-verified |
| 2 wireless_hosts reset in place | fixed: `get_wireless_hosts` builds a local dict and assigns it (coordinator.py:2165-2189); item 3 (shared `ds` between loop and executor) not re-verified |
| 4 MultiTrapError reconnects | fixed (mikrotikapi.py:334,351) |
| 5 static lease `bridge != "unknown"` | fixed (coordinator.py:183 uses `!= ""` in tracker; lease branch not re-read) |
| 6 host restore | fixed (`async_get_host_hass` parses slugified MACs) |
| 12 release-note explosion | fixed (`_fetch_minor_changelogs` bounded, sequential, 404-terminated) |
| Issue 515 wifi signal | fixed (coordinator.py:2167-2170) |
| 7 socket timeout vs 10 s check-for-updates | open (no `timeout` in connect kwargs; coordinator.py:1673) |
| 9 `.close()` never called | open (see M3) |
| 17 pytz undeclared / `utcfromtimestamp` | open (apiparser.py:6,21; coordinator.py:10,90; not in manifest.json) |
| 24 `async_refresh` after toggles | open (switch.py:93,106,181,209) |
| 8, 10, 11, 13-16, 18-23, 25-27 | not re-verified |

## Codex claims dropped or downgraded

- Codex general #1 "unrelated routers share one device": kept (H1), I verified `coordinator.py:1479` and `__init__.py:145`.
- Codex blocking #1 rated HIGH: downgraded to MEDIUM (M10); #2 rated MEDIUM: downgraded to LOW (L2).
- Codex security #2 (HIGH) merged into MEDIUM M1: a defaults/UX issue, not an exploitable bug by itself.
- Codex's note that script buttons skip `require_access` was not reported: RouterOS enforces the policy; the failure is surfaced as `routeros_command_failed`.
