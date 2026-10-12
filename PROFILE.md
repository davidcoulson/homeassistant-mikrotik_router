# Performance and update-frequency profile

Branch base: `origin/child-devices` (fork PR #1, `af569a7`). Date of measurement: 2026-10-12.
Scope: how often the integration writes entity states (recorder load) and how many RouterOS API
calls it makes. Security, blocking and coordinator-correctness issues are in `REVIEW-codex.md` and
`REVIEW-coordinator.md` and are not repeated here. No code was changed.

## 1. Method and limits

| Item | What was done |
|---|---|
| Live data | HA MCP read-only: `ha_get_integration`, `ha_search`, `ha_get_history` (24 h, `significant_changes_only=false`), `ha_get_state`. No restart, no config change, no `profiler.start`. |
| Code count | Read `coordinator.py`, `mikrotikapi.py`, `entity.py`, platform files and `*_types.py`, and counted `api.query()` round trips per poll for each option set. |
| CPU profile | `cProfile` on synthetic data (30 interfaces, 100 clients) in a scratch venv: `parse_api` on `/interface` and the entity-discovery loop in `entity.async_add_entities`. HA's real state-write path was not profiled, so per-write cost is not measured. |

Limits you should know about:

- The live install is small. Only one of four config entries is loaded (`djc-sw-edge`, a CRS305-1G-4S+
  running RouterOS 7.24.5). All sensor options (port traffic, client traffic, NAT, mangle, filter, queues,
  scripts, host tracking) are at their defaults, i.e. **off**. So the live data covers the default entity
  set only (8 sensors + 2 update entities). Numbers for the optional entity groups are modelled from the
  code, and labelled as such below.
- The integration's entities only have about 96 minutes of history (14:14 to 15:50 local, 2026-10-12),
  because the entry was reloaded with the PR #1 build. Per-day figures are straight extrapolations
  of that window (x 15). They contain the `unavailable` flaps at 14:15 and 14:21 (reloads).
- The `sensor.djc_edge_sw_*` / `switch.djc_edge_sw_*` entities seen in the search are **not** from this
  integration (attributes `if_index`, `use_hc`, `sample_ts` point to an SNMP-style integration). They are
  excluded from the tables except for the side note in section 2.3.

## 2. Live measurements

### 2.1 State changes per entity (mikrotik_router, 96 min window, 30 s poll = about 192 polls)

| Entity | Changes in window | % of polls | Extrapolated / day | Note |
|---|---:|---:|---:|---|
| `sensor.…_cpu_load` | **116** | 60 % | ~1,740 | Flips 0 ↔ 1 ↔ 2 on an idle switch. About 85 % of changes are a +/-1 point jitter. |
| `sensor.…_cpu_temperature` | **85** | 44 % | ~1,275 | Flips 36 ↔ 37 ↔ 38 (integer degrees). About 80 % are +/-1 °C jitter. |
| `sensor.…_memory_usage` | 6 | 3 % | ~90 | Mostly reload artefacts (13 → 11 → 12). |
| `update.…_routerboard_firmware_update` | 6 | n/a | n/a | Reload artefacts (`unavailable` ↔ `on` ↔ `off`). |
| `update.…_routeros_update` | 5 | n/a | n/a | Reload artefacts. |
| `sensor.…_captive_portal_clients` | 5 | n/a | n/a | Reload artefacts. |
| `sensor.…_wired_clients` | 5 | n/a | n/a | Reload artefacts. |
| `sensor.…_wireless_clients` | 5 | n/a | n/a | Reload artefacts. |
| `sensor.…_hdd_usage` | 5 | n/a | n/a | Reload artefacts. |
| `sensor.…_uptime` | 0 rows in the 24 h query | n/a | ~0 | Already a boot timestamp with a 10 s hysteresis (`coordinator.py:1611-1621`). `last_changed` is 15:36:38 (reload). The empty history is unexplained, perhaps a recorder exclude, and is unverified. |

Totals: about 240 recorded state rows in 96 min, or **~3,600 rows/day**, of which **~84 % are CPU load and
CPU temperature jitter**. Excluding reload artefacts, everything else in the default entity set writes
essentially nothing per day. HA core already drops a write when state and attributes are identical, so
unchanged entities are free at the recorder; the cost is entirely in values that jitter.

### 2.2 Attribute-only writes and uptime

- None of the 10 live entities had volatile attributes (`attribution`, `unit`, `icon` only), and none wrote
  on an attribute change. Not observable on this install, so see the modelled section 4 for the groups that
  do carry volatile attributes (SFP DDM, wireless signal).
- Uptime is already well handled (timestamp, not a counter). No change needed.

### 2.3 Side note: a noisier neighbour

The SNMP-style entities for the same switch (`sensor.djc_edge_sw_*_{rx,tx}_{throughput,total}`) wrote
**1,427 states in 24 h each (one per 60 s)**, 16 sensors, about **22,800 rows/day**, which is more than
six times the whole mikrotik_router entity set. That is another integration, so it is out of scope here,
but if recorder load is the worry this is where most of it is today.

## 3. API calls per poll (steady state)

`/…/path` queries are one network round trip each. `connection.path()` itself is local. All calls are
serialised on one lock per connection. The main poll and the tracker poll use separate connections.

| Group | Calls per main poll | Runs every poll? | Data changes | Option gate |
|---|---:|---|---|---|
| `/system/resource` | 1 | yes | CPU, memory (volatile) | always |
| `/system/health` | 1 | yes | temperatures (volatile) | needs write/policy/reboot |
| `/interface` | 1 | yes | counters (volatile) | always |
| `/interface/ethernet` | 1 | yes | PoE and SFP shutdown temp config, almost never | always |
| `/interface/ethernet` `monitor` | **E** (one per ether port) | yes | link rate, duplex, SFP DDM | always |
| `/interface/bonding` | 0-1 | yes | static | if a bond exists |
| `/ip/dhcp-client` | 1 | yes | almost never | always |
| `/interface/bridge/host` | 1 | yes | client moves | always |
| `/ip/arp` | 1 | yes | client moves | always |
| `/ip/dhcp-server/lease` | 1 (+1 for `dhcp-server` once) | yes | slow | always |
| wireless: `/interface/wifi*` + registration-table | 2 | yes | config static, registrations volatile | if wireless present |
| capsman registration | 1 | yes | volatile | if CAPsMAN |
| `/ip/firewall/nat`, `mangle`, `filter` | 1 each | yes | rule edits only (and counters) | options, default off |
| `/ip/kid-control`, `/tool/netwatch`, `/queue/simple` | 1 each | yes | config, rare | options, default off |
| `/ppp/secret` + `/ppp/active` | 2 | yes | slow | option, default off |
| client traffic (v6 accounting) | 3 (`accounting` ×2, `snapshot/take`, `snapshot`) | yes | volatile | option, default off |
| client traffic (v7 kid-control devices) | 1 | yes | volatile | option, default off |
| `/system/script`, `/system/script/environment` | 0 / 1 | scripts: 4 h; environment: every poll | rare | options, default off |
| UPS, GPS, hotspot | 1-2 each | yes | volatile | if supported |
| hw-info bundle (access, fw check, capabilities, routerboard, scripts, dhcp network, dns, resource) | ~9 | every 4 h or reconnect | static | always |

Worked totals:

| Scenario | Calls per poll | Calls/day at 30 s | Notes |
|---|---:|---:|---|
| **Live CRS305** (default options, E = 5: ether1 + 4 SFP) | **13** | **37,440** | 5 of the 13 are per-port `monitor` calls. |
| Typical RB with 8 ports + wifi, default options | 8 + 8 + 2 = 18 | 51,840 | |
| Same, all optional groups on (nat, mangle, filter, queues, netwatch, accounting, ...) | ~32 | ~92,000 | 11 of the added calls return near-static config. |

Tracker cycle (only when "track network hosts" is on; it is off on the live install, so this is
**modelled, not measured**): one `/ping` per non-wireless host, `count=3` at `interval=100ms`, run
sequentially (`coordinator.py:148-200`). That is about 0.4-0.5 s per host, so 100 hosts take about 40-50 s
per cycle. HA's `DataUpdateCoordinator` does not overlap runs, so the effective interval is the cycle time and
not the configured 10 s. Each cycle then calls `async_process_host` and notifies every tracker entity.

### CPU profile (synthetic, 30 interfaces / 100 clients, this box, Python 3.14)

| Step | Cost | Verdict |
|---|---:|---|
| `parse_api` on `/interface` (30 rows x 16 fields) | 0.22 ms | negligible |
| `async_update_controller` discovery (`entity.py:111-139`): builds one entity object per (description x uid) **on every poll**, 728 objects, then discards them if the unique id is known | **5.3 ms/poll**, 14,560 `slugify` calls per 20 polls | small, but scales with hosts x sensors, and it runs on the event loop for each of 6 platforms and on both coordinators |

CPU is not the bottleneck. The costs that matter are the RouterOS round trips (blocking the executor,
and the router's own CPU), and recorder rows.

## 4. Modelled write load for the optional groups (not measured live)

Formulas assume a 30 s poll (2,880 polls/day) and that every active entity changes every poll.

| Group | Entities | Rows/day (worst case) | Why |
|---|---|---:|---|
| Port traffic sensors | 4 per interface (`tx`, `rx`, `tx-total`, `rx-total`) | 11,500 x ports | Throughput and totals change every poll. 20 ports ≈ 230k rows/day. |
| ... of which SFP / ether attribute churn | the same 4 sensors per SFP port carry DDM attributes (`sfp-temperature`, `sfp-rx-power`, `sfp-tx-power`, `sfp-tx-bias-current`, `sfp-supply-voltage`) | adds a new attribute blob per write | Attribute dict differs every poll, so every state row gets its own ~0.5-1 KB attributes row. |
| Client traffic sensors | 6 per client (lan/wan tx/rx, tx, rx) | 17,300 x clients | 100 clients ≈ 1.7M rows/day. |
| Wireless host trackers | 1 per wireless client | up to 2,880 x clients | `signal-strength`, `tx-ccq`, `tx-rate`, `rx-rate` are attributes (`device_tracker_types.py`) and change nearly every poll. State is stable, but every attribute change is a `state_changed` event. |

## 5. Ranked recommendations

Estimates are against the measured live baseline (13 API calls/poll, ~3,600 rows/day) or the modelled
groups in section 4.

| # | Recommendation | Where | Est. saving | Evidence |
|---|---|---|---|---|
| 1 | **Add hysteresis / rounding to CPU load, CPU temperature and other jittery health values**: publish a new value only if it moved by >= 2 points (or 2 °C) or 5 minutes passed, otherwise keep the previous value. | `get_system_resource`, `get_system_health` | **~80-85 % of live recorder rows** (about -3,000 rows/day on the live install) | 116 and 85 changes in 96 min; most are +/-1. |
| 2 | **Batch per-port `monitor` into one call** (`/interface/ethernet/monitor numbers=<id1,id2,...> once`). | `get_interface` (`coordinator.py:871-960`) | **-(E-1) calls per poll**: 4 of 13 live (-31 %), 7 of ~18 on an 8-port box | Section 3. |
| 3 | **Tiered poll**: a fast tier (`/interface`, `/system/resource`, `/system/health`, batched monitor) every poll; a medium tier (arp, dhcp lease, bridge host, wireless registrations) every 2nd-4th poll; a slow tier (dhcp-client, `/interface/ethernet`, wireless config, nat/mangle/filter/queue/netwatch/kid-control/ppp config) every 5-10 min, plus an immediate refresh after a switch/button action. | `_async_update_data` | live: 13 → ~5 calls/poll average (**-60 %**, ~-22,000 calls/day); full-options router: ~32 → ~12 (**-60 %**) | 11 of the 14 optional calls return near-static config. |
| 4 | **Move SFP DDM and wireless signal out of the entity attributes**: expose them as separate (disabled-by-default, diagnostic) sensors, or at least list them in `_unrecorded_attributes`. Also stop attaching the 12-30 interface attributes to all four traffic sensors; keep them on one entity. | `sensor.py`, `device_tracker_types.py`, `sensor_types.py` | Removes a new attributes row per write on SFP ports (about 1 KB x 11,500 per SFP port-day), and the per-poll `state_changed` for stable wireless clients (up to 2,880/day/client). Note: `_unrecorded_attributes` shrinks attribute storage but does not remove the state row, a separate sensor does. | Section 4. |
| 5 | **Round and throttle traffic rates**: round `tx`/`rx` throughput to the nearest kB/s or Mbit/s (or emit only when |Δ| > 5 %), and let the `*-total` counters use a longer interval (they are `total_increasing`, the statistics engine does not need 30 s). | `get_interface`, `MikrotikInterfaceTrafficSensor` | **-50 to -80 %** of port traffic rows once port traffic is enabled (230k → ~50-115k rows/day for 20 ports). | Section 4. |
| 6 | **Cap the tracker cycle**: ping only hosts whose ARP entry is stale/incomplete or whose DHCP lease is not `bound`; use `count=1`; skip hosts already seen as active in the last cycle; run pings with a bounded concurrency. | `MikrotikTrackerCoordinator._async_update_data` | 100 hosts: ~45 s → ~5-10 s cycle; **-80 % tracker round trips**; the effective interval becomes the configured one. Modelled. | `coordinator.py:148-200`. |
| 7 | **Cache entity discovery**: rebuild only when the key set of a `data_path` changes (compare `set(data)` to the last seen), and construct the object only for unseen uids. | `entity.async_add_entities` | 5.3 ms → ~0.2 ms per poll per platform with 130 devices, and removes 14k `slugify` calls per 20 polls. Event-loop only, no recorder effect. | Section 3 CPU profile. |
| 8 | **Disable noisy entities by default** (`entity_registry_enabled_default=False`): the 4 traffic sensors for non-WAN ports, per-client traffic, SFP DDM sensors. None of the 6 `*_types.py` files set it today. | `*_types.py` | Keeps the modelled groups in section 4 off unless asked for. | `grep` shows 0 uses. |
| 9 | **Skip `async_write_ha_state` when the source dict is unchanged** (compare a cheap per-entity tuple of `data_attribute` + `data_attributes_list` values). | `MikrotikEntity._handle_coordinator_update` | CPU only: avoids building state/attributes for entities whose data did not change. HA core already dedups the write. | HA behaviour, not measured. |
| 10 | Leave **uptime** as is (boot timestamp, 10 s hysteresis already present), and keep the 4-hourly hw-info bundle. | n/a | n/a | `coordinator.py:1611-1621`, `:600-630`. |

Combined effect on the live install, if 1-3 land: about **-85 % recorder rows** and about **-60 % API calls**.
On a full-featured install, 3 + 5 + 6 + 8 dominate.

## 6. Follow-ups worth measuring

- Turn on port traffic + SFP sensors on the CRS305 for 24 h, then re-run the history count to replace the
  modelled numbers in section 4 with measured ones.
- Take a short HA `profiler.start` window with host tracking on to measure the real per-write cost and the
  true tracker cycle time.
- Check why `sensor.…_uptime` returns no history rows although its `last_changed` is inside the window.
