# Maintainer Overview

This folder is the tracked maintainer surface for Curatarr.

The repository supports both product development and deployment/commissioning/database-correction work. Keep reviewed execution procedures here; keep credentials, actual installation mappings, backups, reports and private chat handovers in the ignored local safety bucket. Routine Mini App maintenance is not the interface for commissioning batches or complex technical repairs.

## Index

- [Feature register](FEATURES.md): current scope, implementation, availability and test/gap links
- [Roadmap](ROADMAP.md): W01–W09 delivery order, exit gates and DATA tasks
- [Decisions](../documentation/Decisions.md): approved changes and superseded choices

- `maintainer/WORKFLOW.md`
  - authoritative lifecycle, change control, user acceptance and handover checklist
- `maintainer/development/README.md`
  - runtime verification, deployment/bootstrap, and controlled database-correction procedure
- `maintainer/development/README-dry-run.md`
  - live Ryot/Radarr dry-run comparison helper usage
- `maintainer/development/dev-build.sh`
  - scaffold image build and dev-runtime preparation helper
- `maintainer/development/ryot_radarr_dry_run.py`
  - dry-run helper for comparing live Ryot and Radarr movie sets using the runtime settings.py
- `maintainer/release/README.md`
  - release expectations and current limitations
- `maintainer/release/release.sh`
  - local release-image build helper
- `maintainer/release/update_available_versions.py`
  - helper for the optional release table in `README.md`
- `maintainer/docker/`
  - tracked container build assets for the scaffold runtime placeholder
- `maintainer/test/`
  - maintainer-facing smoke checks
- `documentation/Documentation Governance Principles.md`
  - tracked documentation-governance rules

## Boundaries

- Keep this folder concise, tracked, and project-specific.
- Keep personal narrative runbooks, experiments, and private templates in `.local/`.
- Keep product architecture in `documentation/`, not here.
- Treat the included scripts and container assets as the supported maintainer baseline for the current scaffold, not as a promise that the product is production-ready.
