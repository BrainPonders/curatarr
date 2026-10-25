# Curatarr

Telegram-first media request and library management assistant for Ryot, Radarr, Sonarr, and Jellyfin.

Curatarr is a new project derived from lessons learned in the Searcharr lineage, but it is not a continuation of the old Searcharr runtime. The workspace contains an active Mac development application. The coordinated movie release is incomplete; the feature register distinguishes source implementation, local availability and remaining acceptance.

## Development Overview

- [Feature register](maintainer/FEATURES.md): complete current feature inventory and implementation gaps.
- [Roadmap](maintainer/ROADMAP.md): delivery order, release gates and historical-data work.
- [Decisions](documentation/Decisions.md): approved changes and superseded choices.
- [Workflow and user acceptance](maintainer/WORKFLOW.md): process and your test reports.
- [Reproducible checks](maintainer/development/README.md#reproducible-project-checks): synthetic tests and handover limits.

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

## Deployment And Database Correction

This repository is also the working home for Curatarr deployment, commissioning, and controlled database correction across the connected services. Routine household follow-up belongs in Telegram and the Mini App; complex identity repairs and commissioning batches belong in reviewed maintainer operations outside the Mini App. This role is not permission to make unreviewed provider changes or treat deployment scaffolding as production-ready.

For a new development chat, start with [maintainer/WORKFLOW.md](maintainer/WORKFLOW.md#resuming-in-a-new-chat), then follow the authoritative documentation linked there. The [development guide](maintainer/development/README.md#deployment-and-database-correction) owns deployment/correction procedures; the [release guide](maintainer/release/README.md) owns release readiness. Preserve uncommitted work and local deployment state when resuming.

## Repository State

This repository contains the Curatarr backend, Telegram movie search/decisions, Mini App browsing and household viewing answers. Admin Sync issues now shows a compact System check: maintenance needed Yes/No, last checked, and Check now. Incomplete or stale checks cannot report a clean result. A dedicated bot worker checks daily; failures retry hourly. Declined viewings retains household confirmation and Review again; technical repairs are reserved for maintainers.

The coordinated movie release is now in development on `codex/coordinator-movie-release`. Shared journal/recovery, Catalogue only / Add and search, Mini App Suggestions, source Settings and wanted-request handling are implemented behind the default-off `coordinator.enabled` flag. Discovery collects daily when enabled; notifications remain off. IMDb uses complete CSV snapshots with a protected historical first import, followed by eligible new-entry execution through the shared journal. Direct Jellyfin viewing now has an opt-in, offline-tested implementation; native commissioning, unified retention, optional source digest/chat presentation and dedicated Docker cutover remain incomplete. The Mac development runtime was upgraded at the user's request on 2026-10-24 with coordinator controls enabled and individual source policies off; this is not the final release/cutover. See [coordinator staging](maintainer/development/README.md#coordinator-staging) before running this development version: current source initializes schema5 even when the writer flag is off; the latest recorded Mac deployment uses schema5. This is a dated deployment observation, not a fresh health check.

All eight detailed library categories, their persisted findings, skip/filter progress and the existing acquisition correction are retained, hidden from the normal Mini App. Maintainers can restore that diagnostic UI with `maintenance.show_diagnostics: true`; the default is false. `--reconcile-movies` retains its Ryot/Radarr report, while `--check-maintenance` runs the broader read-only movie ecosystem checks and prints private diagnostics. There is no automatic repair or database cleanup. See the [development guide](maintainer/development/README.md#system-maintenance-check) for coverage and limitations.

The polling bot also supports opt-in viewing follow-ups: an admin sends `/followups_here` in the test destination to baseline existing Ryot movie completions without historical prompts. Newly observed completions generate a persistent household watched-confirmation and **1-5** rating question with explicit selection and confirmation; legacy questions retain their **0-10** scale. Confirmation does not duplicate a watch; a confirmed rating is linked to the existing event and verified in Ryot. `/followups_stop` pauses delivery. The header eye button opens **Viewing inbox** for all configured users, with a count of unique movies awaiting input. Confirmation and rating can be completed there or in Telegram using the same shared questions. A personal No leaves others pending; the first Yes clears other users immediately and offers the respondent an optional rating. All No answers move to Admin → Declined viewings without deleting Ryot history. New private reviews identify the rater; the viewing history remains household-wide. Retention execution and Jellyfin watch ingestion are not enabled. See [viewing commissioning](maintainer/development/README.md#viewing-confirmation-and-rating) before activation.

Admins can open **Admin → Settings** in the Mini App to change the installation name (default **Household**) and switch **Automatic Owned cleanup** on or off. The optional cleanup removes only movie Owned memberships in Ryot, including existing movies, and preserves series, other collections, watches, ratings and files. It runs daily while the Telegram bot is running. The switch authorizes the policy directly; there is no first-run review. Two independent Telegram cleanup switches remove answered and/or unanswered viewing prompts after **36 hours**, retaining unanswered questions in Viewing inbox with direct answers and a Reopen in Telegram action. All automations default to off. Info controls explain the behavior and compact last-run lines report results. See [Settings and Owned cleanup](maintainer/development/README.md#settings-and-owned-cleanup).

Current retained surfaces:

- `documentation/` contains architecture, governance, and lineage/reference material.
- `src/curatarr/` contains the new Curatarr runtime package.
- `config.yaml.example` contains the initial YAML configuration template.
- `tests/` contains focused runtime foundation tests.
- `maintainer/` contains maintainer workflow, development, release, Docker, and test scaffolding.
- `docker/compose/` contains deployment example structure to be updated as the runtime takes shape.
- `.local/` is the ignored local-only safety bucket and must not be committed.

## Implementation Direction

The foundation was built in the following order. For the next implementation priority, use [the roadmap](maintainer/ROADMAP.md#current-sequence), not this historical breakdown:

1. Define Curatarr's adapter and workflow boundaries.
2. Add a YAML configuration model. Initial loader is present.
3. Add domain primitives for media identity, durable state, runtime state, and match confidence. Initial model is present.
4. Define adapter interfaces for Ryot, Radarr, Sonarr, Jellyfin, Telegram, and metadata. Initial protocols are present.
5. Add read-only Ryot durable-state mapping. Initial GraphQL client and adapter are present.
6. Introduce Curatarr database tables for workflow state, identity mappings, pending decisions, approvals, operational issues, jobs, and audit log. Initial SQLite schema is present.
7. Add identity matching over exact identifiers, confirmed mappings, metadata cross-resolution, and title/year fallback. Initial service is present.
8. Implement pure workflow planning for search/add/reactivation decisions. Initial planner is present.
9. Add metadata search and cross-resolution. Initial TMDB adapter is present.
10. Add read-only Radarr movie lookup and runtime-state mapping. Initial adapter is present.
11. Wire read-only metadata, Ryot, optional Arr lookup, identity matching, and workflow planning into a search decision service. Initial service is present.
12. Add read-only Sonarr show lookup and runtime-state mapping. Initial adapter is present.
13. Persist pending search/add decisions with revalidation fingerprints. Initial service is present.
14. Wire search decisions to optional pending-decision persistence. Initial orchestration path is present.
15. Add Telegram-facing search command rendering over persisted decisions. Initial transport-neutral service is present.
16. Validate Telegram callbacks against audience, expiry, allowed actions, and refreshed Ryot/Arr state. Initial handler is present.
17. Connect Telegram polling to movie search, explicit title selection, and persisted decision review. Initial transport and CLI composition are present; live deployment verification remains required.
18. Subsequent implementation now includes controlled catalogue/acquisition/profile writes, monitoring and default-off direct Jellyfin intake. Use the feature register for gaps rather than this historical sequence.

## Local Bootstrap

The Telegram Mini App is available as a separate opt-in web runtime:

```bash
curatarr --config /path/to/config.yaml --run-web --port 8765
```

It serves the interface and authenticated API together, bound to `127.0.0.1` by default. Open it from a Telegram Mini App launch button after configuring an HTTPS endpoint; opening the URL in a regular browser does not authenticate a user. The existing chat bot remains available independently. See [Mini App commissioning](maintainer/development/README.md#telegram-mini-app) for security, shared-history semantics, and test-hosting boundaries.

Install the package in a virtual environment:

```bash
python -m pip install .
```

Check a configuration file:

```bash
curatarr --config config.yaml --check-config
```

Run the Telegram movie search/decision runtime:

```bash
curatarr --config /path/to/config.yaml --run-telegram
```

Configure `tmdb.api_key`, the Telegram token, permitted user IDs, and the existing Ryot/Radarr read adapters first. Search uses `/movie <title>` or a plain movie title in a private chat. Results default to a poster carousel with title/year, a short synopsis, Previous/Next controls, and explicit movie selection. List view is available as a toggle, with a return to the current poster. Selecting a result shows durable history, current Arr state, warnings, and decision actions. Missing or unavailable artwork falls back to text with the same controls.

Decision callbacks re-read current state. Changed decisions are superseded and replaced; expired or completed prompts lose their buttons. Search prompts expire after 24 hours. `Manage active` refreshes the detail view and `Keep as is` closes the current question without changing media settings. In the default configuration, acquisition, profile, archive and other execution actions remain unavailable. The explicitly enabled coordinator staging acquisition path offers a native profile/root or Catalogue only, then a separate guarded confirmation; it does not enable retention or retire competing integrations.

SQLite state defaults to `data/curatarr.sqlite` beside the configuration file; override with `--database /path/to/curatarr.sqlite`. Candidate selections and polling offsets survive restarts. Run one polling process per bot token. Pending-question recovery/home controls and series commands are later work. See [the development guide](maintainer/development/README.md) for local verification and deployment limits.

Searcharr code may be consulted as reference only. New Curatarr runtime code should follow the architecture in `documentation/`.

## Maintainer Notes

The Docker and release helpers are scaffold-level until real Curatarr workflows exist. Do not publish production releases from this repository until the runtime contract has been re-established.

## Reference Lineage

Curatarr was informed by Searcharr and Searcharr-nxg experiments, but the project is now owned as a standalone architecture and implementation.

Normal Admin Declined viewings includes posters, Confirm household watched and Review again. Technical viewing controls are hidden with the other diagnostics. The maintainer diagnostic mode retains four actions: confirm, Review again, explicitly confirmed Clear viewing history, and local-only Clear stale request. Unavailable diagnostic actions stay grey with reasons. The history reset includes repeat watches, preserves ratings/reviews and library intent, and is never automatic. Library status highlights its compact watched/Radarr summary in blue.

Missing or changed native viewing records and pending history corrections remain monitored. Verified mismatches leave the ordinary inbox/count, and the system check alerts the admin. Diagnosis/repair belongs to repository maintenance; the Viewing requests diagnostic group and its controls are retained behind the maintainer flag.
