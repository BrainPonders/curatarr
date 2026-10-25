# RELEASING

Maintainer guide for preparing and publishing project releases.

## Purpose

- keep the tracked release process concise
- separate local release helpers from project policy
- make it explicit which parts of the template must be customized

## Included Helpers

- `maintainer/release/release.sh`
  - builds local release-tagged container images
- `maintainer/release/update_available_versions.py`
  - optional helper for the release table in `README.md` and the image example in `docker/compose/.env.example`

Both helpers are templates. Review registry names, tag policy, release channels, and publication rules before using them in a real project.

## Current Project State

Curatarr does not currently have a production runtime or active publication workflow.

The repository also serves deployment, commissioning and database-correction work. A working Mac test bot/Mini App, passing unit tests, or an approved commissioning repair does not mean a v1.0 release or server deployment has been completed. Follow the development guide for those operations, then use this guide for release approval. Provider correction permissions and release/deployment permissions are separate.

- do not publish images until the Curatarr runtime contract exists
- do not create release tags until release policy is re-established
- keep local release helpers as scaffold material only
- recreate GitHub Actions release automation after runtime tests and image behavior are real

## Coordinated Movie Release Readiness

The product target is now specified, but no coordinated runtime release or server cutover has been completed. Use [Build Specification AC01–AC20](../../documentation/System%20Architecture.md#acceptance-catalogue) and [Roadmap W01–W09](../ROADMAP.md#coordinated-release-implementation-plan) for acceptance. Use the current evidence in the feature register/development guide; historical test counts do not prove release acceptance. Require current full-suite/browser/container evidence, migration/restore rehearsal, safe historical IMDb/watched commissioning previews and explicitly scoped live acceptance before claiming readiness. One assembled release may be built through internal tested increments; no release version, tag, registry publication or final Telegram-group move is implied by the specification.

Track remaining historical data work in [Database sanitising and historical reconciliation](../ROADMAP.md#database-sanitising-and-historical-reconciliation): DATA01 appends attributable additional Jellyfin watch occurrences, including already-watched Ryot movies, after native creation/recovery is proven (D007); DATA02 validates final consistency and watched parity. A zero-finding service report alone does not establish that historical watches have been imported.

## Local Preflight

1. Rebuild locally.
2. Run maintainer smoke checks.
3. Confirm the container, compose examples, and public docs still match the actual project.
4. Confirm the release tag and publish target match repository policy.

## Common Adaptation Points

- image name and registry
- release tag model
- CI workflow names
- public release table format
- immutable tag policy

## Useful Environment Variables

- `PROJECT_IMAGE_NAME`
- `PROJECT_IMAGE_TAG`
- `PROJECT_VERSION`
- `PROJECT_BUILD_NUMBER`
- `PROJECT_GIT_SHA`
- `PROJECT_IMAGE_REPO`

## Safety Rules

- Do not assume the default tag parser matches the real project.
- Do not publish `latest` unless the project explicitly chooses that policy.
- Keep project-specific release rules here, not in `.local/`.

Before activating the W05 viewing increment, rehearse schema4→5 on a consistent copy, preserve the paired runtime/database and verify selected household accounts plus actual Default webhook delivery. Pause the native Ryot Jellyfin integration before enabling Curatarr intake. Complete genuine-event native-create/read-back commissioning; offline fixtures and a zero-finding monitor do not satisfy that acceptance gate. The latest recorded Mac development deployment uses schema5; direct intake remains off pending setup and native commissioning. See [coordinator staging](../development/README.md#coordinator-staging).

## External Team Handover Gate

A handover is ready only when another team can reproduce the identified baseline without the private chat:

1. Review all modified/untracked project work for inclusion in an explicitly authorized Git baseline. Preserve every existing file; never add the private safety bucket or local policy files. Check tracked/staged paths and new dependency/link references before any commit/push. D011 now authorizes the development baseline commit and synchronization; later unrelated publication still needs its own scope.
2. Record the exact source revision, runtime/schema compatibility, release limitations, unfinished work and approved next action. A source archive may identify a dirty deployment, but does not replace a clean versioned baseline for external collaboration.
3. In a fresh checkout, install documented public dependencies and run record/unit/browser checks from the development guide. Retain dated sanitized results and a source identifier. No `.local/` prerequisite is permitted; installation credentials are supplied separately through the operational handoff.
4. Reconcile F, W, AC, U and DATA records. Implemented/offline-tested/deployed/native-verified/user-accepted are distinct; unanswered acceptance remains pending.
5. For deployment readiness additionally prove the actual image, graceful ownership/shutdown, durable storage, schema migration, restore and supported provider contracts. Inspect unknown native effects before rollback/replay. Do not claim Docker readiness from the scaffold smoke script.
6. Transfer operational secrets and private backups through the agreed private channel, without embedding them in public docs or test fixtures. Confirm one active writer per fact and explicit authorization before live cutover, list retirement, imports or final group move.

Development handover validation is in progress under D011; its source revision and clean-checkout/CI results are owned by the development guide. Production release remains gated by the combined Docker runtime and scoped native commissioning/user acceptance. See F24–F26 and DATA01–DATA02.
