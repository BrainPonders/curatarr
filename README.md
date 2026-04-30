# Curatarr

Telegram-first media request and library management assistant for Ryot, Radarr, Sonarr, and Jellyfin.

Curatarr is a new project derived from lessons learned in the Searcharr lineage, but it is not a continuation of the old Searcharr runtime. The current repository baseline is documentation-first: it preserves the Curatarr architecture, maintainer workflow, and deployment structure while runtime implementation is rebuilt around the Curatarr model.

## Product Direction

Curatarr treats Ryot as the durable source of truth for watched history, archived titles, ownership, and active media intent. Radarr and Sonarr remain execution systems for acquisition, monitoring, profiles, quality, and files. Jellyfin is used as a playback and reception signal where applicable.

Curatarr's job is to orchestrate:

- search, add, and reactivation flows
- already-watched warnings
- post-watch keep/archive decisions
- cleanup and missing-file decisions
- Ryot, Radarr, Sonarr, and Jellyfin reconciliation
- admin-facing operational issues

See [documentation/System Architecture.md](documentation/System%20Architecture.md) for the current doctrine.

## Repository State

This repository currently contains the Curatarr planning baseline plus the first runtime foundation: package identity, CLI bootstrap, and YAML configuration loading.

Current retained surfaces:

- `documentation/` contains architecture, governance, and lineage/reference material.
- `src/curatarr/` contains the new Curatarr runtime package.
- `config.yaml.example` contains the initial YAML configuration template.
- `tests/` contains focused runtime foundation tests.
- `maintainer/` contains maintainer workflow, development, release, Docker, and test scaffolding.
- `docker/compose/` contains deployment example structure to be updated as the runtime takes shape.
- `.local/` is the ignored local-only safety bucket and must not be committed.

## Implementation Direction

The intended build order is:

1. Define Curatarr's adapter and workflow boundaries.
2. Add a YAML configuration model. Initial loader is present.
3. Add domain primitives for media identity, durable state, runtime state, and match confidence. Initial model is present.
4. Define adapter interfaces for Ryot, Radarr, Sonarr, Jellyfin, Telegram, and metadata. Initial protocols are present.
5. Add read-only Ryot durable-state mapping. Initial GraphQL client and adapter are present.
6. Introduce Curatarr database tables for workflow state, identity mappings, pending decisions, approvals, operational issues, jobs, and audit log. Initial SQLite schema is present.
7. Add identity matching over exact identifiers, confirmed mappings, metadata cross-resolution, and title/year fallback. Initial service is present.
8. Rebuild remaining integrations behind adapters for Radarr, Sonarr, Jellyfin, Telegram, and metadata.
9. Implement Telegram workflows and background reconciliation jobs.

## Local Bootstrap

Install the package in a virtual environment:

```bash
python -m pip install .
```

Check a configuration file:

```bash
curatarr --config config.yaml --check-config
```

The runtime workflows are not implemented yet; the CLI currently validates configuration and reports bootstrap status.

Searcharr code may be consulted as reference only. New Curatarr runtime code should follow the architecture in `documentation/`.

## Maintainer Notes

The Docker and release helpers are scaffold-level until real Curatarr workflows exist. Do not publish production releases from this repository until the runtime contract has been re-established.

## Reference Lineage

Curatarr was informed by Searcharr and Searcharr-nxg experiments, but the project is now owned as a standalone architecture and implementation.
