# Review 2: regressions and new defects on origin/master 1e9ab76 (PRs #1-#4)

Method: three read-only Codex scans (allowed_changes + security; data correctness; concurrency/lifecycle), each finding then re-checked against the source by me, plus my own read of `mikrotikapi.py` guard code, `__init__.py` identifier/orphan code and the throughput/hysteresis/ethernet code. Paths relative to `custom_components/mikrotik_router`. Tags: `[SEC]` security, `[BLK]` blocking, `[REG]` regression from PRs #1-#4, `[OTH]` other. `LIVE` = can affect the read-only CRS305 setup.

No CRITICAL findings. Not found: blocking calls on the event loop (all RouterOS I/O still goes through executor jobs), TLS/diagnostics/log-redaction regressions (hostname check, environment redaction still present), injection via entity/service values.

## HIGH

**H1 `[REG][SEC]` LIVE: the removed `read_only` option now means "all changes allowed".**
`helper.py:62-67` (`allowed_changes` returns `ALL_CHANGES` when `allowed_changes` is absent), `config_flow.py:177-178` (VERSION 3 / MINOR 3, unchanged), `__init__.py:638-690` (`async_migrate_entry` has no step for it). `grep read_only` finds nothing in the code: commit 2314893 dropped the option without migrating it.
Scenario: an entry created on the earlier read-only build (aa9de32, options `{"read_only": true}`) upgrades. Both coordinator APIs get all eight categories, switches/buttons/update-install reappear, and the firmware poll runs `check-for-updates` when the account has write+policy+reboot. The user's "no changes" choice is silently gone. A RouterOS account that is itself read-only still rejects writes, so on the CRS305 the exposure is the entities returning plus failed commands, not a changed router, but it is only safe because of that account.
Fix: bump MINOR to 4 and migrate `read_only: True` -> `allowed_changes: []` (False -> full list), keep an existing granular selection, pop the legacy key.

**H2 `[REG]` Recovering a real serial re-parents children under a new router device and orphan cleanup can then delete live devices.**
`__init__.py:264-316` (`_async_router_identifier` returns `(DOMAIN, serial)` as soon as the serial is valid, never touching an existing placeholder device keyed `(DOMAIN, entry_id)`), `entity.py:377` (children keep identifier `entry-interface-x` but now name the new parent), `__init__.py:440-462` (`_async_remove_orphaned_devices` only counts devices that directly own entities).
Scenario: a board whose first poll returned `unknown`/`N/A` (placeholder device P1, children C1..) later returns a real serial. Setup creates P2; HA refuses to re-parent existing children, so port entities fail to register; P1 has no direct entities so cleanup removes it and, with it, its child devices and their registry entities (areas, customised names, entity_ids, history link). Codex reproduced this only through isolated calls of the identifier function; I confirmed the missing branch and the cleanup predicate by reading, not HA's reparent behaviour. The trigger is rare (placeholder -> serial transition) and does not apply to the CRS305, which reports its serial.
Fix: look for the entry's existing router device first and, when it carries a placeholder identifier, `async_update_device(new_identifiers={serial identifier})` in place (as the opposite branch already does); in cleanup, never remove a device that is the parent of a retained device.

## MEDIUM

**M1 `[SEC]` Absolute command names bypass the path guard.** `mikrotikapi.py:408-409,519,551`. The guard authorises the base `path` only; librouteros joins with `posixpath.join`, so `execute("/system", "/interface/set", None, None, {".id": "*1", "disabled": True})` passes as "firmware" and sends `/interface/set`. Verified by Codex with a fake transport; no current caller passes an absolute command (all are fixed strings), so it is a latent hole. Fix: reject commands starting with `/` or containing `..` before dispatch.

**M2 `[SEC]` `query(return_list=False)` hands out a raw, unguarded librouteros Path.** `mikrotikapi.py:432-473`. With `allowed_changes=[]`, `api.query("/interface", return_list=False).update(...)` writes without `_require_change_allowed`. `set_value/execute/run_script/arp_ping/take_client_traffic_snapshot` use it internally and only they guard themselves, so any future caller gets a write handle. Fix: keep raw Paths private (`_path()`), make public `query` return data only.

**M3 `[SEC]` Malformed `allowed_changes` fails open.** `helper.py:65-66`: `None` or a string yields all categories (a test even pins the string case). Fix: treat only an absent key as "all"; invalid type -> empty set; validate members.

**M4 `[OTH]` Client-traffic rate is inflated after a missed sample.** `coordinator.py:2938-2969`, `mikrotikapi.py:699-720`. The shared client-traffic clock advances when a device is absent from a reply or the reply is empty, while per-device baselines do not: device at 1000 B (t=0), empty reply (t=60), 13000 B (t=120) -> 200 B/s instead of 100. Fix: per-device (bytes, timestamp) baselines updated only on a valid sample.

**M5 `[OTH]` New client gets a lifetime-counter spike.** `coordinator.py:2908,2958-2969`. Baselines start at 0 and the first-sample suppression is global, so a client discovered after the clock is initialised publishes its whole lifetime bytes as one interval's rate. Fix: baseline `None`, publish unknown until two samples. (Interface throughput itself is sound: `_counter_delta` handles reset/wrap and the first window publishes `None`.)

**M6 `[OTH]` Last-link-change timestamps use HA's time zone.** `coordinator.py:226-240` (`_parse_routeros_time` assumes HA's zone for RouterOS local time), consumed at 1143-1150. Router and HA in different zones shift the sensor by the offset. Fix: read `/system/clock` time-zone-name in the slow tier and convert; unknown if unreadable.

**M7 `[OTH]` LIVE: missing SFP temperature is published as 0 °C.** `coordinator.py:99,1129,1267`, `sensor_types.py:766`, `sensor.py:156`. `sfp-temperature` defaults to 0 in the monitor vals, and `sfp-shutdown-temperature` defaults to numeric 0, which is `!= ""`, so even copper ports are classed as SFP. A CRS305 cage with the module removed (or a copper port) shows 0 °C in history. Fix: default `None`; classify SFP from `sfp-module-present`/a real field, and report unknown without DDM.

**M8 `[REG]` Tracker keeps cached wireless/CAPsMAN hosts "home" while the router is unreachable.** `coordinator.py:346` (tracker always reprocesses main data), `2554-2555` and the CAPsMAN equivalent (sets `available=True`, `last-seen=utcnow()` from cached rows), `device_tracker.py:124`. Router down for an hour: the tracker, returning success every 10 s, refreshes `last-seen` forever. Fix: only stamp last-seen from a fresh registration read; skip when `coordinator.last_update_success` is False or apply a freshness deadline. Unlikely on the CRS305 (no wireless).

**M9 `[REG]` LIVE if host tracking is on: without the `test` policy wired hosts are permanently `not_home`.** `coordinator.py:329-346,2575,2617`; `device_tracker.py:127`. Since the REVIEW-codex M4 fix entities are created, but presence for non-wireless hosts only comes from ping; the DHCP merge never sets `last-seen`, so a connected, leased host is not_home forever. A read-only account has no `test`. Fix: mark wired trackers unavailable when no presence evidence exists (or derive presence from ARP/bridge-host reachability).

## LOW

**L1 `[OTH]` Batched monitor falls back permanently on any trap.** `coordinator.py:1295-1330`. One refused batch (e.g. a port `.id` removed between polls) sets `_monitor_batch=False` until reload; the per-port calls still return correct data, so it is a performance loss only. Fix: retry batching in the next slow tier or disable only after N consecutive refusals.

**L2 `[OTH]` Allowed-changes cleanup removes registry entities.** `__init__.py:420-430`. Switching a category off and on again drops the switch/button registry entries, losing entity_id customisations and the history association. Fix: disable (`disabled_by=INTEGRATION`) instead of remove.

**L3 `[OTH]` Hysteresis delays alert thresholds.** `coordinator.py:170-171,1835-1865`. Temperatures/CPU hold within 2 units for up to 5 min; an automation on a threshold can lag by that. Document it, or use a smaller per-sensor delta (0.5 for temperatures). `_published` is never pruned for removed sensors (cosmetic).

## Tags

| Tag | Items |
|---|---|
| Security | H1, M1, M2, M3 |
| Blocking | none found |
| Regression | H1, H2, M8, M9 |
| Other | M4, M5, M6, M7, L1, L2, L3 |

## Affecting the live read-only CRS305

H1 (legacy `read_only` option no longer honoured; only safe if the RouterOS user is itself read-only), M7 (SFP temperature 0 °C when a cage is empty and on copper ports), M9 (only if host tracking is on), M2/M3 as defence in depth for the read-only guarantee. H2 does not apply: the CRS305 reports its serial.

## Checked and dropped

- Normal write paths (`set_value`, `execute`, `run_script`, accounting snapshot, firmware check) all call `_require_change_allowed` before any I/O and are fail-closed for unlisted paths when not all categories are allowed.
- `arp_ping` and `/ping` are not writes; leaving them outside the guard is intentional (they need `test`).
- Interface rate math: counter reset/wrap, first window and failed-poll gaps are handled.
- A separate "reboot" category was not raised: `/system` reboot sits under `firmware`, which already gates update install.
