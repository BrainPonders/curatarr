# DEVELOPMENT

Maintainer guide for local scaffold rebuilds and runtime bootstrap preparation.

## Purpose

- build a local scaffold container image from the current working tree
- prepare a separate dev runtime folder outside the repository
- preserve local `.env` and compose files instead of overwriting them

## Script Boundary

`maintainer/development/dev-build.sh` is a template helper. Review and adapt these assumptions before relying on it:

- the repository root is resolved from `../..`
- the Dockerfile lives at `maintainer/docker/Dockerfile`
- the runtime folder defaults to `<repo-parent>/<project-slug>-dev/`
- the runtime env file uses `PROJECT_IMAGE=` as the image reference key
- this is a scaffold helper until the Curatarr runtime exists

## Runtime Layout

Default runtime folder:

- `<repo-parent>/<project-slug>-dev/`

Default runtime files:

- `docker-compose.yml`
- `.env`
- `data/`

Template sources:

- `docker/compose/docker-compose-dev.yml.example`
- `docker/compose/.env.dev.example`

## First Run

1. Review `docker/compose/docker-compose-dev.yml.example` and `docker/compose/.env.dev.example`.
2. Run `bash maintainer/development/dev-build.sh` to build the scaffold image and prepare the external runtime folder.
3. Edit the generated `.env` before starting the dev container for scaffold checks.
4. Replace this flow when the real Curatarr runtime exists.

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
- The script should not write secrets into the repository.
- The script is only a starting point and should be tightened per project.
