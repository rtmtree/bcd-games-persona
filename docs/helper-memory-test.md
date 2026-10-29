# Testing helper memory on a device

Helper memory (`mempool-mb`, the ml1150 pool in `build/ntdll-unix/virtual_ios.c`) holds game data in memory that BCD's
helper extension created and left behind. iOS bills that memory to the exited extension, not to BCD, so it does not
count against BCD's limit (6.65 GB on a 12 GB iPhone Air). It is still real RAM: it can only use what the phone has
free, which is realistically 2–3 GB on that phone while a game runs.

The fixes in this branch have only run in the host test (`tests/host/check-helper-memory.py`). A device run decides
whether they work.

## What changed, and what each fix looks like in the log

Build 622 registered helper memory (`8 x 512 MB registered, extension exited`) and then took nothing:
`0 takes, 178 guarded` in Persona 3 Reload until iOS closed BCD.

| Fix | Line to look for |
|---|---|
| The guard counts free **and** file-backed pages, as iOS does before closing apps; it used free pages alone, always under its 1 GB margin | `takes stop below 1024 MB available (now N MB)` |
| BCD's own limit warning, at about 80% of the limit, no longer shuts helper memory; only the system's critical pressure does | `memory-pressure source with BCD's own limit events apart`, later `BCD is at its own limit's warning mark; the pool goes on taking` |
| `MADEIRA_POOL_MARGIN_MB` set in bcd.cfg now takes effect | the margin in the line above |
| A block can span several 512 MB regions | large `backed` lines with offsets that carry the helper-memory flag (`file off 8796093022…`) |
| Asked before the swap file's disk cap | nothing refused while the pool has room |
| A decommit inside helper memory stays there, zeroed | `decommit … zeroed in place` |
| Freed space is merged, and never reused if it could not be zeroed | `zeroing … failed; that space is not used again` (should not appear) |
| Works without a swap file | `helper memory N MB without swap-mb: the tier runs with a 64 MB file` |
| Settings › Memory & performance › This device shows what it adds | the row **Helper memory adds: Up to N GB** |

## Build

Build a BCD that includes this branch. If the game still crashes at start on build 622, also include the crypt32 root
certificate fix (`fix/crypt32-ios-root-enum`). Persona 3 Reload has been dying 13–14 s in, in its copy protection, since
2026-10-29 11:25 with any settings. That is a separate problem, and helper memory cannot be tested in that game until it starts.

## Settings

Settings › Memory & performance:

| Row | Set to |
|---|---|
| Helper memory | 4 GB |
| Swap file | 4 GB |
| What can move | Whole reservations (4 MB and up) |
| Video memory | 1 GB |

Settings › All settings, for Persona 3 Reload (its 30 GPU buffers of 16 MB, 480 MB, go to the swap file):

| Option | Value |
|---|---|
| `default-buffer-swap` | 1 |
| `default-buffer-swap-mb` | 16 |

Leave `swap-min-mb` and `swap-min-kb` at Standard. Restart BCD: swipe it away and open it again.

## Run and read the log

Play to where the game was closed before. Then, on the Mac, with the phone on the same network:

```bash
xcrun devicectl device copy from --device 00008150-0016651634F8401C --domain-type appDataContainer --domain-identifier com.thebcd69.bcd --source Documents/bcd-log.prev.txt --destination bcd-log.txt
```

Use `bcd-log.txt` as the source while BCD is still open on the same run, `bcd-log.prev.txt` after it was closed and
reopened. Then:

```bash
grep -a -E "ml1150 pool|\[session\] length|dock-game-end|^\[footprint\] rev=ml358" bcd-log.txt | tail -40
```

**It works** when:

- the stats line `[swap] ml1150 pool: X MB in use (peak Y), N takes, M guarded, …` shows takes and MB in use that grow
  while the game plays;
- the `[footprint]` lines (what iOS counts for BCD) stay below the old 6.6 GB for longer than before. In the old run they
  went from 5.3 GB to 6.6 GB in about 4 minutes;
- `[session] length=…` is longer than 7 minutes.

**Report these** if they show up:

- `guard closed (N MB available …)`: the phone ran low; N says how low. Normal near the end, a problem early on.
- `zeroing … failed`: MADV_ZERO does not work on helper memory on this iOS. Freed space is then not reused (safe), and
  decommits fall back to the old behaviour.
- other apps on the phone being closed, or the whole phone slowing down: helper memory took more than the phone had
  spare. In All settings set **Helper memory: keep this much free (MB)** (`env.POOL_MARGIN_MB`; bcd.cfg
  `env.MADEIRA_POOL_MARGIN_MB`) to 2048.

## Optional: small allocations too

Most of P3R's memory sat in reservations under 4 MB (4.4 GB of reservations, 3.8 GB of commits), which neither the swap
file nor helper memory takes. To let helper memory take them, from 1 MB, add in All settings:

| Option | Value |
|---|---|
| **Helper memory: smallest reservation (KB)** (`env.POOL_MIN_KB`; bcd.cfg `env.MADEIRA_POOL_MIN_KB`) | 1024 |

It stays off by default until a device run shows it is safe. With it on, the log names them
`(whole reservation, helper memory only)`, and the stats line counts `N reservations under the file's floor (M MB)`.
When helper memory cannot take one, it stays in BCD's own memory; it never goes to the swap file.
