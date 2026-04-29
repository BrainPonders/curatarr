# Maintainer Overview

This folder is the tracked maintainer surface for Curatarr.

## Index

- `maintainer/WORKFLOW.md`
  - authoritative maintainer lifecycle for this repository
- `maintainer/development/README.md`
  - local scaffold and container-bootstrap workflow
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
