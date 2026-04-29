# System Architecture

Tracked architecture and decision-model reference for Curatarr.

## History

- `2026-03-23 | Codex | Initialized the tracked architecture baseline from the project handover and design specification`
- `2026-03-26 | Codex | Recorded the initial Ryot-Arr sync brainstorm and interim tag model`
- `2026-04-29 | Codex | Refined the Curatarr model around Ryot durable state, Arr active overlays, reconciliation, and pending decisions`
- `2026-04-29 | Codex | Compacted the architecture document and moved superseded thinking into historical notes`

## Scope & Boundaries

- This document defines Curatarr's product architecture, state model, authority model, workflows, and open design questions.
- It covers the intended Curatarr model, not inherited legacy Searcharr behavior.
- It does not define maintainer workflow or release process; those live under `maintainer/`.

## Authority (Owned Facts)

This document owns:

- component roles and authority boundaries
- durable Ryot state language
- Arr runtime state language
- workflow and reconciliation rules
- Telegram decision UX principles
- open architecture questions

## Authoritative Inputs (Consumed Facts)

This document consumes:

- project handover and design notes
- observed behavior from the Searcharr lineage
- current Ryot, Radarr, Sonarr, Jellyfin, TMDB, and Telegram integration constraints
- implementation findings recorded during development and cleanup experiments

## Change Rules

- Keep current doctrine in the main sections.
- Keep superseded reasoning only when it explains why the current model exists.
- Prefer one authoritative location for each mutable fact.
- When runtime behavior materially changes, update this document in the same change set.

## Product Objective

Curatarr is a Telegram-first media request and library-management assistant.

It lets users search and add movies or series while checking durable history in Ryot and current execution state in Radarr or Sonarr. Its purpose is to prevent blind re-adds, avoid unnecessary downloads, surface watched history, and keep Ryot and Arr applications aligned through controlled reconciliation.

## System Roles

| Component | Role |
| --- | --- |
| `Curatarr` | Workflow orchestrator, policy engine, Telegram control surface, and reconciliation worker. |
| `Ryot` | Durable source of truth for user-facing media state and historical awareness. |
| `Radarr` | Movie acquisition, monitoring, profile, quality, file, and execution layer. |
| `Sonarr` | Series equivalent of Radarr; same architectural role with series-specific semantics still open. |
| `TMDB` | Search and metadata candidate source for movies. |
| `Telegram` | Interactive user and admin decision surface. |
| `Jellyfin` | Passive playback sensor that marks media watched through Ryot. |

### Durable-State Adapter Principle

Curatarr should treat Ryot as the first durable-state backend, not as an inseparable internal model.

The internal Curatarr domain should use neutral concepts such as `Library`, `Watched`, `Archived`, `Owned`, `Radarr active`, and `Sonarr active`. The Ryot integration maps those concepts to Ryot records and collections. If another durable media database later supports equivalent mechanisms such as collections, tags, labels, or status flags, adding it should primarily require a new adapter rather than rewriting Curatarr workflows.

Adapter responsibilities:

- resolve titles and stable external identifiers
- read and write durable state memberships
- expose changed-state snapshots for reconciliation
- preserve dates or metadata where the backend supports them
- translate backend-specific errors into Curatarr workflow errors

Curatarr workflows should not depend directly on Ryot GraphQL shapes outside the Ryot adapter boundary.

### Media Identity Principle

Curatarr should model media identity as typed external identifiers, not as unqualified numeric IDs.

Examples:

- `tmdb:123`
- `imdb:tt123456`
- `tvdb:123456`
- `ryot:met_xxx`
- `radarr:123`
- `sonarr:456`
- `jellyfin:item_id`

The namespace is part of the identity. `imdb:tt123456` and `tvdb:123456` are not related just because their numeric portions look similar.

Confirmed cross-reference mappings belong in the Curatarr database. They are adapter/workflow glue, not durable user-facing media state in Ryot.

Matching rules:

- strong match:
  - exact same typed external identifier
  - user/admin-confirmed local cross-reference mapping
- medium match:
  - identifiers can be resolved through trusted metadata lookup to the same canonical media item
  - example: TMDB and IMDb resolve to the same movie
- weak match:
  - title/year or title-only candidate
  - useful for suggestions, never enough for destructive sync

Default media priorities:

- movies:
  - prefer exact `tmdb` or exact `imdb`
  - use metadata lookup to cross-resolve TMDB/IMDb where needed
- series:
  - prefer exact `tvdb`
  - allow trusted cross-resolution when Ryot, Sonarr, or Jellyfin expose different identifier namespaces

Destructive actions such as archive, delete, exclusion sync removal, or Arr removal require a strong match or an explicitly confirmed local mapping.

### Identity Conflict UI

Curatarr should open an identity-conflict decision whenever it finds a likely match that is not strong enough for safe sync.

Triggers:

- title/year match but no shared typed identifier
- Ryot and Arr records have different identifier namespaces that cannot be cross-resolved automatically
- Jellyfin confirms a title by path/title but not by trusted provider ID
- multiple candidate matches are possible
- a destructive action would otherwise depend on a weak or medium match

Prompt audience:

- admin-only by default for reconciliation, drift, exclusion sync, and destructive actions
- standard user may see the prompt only when they initiated the search/add flow and the action is non-destructive

Prompt content:

- source system and record for each candidate
- title, year, media type, and current state
- all known typed identifiers
- relevant file/path/source hints if available
- proposed match confidence: `medium` or `weak`
- reason Curatarr cannot prove the match automatically

Default actions:

| Action | Meaning |
| --- | --- |
| `Confirm same title` | Store a strong local mapping in the Curatarr database. |
| `Not the same` | Store a negative mapping or suppress this candidate pair for future prompts. |
| `Choose another candidate` | Show alternative candidates if available. |
| `Ask later` | Keep parked and revalidate before showing again. |
| `Open details` | Show extended identifiers, source links, and file/path details. |

After confirmation:

- confirmed positive mappings become strong matches
- confirmed negative mappings prevent repeated false-positive prompts
- mappings are Curatarr workflow/adapter state, not Ryot durable media state
- destructive actions may proceed only after revalidation of the now-strong mapping

## Authority Model

Curatarr must separate durable user meaning from execution facts.

| Fact type | Authority |
| --- | --- |
| Title is known to the library | Ryot |
| Title was watched | Ryot `Completed` |
| Title is archived or canceled | Ryot `Archived` collection/label |
| Title has existed in inventory | Ryot `Owned` |
| Title is active in movie workflow | Ryot `Radarr` overlay plus Radarr verification |
| Title is active in series workflow | Ryot `Sonarr` overlay plus Sonarr verification |
| File exists, profile, quality, monitored state | Radarr or Sonarr |
| Native Arr watchlist re-add prevention | Arr exclusion list derived from Ryot `Archived` |
| Pending questions, webhook state, scan cursors | Curatarr internal workflow state |
| Jellyfin reception check status | Curatarr internal operational state |

### Control Rule

- Ryot is the durable source of truth.
- Curatarr is the orchestrator that reads Ryot and Arr state, asks for missing intent, then writes the required changes.
- Radarr and Sonarr are execution systems. Their direct changes are operational facts that Curatarr reconciles back into Ryot before deciding follow-up actions.
- Once Curatarr is operational, users should make stack changes through Curatarr rather than directly in Ryot. Direct Ryot edits are tolerated for commissioning and exceptional cleanup, then detected by periodic scan.

## State Model

The current model separates durable Ryot state from Arr runtime state.

### Durable Ryot State

| State | Meaning |
| --- | --- |
| `Library` | The title exists in Ryot and is part of the long-term awareness space. Searched, watched, active, archived, and manually known titles may all live here. |
| `Watched` | The user has watched the title, regardless of where or how. This maps to Ryot `Completed` and does not imply archive or deletion. |
| `Archived` | The title is no longer wanted in active processing but should remain known. This includes watched-and-deleted titles, unwatched canceled titles, and abandoned titles. |
| `Owned` | The title has existed in the user's physical or digital inventory at least once. This maps to Ryot `Owned`, may carry Ryot's optional `Owned on` date, and remains true after deletion or archive. |
| `Radarr` | The title is part of the active movie workflow. It should mirror current Radarr presence after reconciliation, not historical ownership. |
| `Sonarr` | The title is part of the active series workflow. It should mirror current Sonarr presence after reconciliation, not historical ownership. |

### Arr Runtime State

| State | Meaning |
| --- | --- |
| `Not active` | Title is not present in Radarr or Sonarr. |
| `Active missing monitored` | Title exists in Arr, has no file, and is actively wanted. |
| `Active missing unmonitored` | Invalid state; title should leave Arr or be re-monitored. |
| `Active downloaded monitored` | Title has a file and remains eligible for upgrade processing. |
| `Active downloaded unmonitored` | Title has a file and is intentionally retained without further upgrade processing. |

### Runtime Modifiers

These are decision inputs, not durable state:

- `file_present`
- `monitored`
- `current_profile`
- `desired_profile`
- `current_quality`
- `quality_meets_profile`
- `current_size`

### Derived Rules

- `Library = true` when the title exists in Ryot.
- `Watched = true` when Ryot marks the title `Completed`.
- `Owned = true` when the title is in Ryot `Owned`; Curatarr may add this after a successful Arr import.
- `Arr active = true` when the matching Ryot active overlay and the matching Arr application agree after reconciliation.
- `quality_meets_profile = true` when the current file quality satisfies the chosen profile.

### Role Of `Owned`

- `Owned` replaces the earlier proposed `Downloaded` marker.
- `Owned` means the title has existed in the user's inventory at least once.
- `Owned` is useful during search, reactivation, cleanup, and audit flows.
- `Owned` does not mean a file is currently present.
- `Owned` does not decide archive, delete, monitor, or profile actions by itself.
- Curatarr should treat external changes to `Owned` as Ryot state that may need reconciliation, not as a command to Radarr or Sonarr.

### Stable States

| Stable state | Durable state | Runtime state |
| --- | --- | --- |
| `Library only` | Library, not watched, not archived, not Arr active | Not active |
| `Active wanted` | Library, Arr active | Missing file, monitored |
| `Active downloaded` | Library, Arr active, optionally watched | File present |
| `Watched active` | Library, Watched, Arr active | File may be present |
| `Archived watched` | Library, Watched, Archived | Not active, no file expected |
| `Archived unwatched` | Library, Archived | Not active, no file expected |

The invalid state is `Arr active + missing file + unmonitored`. If a title has no file and is not wanted, it should leave Arr and remain only in Ryot.

## Action Model

### Universal UX Rule

If `Watched = true`, Curatarr must always warn the user that the title was already watched. This warning is independent of current Arr state.

### Allowed Actions

| Situation | Allowed actions |
| --- | --- |
| Not in Ryot and not active in Arr | Add |
| In Ryot, not archived, not active in Arr | Reactivate with profile selection |
| In Ryot, archived, not active in Arr | Reactivate only after explicit confirmation and profile selection |
| Arr active, missing file, monitored | Search now, change profile, archive/cancel |
| Arr active, missing file, unmonitored | Re-monitor with profile selection, archive/cancel |
| Arr active, downloaded, monitored | Open in Jellyfin, keep as is, keep searching, change profile, archive |
| Arr active, downloaded, unmonitored | Open in Jellyfin, re-monitor, change profile, archive |

### Post-Watch Intent Actions

| Action | Ryot result | Arr result | File result |
| --- | --- | --- | --- |
| `Archive` | Keep Library and Watched, add Archived, remove active overlay | Remove from Arr | Delete file |
| `Keep as is` | Keep Library, Watched, and active overlay | Keep entry, set unmonitored | Keep file |
| `Keep searching` | Keep Library, Watched, and active overlay | Keep entry monitored | Keep file |
| `Change profile` | Keep Library, Watched, and active overlay | Apply selected profile, monitor as needed | Keep file |

Archive always means leaving active processing. It deletes the file if one exists, removes the active Arr overlay, and keeps Ryot history.

### Cancel Before Download

Cancel is archive-like for an unwatched or not-yet-downloaded title. The title remains known in Ryot, active Arr state is removed, and `Archived` may be added to record that the user is done with it.

## Core Workflows

### Flow 1: Search, Add, Reactivate

Trigger:

- user searches for a movie or show through Curatarr
- user selects a title and wants to add or manage it

Curatarr checks:

- Ryot existence, watched state, archived state, owned state, and active overlays
- Radarr or Sonarr presence, file presence, monitored state, profile, and quality
- exclusion-list or blocked-add conditions where applicable

Branches:

| Branch | Handling |
| --- | --- |
| No Ryot record and not active in Arr | Offer add flow. |
| Ryot record exists, not archived, not active in Arr | Show full history/context and ask for reactivation with profile selection. |
| Ryot record exists, archived, not active in Arr | Require explicit confirmation before reactivation and profile selection. |
| Ryot record exists and Arr active | Do not duplicate-add; show management options. |

Write order:

1. Resolve the intended title.
2. Reconcile obvious drift if needed.
3. Write durable intent to Ryot.
4. Execute the Arr change.
5. Verify resulting Ryot and Arr state.

### Flow 2: Watched / Completed

Trigger:

- Jellyfin marks a title watched in Ryot
- user manually marks a title watched in Ryot

Rules:

- Ryot `Completed` means `Watched`.
- Watched does not imply archived, deleted, or no further action.
- Whenever a movie is watched, Curatarr should ask what to do with it.
- The prompt must live in persistent Curatarr workflow state, not only in an old Telegram message.

Curatarr checks:

- whether the title has a file
- whether quality meets the desired profile
- whether an unresolved decision already exists

Known post-watch outcomes:

- archive and delete
- keep file and unmonitor
- keep file and continue searching
- keep file and change profile

### Flow 3: Post-Watch Resolution

Trigger:

- a watched title with file present has an open decision
- user answers the post-watch prompt

Resulting states:

| Choice | Result |
| --- | --- |
| Archive | Library yes, Watched yes, Archived yes, Arr active no, file no. |
| Keep as is | Library yes, Watched yes, Arr active yes, file yes, monitored no. |
| Keep searching | Library yes, Watched yes, Arr active yes, file yes, monitored yes. |
| Change profile | Library yes, Watched yes, Arr active yes, file yes, profile user-selected. |

Decision handling:

1. Reload current Ryot and Arr state.
2. Validate that the pending question still applies.
3. Update Ryot first.
4. Update Arr second.
5. Verify resulting state.
6. Close the pending decision.

### Flow 4: Missing File / Delete

Trigger:

- Arr reports a missing file
- Curatarr scan detects a missing file
- file disappears through Jellyfin, filesystem, Radarr, or manual cleanup

Rule:

- A missing file is a fact, not a complete intent.
- File loss can mean intentional archive, cancellation, accidental deletion, temporary mount failure, or external cleanup.

Handling:

| Case | Handling |
| --- | --- |
| Expected deletion after Curatarr archive | Verify file gone, active overlay removed, Ryot archive state correct, close task. |
| Unexpected deletion | Reconcile visible facts and open or resume clarification prompt. |

Prompt options for unexpected missing files:

- `Archive`: preserve Ryot history, add Archived, remove active overlay.
- `Keep monitored`: keep Arr active, set monitored, require or offer profile selection.

No third branch is needed.

### Flow 5: Ryot Drift / Manual Edit

Trigger:

- user changes state directly in Ryot UI
- future Ryot app changes state
- scheduled Curatarr scan detects a Ryot difference

Strong Ryot-side actions:

- add movie or series
- mark watched
- archive title

Rule:

- Direct Ryot edits are not the normal operational path.
- Curatarr remains the main authority for user-initiated changes to the stack.
- Ryot is scanned periodically to catch setup-time edits, accidental direct edits, or future app-side changes.
- Detected Ryot changes trigger reconciliation, not blind propagation.
- Curatarr must consult Ryot, Arr, and pending workflow state before acting.

Default handling:

| Ryot action | Handling |
| --- | --- |
| Add title | Reconcile, then add or reactivate downstream if no contradiction exists. |
| Mark watched | Reconcile, then open post-watch resolution if needed. |
| Archive title | Reconcile, then remove active Arr state and delete file if present unless policy requires confirmation. |

Installations should be able to configure whether Ryot-initiated actions are auto-applied, user-confirmed, or admin-confirmed.

### Ryot Scan Policy

Curatarr should not rely on hacked Radarr/Sonarr push integrations to detect Ryot changes.

Recommended scan model:

- `Targeted scan`
  - run before showing a pending decision
  - run before executing a user action
  - checks only the title being handled
- `Periodic Ryot scan`
  - configurable interval
  - intended to catch direct Ryot changes made outside Curatarr
  - default recommendation: every 30-60 minutes once operational
- `Daily full reconciliation`
  - compares Ryot, Radarr, and Sonarr fully
  - intended to catch missed webhooks, downtime drift, and bulk inconsistencies

Expected cost:

- At the current known scale of roughly 1,000-2,000 Ryot movie items, a collection-based scan is small.
- A scan should page through only the collections Curatarr owns or depends on: `Completed`, `Archived`, `Owned`, `Radarr`, and `Sonarr`.
- With a page size around 100-250, this is typically tens of GraphQL requests, not thousands.
- Curatarr should store a local snapshot of membership sets and only fetch per-title details for changed items.
- Direct Ryot edits may be detected with scan latency, which is acceptable because Curatarr is the intended operational entry point.

### Flow 6: Arr-Initiated Manual Edit

Trigger:

- user changes Radarr or Sonarr directly
- external process changes Arr state
- webhook or scheduled scan detects the change

Rule:

- Arr-side changes are operational facts, not complete semantic intent.
- Curatarr reconciles the Arr change into Ryot first.
- Only after Ryot reflects the reconciled state may Curatarr decide whether to auto-sync or prompt.

Classification:

| Change | Default handling |
| --- | --- |
| First successful import | Add `Owned` in Ryot if missing. |
| Direct profile change | Usually accept as execution-layer state. |
| Direct Arr add, non-archived title | Auto-reconcile unless policy requires confirmation or contradiction exists. |
| Direct Arr add, archived title | Never silently reactivate; require Telegram confirmation. |
| Direct file delete | Reuse missing-file flow. |
| Direct title removal | Ask whether to archive/cancel or reactivate unless caused by known Curatarr action. |
| Monitored change creating active + missing + unmonitored | Prompt for archive/cancel or re-monitor with profile selection. |

Direct Arr removal prompt:

- watched title: `Confirm Archive` or `Reactivate`
- unwatched title: `Confirm Cancel / Archive` or `Reactivate`

If archive/cancel is confirmed, Curatarr keeps Ryot Library, preserves Watched and Owned if present, adds Archived, removes the active overlay, and leaves the title absent from Arr.

If reactivation is chosen, Curatarr removes Archived only after confirmation, restores the active overlay, adds the title back to Arr, requires profile selection, and sets monitored on.

### Arr Exclusion Sync

Ryot `Archived` remains the source of truth for content that should not be reactivated silently.

Arr exclusions are a derived execution-layer guard used to prevent native Radarr/Sonarr watchlists from repeatedly re-adding archived or canceled titles. This is especially relevant when Radarr or Sonarr imports external lists on a schedule.

Rules:

- Curatarr should sync Ryot `Archived` into Radarr/Sonarr exclusions when native Arr watchlists are enabled.
- Curatarr should add missing Arr exclusions for archived titles with stable identifiers.
- Curatarr should detect Arr exclusions that do not correspond to Ryot `Archived` and treat them as drift.
- Curatarr explicit reactivation removes the matching Arr exclusion after user confirmation.
- Arr exclusion state must not become durable truth; Ryot `Archived` remains authoritative.
- Users should not manage Arr exclusions directly once Curatarr is operational.

Recommended sync behavior:

1. Read Ryot `Archived`.
2. Resolve movie TMDB IDs and series TVDB IDs where available.
3. Read current Radarr/Sonarr exclusions.
4. Add exclusions missing from Arr.
5. Detect extra Arr exclusions that are not backed by Ryot `Archived`.
6. Resolve extra exclusions according to policy:
   - auto-remove when safe and policy allows
   - or ask admin whether to remove the Arr exclusion or add `Archived` in Ryot
7. On confirmed Curatarr reactivation, remove the matching exclusion.

This prevents recurring bulk drift prompts from native watchlists while preserving Curatarr's ability to intentionally reactivate archived content.

### Flow 7: Sonarr / Series Semantics

Series use the same high-level model as movies, but the default workflow operates at show level to avoid excessive prompts.

Durable state:

- `Library`
  - the show exists in Ryot
- `Watched`
  - Ryot has completed/watched state for the show or tracked episode progress
  - for series, watched state may be partial
- `Archived`
  - the user is done with the show as an active Sonarr target
  - archiving a show removes the show from active Sonarr processing
- `Owned`
  - at least one episode has existed in the user's inventory
  - Curatarr may add this after the first successful Sonarr import for the show
- `Sonarr`
  - the show is part of the active series workflow

Runtime state remains Sonarr-owned:

- monitored show state
- monitored season state
- monitored episode state
- episode file presence
- episode quality and profile
- season folder/path state

Default scope:

- Curatarr MVP should treat series activation, archive, reactivation, and exclusion as show-level actions.
- Season-level and episode-level controls are runtime details Curatarr may show, but they should not become durable user intent in Ryot by default.
- Per-season or per-episode intent can be added later if there is a clear need.

Post-watch behavior:

- Curatarr should not ask a keep/archive question after every watched episode.
- Default post-watch prompt for series should trigger only when:
  - Ryot marks the whole show completed
  - the user explicitly opens management for the show
  - a configurable season-completed policy is enabled
- For long-running shows, the normal stable state is active in Sonarr while watched progress accumulates in Ryot.

Import behavior:

- First successful episode import adds Ryot `Owned` for the show if missing.
- Episode or season upgrades update runtime facts only.
- Each import may start a Jellyfin reception check for the imported episode, but failures are admin-only operational issues.

Archive behavior:

- Archiving a show means the user is done with the show as active processing.
- Curatarr removes the show from Sonarr and removes the Ryot `Sonarr` overlay.
- If files exist, archive deletes the show files unless policy requires admin confirmation.
- Watched progress and `Owned` remain in Ryot.

Missing-file behavior:

- Missing individual episode files are operational facts and should not create normal user prompts by default.
- Unexpected large-scale missing files, missing season folders, or missing whole-show files should create admin-facing operational issues.
- If the user explicitly removes a show from Sonarr, Curatarr uses the same archive/reactivate decision model as movies.

Exclusion behavior:

- Ryot `Archived` shows should sync to Sonarr exclusions when native Sonarr watchlists/import lists are enabled.
- Confirmed Curatarr reactivation may remove or override the matching Sonarr exclusion.

### Flow 8: Import And Jellyfin Reception

Trigger:

- Radarr or Sonarr reports a successful import
- scheduled reconciliation discovers a newly present file

Rules:

- Import is an acquisition fact, not a user intent decision.
- Import should not create a normal user prompt.
- First successful import should add Ryot `Owned` if missing.
- Upgrade import should update runtime facts only.
- Redownload after missing-file recovery should close or revalidate the recovery task if applicable.

After import, Curatarr should create a short-lived internal `Jellyfin reception pending` check.

Reception check:

1. Wait a configurable delay so Jellyfin has time to scan.
2. Query Jellyfin for the title using stable identifiers where possible.
3. Mark reception confirmed when Jellyfin has indexed the item.
4. If confirmation times out, create an operational issue.

Reception failure:

- means Radarr/Sonarr imported a file, but Jellyfin does not show it as watchable
- may indicate mover failure, datastore visibility problems, mount issues, permissions, or Jellyfin scan delay
- does not change user intent state by itself

Operational prompts:

- reception failure prompts should be admin-only by default
- normal users should not receive technical datastore or mover-failure questions
- admin options may include:
  - `Retry Jellyfin check`
  - `Trigger Jellyfin library scan`
  - `Show paths / diagnostics`
  - `Ignore for now`
  - `Mark resolved manually`

### Flow 9: Drift Detection And Reconciliation

Trigger:

- scheduled scan
- startup scan
- webhook follow-up validation
- initial Curatarr commissioning
- recovery after downtime

Detection logic is the same for single drift and bulk drift. Handling mode differs.

| Mode | Use |
| --- | --- |
| `Daily sanitizing` | Normal operation and low mismatch volume. Auto-fix factual discrepancies and ask about ambiguous intent individually. |
| `Bulk correction` | Commissioning, long offline recovery, or larger mismatch sets. Group by category and process in batches. |

Classification:

| Class | Meaning | Examples |
| --- | --- | --- |
| `Safe auto-fix` | Factual mismatch with low ambiguity. | Arr title missing from Ryot Library, `Owned` missing while file presence is known, active overlay missing for active Arr title. |
| `Policy auto-fix` | Safe only if installation policy allows it. | Direct Arr add of non-archived title, direct Ryot add that should activate downstream. |
| `Needs clarification` | Intent cannot be inferred safely. | Archived in Ryot but active in Arr, unexpected file missing, title removed from Arr but still active in Ryot, watched active title with unclear keep/archive intent. |

Bulk correction wizard example:

```text
23 archived in Ryot, but active in Arr
[Fix all] [List first 5 titles] [Fix next 5]
```

Guiding rule:

- auto-fix facts
- ask about intent

### Flow 10: Pending Decision Recovery

Pending decisions are Curatarr workflow state. They must not rely on old Telegram messages remaining visible.

Trigger:

- user returns later
- Curatarr restarts
- scheduled reconciliation finds unresolved decisions
- a new event affects a title with an open decision

UX direction:

- maintain one persistent Telegram home/control message
- show system summary and outstanding question counts
- group pending questions by category
- allow the user to park questions and perform other actions
- open one active detail decision at a time

Validation rule:

- revalidate each pending decision against current Ryot and Arr state before showing it
- close obsolete questions as resolved or superseded
- replace stale questions with the current correct question

Background maintenance:

- scheduled reconciliation should refresh pending-question validity and counts
- a default 24-hour scan is a reasonable configurable starting point

## Entry Points

| Entry point | Strength | Limitation | Curatarr role |
| --- | --- | --- | --- |
| `Curatarr / Telegram` | Full controlled workflow, best context, profile selection, user prompts. | Requires user to use Curatarr. | Primary control surface and orchestrator. |
| `Ryot UI` | Useful for commissioning and exceptional cleanup. | Not the normal operational entry point; no reliable general outbound events. | Detect by periodic scan, reconcile, complete downstream action or prompt. |
| `Radarr/Sonarr UI` | Strong execution UI and webhook source. | Does not express durable user intent completely. | Reconcile into Ryot first, then prompt or auto-fix. |
| `Jellyfin` | Reliable watched signal through Ryot. | Does not express keep/archive intent. | Detect watched state and open pending decision. |

### High-Level Data Flows

```text
Search/Add:
User -> Telegram -> Curatarr -> Ryot check
                             -> Arr check
                             -> user decision
                             -> Ryot update
                             -> Arr execution
                             -> verification
```

```text
Watched:
Jellyfin -> Ryot Completed -> Curatarr detects/reconciles
                           -> Telegram decision
                           -> Ryot + Arr update
```

```text
Import reception:
Radarr/Sonarr import -> Curatarr marks/keeps Owned
                     -> Jellyfin reception pending
                     -> Jellyfin API check
                     -> confirmed or admin-only operational issue
```

```text
Direct Arr change:
Radarr/Sonarr -> webhook or scan -> Curatarr
                                  -> Ryot reconciliation
                                  -> optional prompt
                                  -> downstream correction
```

```text
Direct Ryot change:
Ryot UI -> periodic Curatarr scan
        -> Curatarr reconciliation
        -> Arr check
        -> optional prompt
        -> Arr correction
```

## Integration Rules

### Radarr And Sonarr

- Prefer edit and monitor-state changes over delete-and-readd flows.
- Do not delete titles automatically unless an explicit archive/cancel workflow authorizes it.
- Respect exclusion lists and surface that condition explicitly.
- Maintain Arr exclusions as a derived guard from Ryot `Archived` when native Arr watchlists are enabled.
- MVP actions should rely on lookup, add, edit monitored state, profile selection where supported, and search trigger.
- Webhooks are useful event sources but do not replace reconciliation scans.

### Ryot

- Curatarr reads Ryot for Library, Completed/Watched, Archived, Owned, and active overlay state.
- Curatarr writes durable intent to Ryot before executing downstream changes.
- Ryot `Completed` must not be overloaded to mean archived, deleted, or no further action.
- Curatarr is the intended writer of Ryot `Owned` for Radarr/Sonarr media.
- External Ryot integrations that sync to `Owned` should be avoided by deployment policy unless explicitly accepted.
- If Curatarr detects an external `Owned` change, it should accept it as durable Ryot state but record it as external-origin drift.
- Direct Ryot edits are allowed during setup or exceptional cleanup, but Curatarr remains the intended operational control surface.
- Ryot changes outside Curatarr are detected by configurable periodic scan, not by hacked Arr push integrations.

### Telegram

- Telegram should expose both action flows and pending decision recovery.
- The home/control message should summarize system state and outstanding decisions.
- Completed actions should reach a terminal completed state and should not leave stale action buttons open.
- `/help` remains command reference; `/start` should become the interactive entrypoint.
- Technical operational issues, such as Jellyfin reception failures after Arr import, should be routed to admin users by default.

## Configuration Model

Curatarr should use a YAML configuration file for the initial implementation.

The repository should provide a heavily commented template that explains every section and the operational impact of each option. The template should be understandable without reading the source code.

Required sections:

| Section | Purpose |
| --- | --- |
| `users` | Telegram user IDs, admin IDs, standard users, and optional per-user permissions. |
| `telegram` | Bot token, default chat behavior, home/control message behavior, and pending-question display rules. |
| `ryot` | URL, API key, scan interval, and collection names for `Archived`, `Owned`, `Radarr`, and `Sonarr`. |
| `radarr` | URL, API key, default profile, root folder, exclusion sync, and native watchlist assumptions. |
| `sonarr` | URL, API key, default profile, root folder, exclusion sync, and native watchlist assumptions. |
| `jellyfin` | URL, API key, reception checks, first-check delay, retry interval, and timeout. |
| `policy` | Approval modes, timeout values, exposure routing, destructive-action rules, and drift auto-fix behavior. |
| `identity` | Matching rules, weak-match behavior, title/year suggestion settings, and admin-confirmation requirements. |
| `logging` | Log level and operational audit settings. |

Configuration principles:

- YAML is the user-facing configuration source for MVP.
- Top-level keys and nested keys should use `snake_case`.
- Values that can cause destructive behavior must be explicit and documented.
- Defaults should be safe and match the policy tables in this document.
- Secrets should be supported through `${ENV_NAME}` environment variable expansion.
- Missing referenced environment variables should fail startup.
- Durations should use human-readable strings such as `30m`, `24h`, and `7d`.
- Future database/admin-UI configuration may override YAML, but YAML remains the bootstrap and recovery format.

Example policy shape:

```yaml
policy:
  approvals:
    default_mode: confirm_user
    timeout_defaults:
      normal_user_media_decision: 24h
      admin_operational_issue: 24h
    actions:
      add_new_title: confirm_user
      reactivate_archived_title: confirm_user
      first_import_owned_sync: auto_apply
      jellyfin_reception_failure: confirm_admin
      destructive_drift_correction: confirm_admin
      weak_match_action: disabled
```

### User Exposure Policy

Curatarr should distinguish normal media decisions from technical operational decisions. Defaults should be safe, but installations must be able to configure routing.

| Interaction type | Standard user default | Admin default | Notes |
| --- | --- | --- | --- |
| Search/add/reactivate title | Yes | Yes | Core user-facing Curatarr function. |
| Already-watched warning | Yes | Yes | Shown whenever relevant to the acting user. |
| Post-watch keep/archive decision | Yes | Yes | Normal media intent decision. |
| Missing-file decision for user-requested title | Optional | Yes | Can be user-facing if phrased as keep/archive; technical detail should stay admin-facing. |
| Archived-title reactivation confirmation | Yes | Yes | Required because archived content must not silently reactivate. |
| Profile selection/change | Yes | Yes | User can choose when they initiated or own the request; admin can override by policy. |
| Bulk drift correction wizard | No | Yes | Administrative maintenance. |
| Jellyfin reception failure | No | Yes | Technical issue: datastore, mover, mount, permissions, or Jellyfin scan failure. |
| Arr webhook failure / API failure | No | Yes | Operational issue. |
| Ryot scan drift summary | No | Yes | Admin-facing unless a specific user decision is required. |
| Approval request for destructive action | Configurable | Yes | Installations may require admin confirmation for delete/archive actions. |

Configuration should allow each interaction class to be routed as `standard user`, `admin only`, `both`, or `silent auto-handle` where safe.

### Approval Policy

Approvals are separate from exposure. Exposure controls who sees a question; approval controls whether the selected action may execute immediately.

Default policy:

- normal search, add, reactivate, profile selection, and post-watch keep decisions can execute for the acting standard user
- archive/delete actions initiated by a standard user are allowed by default but must be configurable to require admin approval
- destructive actions detected from drift or external systems should require admin confirmation unless policy explicitly allows auto-fix
- technical maintenance actions are admin-only
- bulk correction actions are admin-only
- admins may override or resolve pending decisions

Approval lifecycle:

1. Curatarr records the requested action and the current state fingerprint.
2. If approval is required, Curatarr opens an approval item for the configured approver group.
3. Before execution, Curatarr reloads Ryot and Arr state.
4. If the state still matches the approval context, Curatarr executes the action.
5. If the state changed, Curatarr closes the approval as superseded and creates a new current decision if needed.

Timeout behavior:

- approval timeout should be configurable
- timed-out approvals should not execute
- timed-out approvals remain visible in admin history and may be reopened only after revalidation
- high-risk actions should require a fresh approval after timeout

Default timeout values:

| Item type | Default timeout | After timeout |
| --- | --- | --- |
| Normal user media decision | 24 hours | Keep parked; revalidate before showing again. |
| User destructive action awaiting optional admin approval | 24 hours | Expire approval; require fresh user/admin decision. |
| Admin technical operational issue | 24 hours | Keep visible as stale operational issue; do not auto-resolve. |
| Jellyfin reception failure | 24 hours | Keep visible as stale operational issue; allow retry/recheck. |
| Bulk correction wizard session | 24 hours | Expire active batch session; keep scan result if still valid after revalidation. |
| Weak-match identity confirmation | 24 hours | Keep parked; revalidate candidates before showing again. |
| High-risk destructive drift correction | 24 hours | Expire approval; require fresh admin approval. |

Timeouts are policy defaults, not hard-coded product rules. The default minimum is 24 hours because system state can become stale quickly.

Configurable approval modes:

| Mode | Meaning |
| --- | --- |
| `auto-apply` | Execute immediately after validation. |
| `confirm-user` | Ask the acting user before execution. |
| `confirm-admin` | Require admin approval before execution. |
| `disabled` | Do not offer the action. |

Default per-action policy:

| Action class | Default mode | Notes |
| --- | --- | --- |
| Add new movie/show through Curatarr | `confirm-user` | The user selects the title and profile; execution follows that explicit action. |
| Reactivate non-archived inactive title | `confirm-user` | Requires profile selection. |
| Reactivate archived title | `confirm-user` | Must show archived warning; admin approval can be enabled by policy. |
| Change profile | `confirm-user` | User-facing management action. |
| Re-monitor missing title | `confirm-user` | Requires or offers profile selection. |
| Keep watched title as is | `confirm-user` | User intent decision; execution unmonitors. |
| Keep watched title searching | `confirm-user` | User intent decision; execution keeps monitored. |
| Archive watched title from prompt | `confirm-user` | Destructive because it deletes files; admin approval can be enabled by policy. |
| Archive unwatched/canceled title from prompt | `confirm-user` | Destructive if files exist; admin approval can be enabled by policy. |
| First import adds Ryot `Owned` | `auto-apply` | Factual sync from Arr import. |
| Upgrade import updates runtime facts | `auto-apply` | Factual sync only. |
| Jellyfin reception failure | `confirm-admin` | Technical operational issue. |
| Arr webhook/API failure | `confirm-admin` | Technical operational issue. |
| Safe factual drift fix | `auto-apply` | Only when strong match exists and no intent is ambiguous. |
| Destructive drift correction | `confirm-admin` | Includes external removals, archive/delete, or unclear intent. |
| Bulk correction wizard | `confirm-admin` | Administrative maintenance. |
| Arr exclusion sync from Ryot `Archived` | `auto-apply` | Derived downstream guard when watchlists are enabled. |
| Remove Arr exclusion on confirmed reactivation | `confirm-user` | Part of explicit reactivation; admin approval can be enabled. |
| Weak/title-only match action | `disabled` | Must first resolve identity mapping. |

## Implementation Phases

### Phase 1: Search And Decision MVP

- TMDB movie search
- Ryot lookup
- Radarr lookup
- decision summary message
- action buttons
- watched warning

### Phase 2: Curatarr State Writes And Movie Reconciliation

- Ryot Library/Watched/Archived/Owned writes
- local Ryot state snapshots
- Radarr active overlay synchronization
- profile changes
- import handling that adds `Owned`
- Jellyfin reception checks after import
- post-watch pending decisions
- missing-file handling

### Phase 3: Operational Sync

- Radarr webhooks
- scheduled reconciliation scans
- drift classification
- bulk correction wizard
- Telegram home/control message
- exclusion-list handling
- Arr exclusion sync from Ryot `Archived`

### Phase 4: Sonarr And Multi-User Policy

- Sonarr support
- series-specific state semantics
- user/admin confirmation policies
- approval timeouts and superseded approvals

## Implementation Breakdown

This section converts the architecture into the first implementation plan. It defines build order and major ownership boundaries, not a detailed task tracker.

### 1. Data Model / Database Tables

Curatarr needs its own database for workflow state that should not live in Ryot.

Core tables:

| Table | Purpose |
| --- | --- |
| `media_identity` | Stores typed identifiers known for a logical media item. |
| `identity_mapping` | Stores positive and negative confirmed mappings between typed identifiers. |
| `state_snapshot` | Stores last known Ryot, Arr, and Jellyfin state fingerprints for reconciliation. |
| `pending_decision` | Stores user/admin questions, state fingerprints, expiry, audience, and current status. |
| `approval_request` | Stores approval lifecycle for actions requiring user/admin confirmation. |
| `operational_issue` | Stores admin-facing technical issues such as Jellyfin reception failure or webhook/API failure. |
| `job_run` | Stores background job status, timing, errors, and last successful run cursors. |
| `audit_log` | Stores user/admin actions and automated changes for traceability. |

Design rules:

- Ryot remains durable media state.
- Curatarr database stores workflow, mappings, snapshots, approvals, operational issues, and audit history.
- Destructive actions must reference a pending decision or approval record.
- Identity mappings must support positive and negative decisions.

### 2. Adapter Interfaces

Curatarr should isolate external systems behind adapters.

Required adapters:

| Adapter | Responsibilities |
| --- | --- |
| `RyotAdapter` | Read/write durable state collections, resolve metadata, scan collection memberships, apply `Archived`, `Owned`, `Radarr`, and `Sonarr` overlays. |
| `RadarrAdapter` | Lookup/add/update movies, monitor state, profile changes, file state, exclusion sync, command/search trigger, webhooks. |
| `SonarrAdapter` | Lookup/add/update shows, show-level active state, profile changes, file/runtime facts, exclusion sync, webhooks. |
| `JellyfinAdapter` | Confirm imported media is indexed/watchable, optionally trigger library scan, expose diagnostics. |
| `TelegramAdapter` | Send home/control message, decision prompts, admin issues, callbacks, and action completion messages. |
| `MetadataAdapter` | Search/candidate resolution and cross-resolution of TMDB/IMDb/TVDB where available. |

Design rules:

- Curatarr workflows consume domain objects, not raw API payloads.
- Ryot-specific GraphQL shapes stay inside `RyotAdapter`.
- Arr-specific API quirks stay inside Radarr/Sonarr adapters.
- Each adapter should expose typed errors that workflows can route to users or admins.

### 3. Background Jobs

Background jobs keep the stack aligned without requiring users to trigger every check.

Required jobs:

| Job | Purpose | Default cadence |
| --- | --- | --- |
| `ryot_scan` | Detect direct Ryot changes and collection drift. | Configurable, default `30-60m`. |
| `daily_reconciliation` | Full Ryot, Arr, Jellyfin consistency check. | Daily. |
| `arr_webhook_processor` | Process Radarr/Sonarr add/import/delete/file events. | Event driven. |
| `jellyfin_reception_check` | Verify imported media became watchable in Jellyfin. | Event driven with retries. |
| `pending_decision_revalidator` | Refresh pending decision validity and close superseded questions. | Before display and scheduled. |
| `arr_exclusion_sync` | Project Ryot `Archived` into Radarr/Sonarr exclusions. | Scheduled and after archive/reactivation. |
| `bulk_drift_wizard_builder` | Group large drift sets for admin batch review. | On commissioning or large scan result. |

Design rules:

- Jobs must be idempotent.
- Jobs must store last success/error state.
- Jobs must re-read current state before executing changes.
- Webhooks are wake-up signals, not final truth.

### 4. Telegram Workflows

Telegram is both the user control surface and the admin operations console.

Core flows:

- search/add/reactivate
- watched warning and post-watch keep/archive decision
- missing-file decision
- identity conflict decision
- archived reactivation confirmation
- admin operational issue handling
- bulk correction wizard
- persistent home/control message with pending counts

Design rules:

- One stable home/control message should summarize system state and pending work.
- Old detail prompts must be revalidated before action.
- Completed actions should reach a terminal state and remove stale buttons.
- Technical operational issues are admin-only by default.
- Normal users should not see datastore, mover, mount, or webhook failure details unless explicitly configured.

### 5. YAML Configuration Template

MVP configuration is YAML with clear comments.

Build outputs:

- `config.yaml.example`
- config loader with `${ENV_NAME}` interpolation
- fail-fast validation for missing required environment variables
- duration parser for `30m`, `24h`, and `7d`
- typed config model used by services and jobs

Template sections:

- `users`
- `telegram`
- `ryot`
- `radarr`
- `sonarr`
- `jellyfin`
- `policy`
- `identity`
- `logging`

Design rules:

- Comment every destructive or privacy-sensitive option.
- Safe defaults must match this document.
- Runtime secrets should be read from environment variables.

### 6. Clean Implementation Plan

Curatarr should be implemented as a new runtime that uses Searcharr lineage only as selective reference material.

Suggested order:

1. Establish Curatarr project/package/runtime identity.
2. Add the YAML configuration loader.
3. Define domain objects and adapter interfaces.
4. Add Ryot adapter and durable-state read/write operations.
5. Add Radarr and Sonarr adapters.
6. Add Curatarr database and persistence tables.
7. Implement Telegram search/add/reactivation as the first user-facing workflow.
8. Add pending decisions, approvals, and audit log.
9. Add background jobs and webhooks.
10. Add Jellyfin reception checks.
11. Add Arr exclusion sync.
12. Add Sonarr show-level workflow.

Implementation rule:

- Build Curatarr workflows around the adapter and decision model from the start.
- Consult Searcharr code only for proven API interaction details or Telegram UX references.
- Do not block MVP on future Ryot generic event support or season-level Sonarr management.

## Open Questions And Risks

There are no known architecture blockers at this stage. This section tracks implementation-sensitive decisions and risks that must remain visible while building the MVP.

### Download / Import Flow Detail

- Decision: import events update facts and do not create normal user-intent prompts.
- First successful import adds Ryot `Owned` if missing.
- Upgrade import updates runtime facts only.
- Redownload after missing-file recovery closes or revalidates the recovery task.
- Import starts a Jellyfin reception check; timeout creates an admin-only operational issue.

### Owned Automation Boundaries

- Decision: Curatarr is the preferred writer of Ryot `Owned` for Radarr/Sonarr media.
- Decision: external Ryot integrations that sync to `Owned` should be avoided by default.
- Decision: detected external `Owned` changes are accepted as durable Ryot facts but recorded as external-origin drift.
- Decision: Curatarr must not remove `Owned` automatically just because Arr no longer has a file, because `Owned` is historical inventory.

### Approval Policy

- Decision: Curatarr supports `auto-apply`, `confirm-user`, `confirm-admin`, and `disabled` approval modes.
- Decision: normal user media actions can execute directly by default, while technical maintenance and bulk correction are admin-only.
- Decision: approvals must be revalidated before execution and closed as superseded if state changed.
- Decision: per-action default policy matrix is defined in the Approval Policy section.
- Decision: default timeout values are defined in the Approval Policy section and remain configurable.

### Ryot Change Detection

- Curatarr treats Ryot changes as authoritative triggers.
- Decision: Curatarr detects direct Ryot changes through configurable periodic scans plus targeted revalidation before actions and prompts.
- Ryot generic outbound webhooks are not currently assumed.
- Curatarr will not depend on hacked Radarr/Sonarr push integrations for Ryot change detection.
- Ryot generic outbound webhooks are tracked as a future enhancement only.
- Even if generic Ryot events exist later, Curatarr must still re-read Ryot before acting; events are signals, not final truth.

### Exclusion / Re-add Policy

- Decision: Curatarr should implement Arr exclusion sync when native Radarr/Sonarr watchlists are enabled.
- Decision: Ryot `Archived` remains authoritative; Arr exclusions are a derived downstream projection.
- Decision: Curatarr explicit reactivation can remove or override the matching exclusion after confirmation.
- Decision: extra Arr exclusions not backed by Ryot `Archived` are drift and should be auto-removed or escalated by policy.

### Matching Risk

- Decision: Curatarr should use typed external identifiers such as `tmdb:123`, `imdb:tt123456`, and `tvdb:123456`.
- Decision: exact typed identifier matches and user/admin-confirmed local mappings are strong matches.
- Decision: trusted metadata cross-resolution is a medium match.
- Decision: title/year-only matches are weak matches and must not drive destructive sync.
- Decision: confirmed cross-reference mappings are stored in the Curatarr database.
- Decision: identity-conflict UI is admin-only by default for reconciliation, drift, exclusion sync, and destructive actions.
- Decision: positive and negative mapping decisions are stored in the Curatarr database.

### Configuration Schema

- Decision: MVP configuration uses YAML.
- Decision: the sample YAML must be heavily commented and organized by user-facing sections.
- Decision: safe defaults should match the exposure, approval, scan, exclusion, and identity policies in this document.
- Decision: YAML keys use `snake_case`.
- Decision: secrets use `${ENV_NAME}` interpolation.
- Decision: missing referenced environment variables fail startup.
- Decision: durations use human-readable strings such as `30m`, `24h`, and `7d`.

## Future Enhancements

These items are intentionally outside MVP scope.

### Optional Season-Level Management

- Maybe add season-level activation, archive, retention, and prompt policies later.
- Current decision remains show-level durable intent with season/episode state treated as Sonarr runtime facts.
- This should get a separate design pass only if real usage shows show-level management is insufficient.

### Ryot Generic Event Support

- Maybe request or contribute generic outbound Ryot webhooks later.
- Candidate event types include collection item added, collection item removed, seen/completed changed, metadata merged or disassociated, metadata deleted, and collection renamed or deleted.
- A future request or PR should define stable JSON payloads, configurable event selection, retry/error tracking, and per-user integration settings.
- Even if added, Curatarr should treat events as wake-up signals and still re-read Ryot before acting.

## Historical Notes

This section preserves superseded reasoning that explains the current model.

### Initial Sync Brainstorm

Early design considered keeping Radarr and Sonarr as near-complete historical mirrors. That approach was rejected because it made Arr databases large and messy while adding limited value. The current model keeps Arr applications as active execution sets and uses Ryot as durable memory.

### Interim Tag Model

The earlier interim model considered simple Arr-side tags such as `owned` and `watched`, with `wanted` inferred rather than stored. Current doctrine prefers Ryot durable collections/labels for user-facing history and keeps Arr tags as optional visibility aids only.

### Owned Versus Downloaded

The proposed `Downloaded` marker was replaced by Ryot `Owned` because it matches Ryot's existing language and supports an optional `Owned on` date. In Curatarr, `Owned` means historical inventory, not current file presence.
