# DEVELOPMENT

Maintainer guide for local runtime foundation checks and container bootstrap preparation.

## Purpose

- bootstrap the Python package locally
- build a local container image from the current working tree
- prepare a separate dev runtime folder outside the repository
- preserve local `.env` and compose files instead of overwriting them

## Script Boundary

`maintainer/development/dev-build.sh` is a template helper. Review and adapt these assumptions before relying on it:

- the repository root is resolved from `../..`
- the Dockerfile lives at `maintainer/docker/Dockerfile`
- the runtime folder defaults to `<repo-parent>/<project-slug>-dev/`
- the runtime env file uses `PROJECT_IMAGE=` as the image reference key
- the runtime currently supports config validation only

## Runtime Layout

Default runtime folder:

- `<repo-parent>/<project-slug>-dev/`

Default runtime files:

- `docker-compose.yml`
- `.env`
- `config.yaml`
- `data/`

Template sources:

- `docker/compose/docker-compose-dev.yml.example`
- `docker/compose/.env.dev.example`
- `config.yaml.example`

## First Run

1. Create a local virtual environment if needed.
2. Install the package with `pip install .`.
3. Copy `config.yaml.example` to `config.yaml` outside the repository or into a local ignored runtime folder.
4. Run `curatarr --config /path/to/config.yaml --check-config` to validate bootstrap-level wiring.
5. Review `docker/compose/docker-compose-dev.yml.example` and `docker/compose/.env.dev.example`.
6. Run `bash maintainer/development/dev-build.sh` to build the image and prepare the external runtime folder.
7. Edit the generated runtime `config.yaml` and `.env` before starting the dev container for real work.

## Useful Environment Variables

- `PROJECT_SLUG`
- `PROJECT_IMAGE_NAME`
- `PROJECT_DEV_PROJECT`
- `PROJECT_DEV_ROOT`
- `PROJECT_DOCKERFILE_PATH`
- `PROJECT_DEV_UP`

## Safety Rules

- The script should not overwrite an existing runtime `.env`.
- The script should not overwrite an existing runtime compose file.
- The script should not overwrite an existing runtime `config.yaml`.
- The script should not write secrets into the repository.
- The script is only a starting point and should be tightened per project.
