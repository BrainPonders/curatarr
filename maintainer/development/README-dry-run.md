# Ryot/Radarr Dry Run

`maintainer/development/ryot_radarr_dry_run.py` compares live Ryot and Radarr movie sets without changing either system.

`maintainer/development/ryot_collection_add.py` consumes the dry-run CSV outputs and performs additive Ryot collection updates for reviewed cleanup work.

`maintainer/development/ryot_resolve_missing_metadata.py` resolves missing Ryot `metadata_id` values by searching TMDB-backed Ryot metadata and matching on `identifier == tmdb_id`.

`maintainer/development/ryot_radarr_collection_cleanup.py` empties temporary Ryot collections such as `Watchlist` and `Reminders`, then applies the current Radarr movie set to the dedicated `Radarr` collection without touching `Owned` or `Completed`.

`maintainer/development/ryot_owned_audit.py` compares the real `Owned` collection membership against the Ryot movie-view inventory to explain count mismatches such as `Owned > movie view`.

`maintainer/development/ryot_owned_cleanup_dry_run.py` computes a destructive movie-side `Owned` cleanup plan without executing it. The keep rule is: keep `Owned` if the movie is in `Completed`, or has a Radarr file, or is unmonitored in Radarr.

It:

- loads the same `settings.py` format used by Curatarr
- accepts an explicit `--settings-file` so it can point at the runtime config on `RJ-Mediarr`
- queries live Ryot and Radarr
- writes `summary.json` plus review CSVs to an output directory
- supports `--manual-watch-csv` so reviewed watch overrides are folded into the preview

Example:

```bash
python3 maintainer/development/ryot_radarr_dry_run.py \
  --settings-file /home/mediarr/arr-stack/curatarr/settings.py \
  --output-dir /home/mediarr/arr-stack/curatarr/.local/dry-run-ryot-radarr
```

Notes:

- The dry-run script enumerates Ryot movies through `userMetadataList`, not `metadataSearch`.
- This is a dry run only. It does not change Ryot collections or Radarr entries.

Additive collection update example:

```bash
python3 maintainer/development/ryot_collection_add.py \
  --settings-file /home/mediarr/arr-stack/curatarr/config/settings.py \
  --owned-csv /home/mediarr/arr-stack/curatarr/dry-run-output/owned_to_add.full.csv \
  --reminders-csv /home/mediarr/arr-stack/curatarr/dry-run-output/reminders_candidates.full.csv
```

Notes for the updater:

- It is additive only. It does not remove items from any Ryot collection.
- It skips CSV rows without `metadata_id` and reports those counts separately.
- Add `--execute` only after reviewing the JSON plan it prints by default.
- For arbitrary collections such as `Watchlist`, use `--collection-input Watchlist=/path/to/file.csv`.

Resolver example:

```bash
python3 maintainer/development/ryot_resolve_missing_metadata.py \
  --settings-file /home/mediarr/arr-stack/curatarr/config/settings.py \
  --input-csv /home/mediarr/arr-stack/curatarr/dry-run-output/owned_to_add.full.csv \
  --output-dir /home/mediarr/arr-stack/curatarr/dry-run-output/resolve-missing
```

Resolver notes:

- It only inspects rows where `metadata_id` is empty.
- It searches Ryot by title and confirms candidates via `metadataDetails`.
- `resolved.full.csv` is safe to use as the next review surface before another additive `Owned` run.

Cleanup helper example:

```bash
python3 maintainer/development/ryot_radarr_collection_cleanup.py \
  --settings-file /home/mediarr/arr-stack/curatarr/config/settings.py
```

Cleanup helper notes:

- Default temporary collections are `Watchlist` and `Reminders`.
- It empties only the named temporary collections.
- It then adds the current Radarr set to the Ryot `Radarr` collection.
- It does not touch `Owned` or `Completed`.
- Add `--execute` only after reviewing the JSON plan it prints by default.

Owned audit example:

```bash
python3 maintainer/development/ryot_owned_audit.py \
  --settings-file /home/mediarr/arr-stack/curatarr/config/settings.py \
  --output-dir /home/mediarr/arr-stack/curatarr/dry-run-output/owned-audit
```

Owned audit notes:

- It reads the actual `Owned` collection contents from Ryot.
- It compares those members against the Ryot movie-view inventory.
- It writes CSVs for `Owned` entries missing from movie view and for duplicate TMDB ids inside `Owned`.

Owned cleanup dry run example:

```bash
python3 maintainer/development/ryot_owned_cleanup_dry_run.py \
  --settings-file /home/mediarr/arr-stack/curatarr/config/settings.py \
  --output-dir /home/mediarr/arr-stack/curatarr/dry-run-output/owned-cleanup
```

Owned cleanup dry run notes:

- It does not change Ryot.
- It only evaluates movie members of `Owned`.
- It writes `owned_keep` and `owned_remove` CSVs for review before any destructive follow-up step.
