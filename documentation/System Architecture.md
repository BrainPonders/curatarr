# System Architecture

Tracked architecture and decision-model reference for Curatarr.

## History

- `2026-03-23 | Codex | Initialized the tracked architecture baseline from the project handover and design specification`
- `2026-03-26 | Codex | Recorded the initial Ryot-Arr sync brainstorm and interim tag model`
- `2026-04-29 | Codex | Refined the Curatarr model around Ryot durable state, Arr active overlays, reconciliation, and pending decisions`
- `2026-04-29 | Codex | Compacted the architecture document and moved superseded thinking into historical notes`
- `2026-10-24 | Codex | Reviewed sole-coordinator transition, actual bypass integrations, workflow gaps and staged migration; no live cutover performed`

## Scope & Boundaries

- This document defines Curatarr's product architecture, state model, authority model, workflows, and open design questions.
- It covers the intended Curatarr model, not inherited legacy Searcharr behavior.
- It does not define maintainer workflow or release process; those live under `maintainer/`.

## Authority (Owned Facts)

This document owns:

- component roles and authority boundaries
- durable state roles and workflows; exact Ryot collection mapping is owned by the Ryot Collection Contract
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
| `Jellyfin` | Playback and library evidence source. The coordinator transition below proposes direct Curatarr ingestion in place of the legacy Ryot sink. |

### Coordinator Transition Review — 2026-10-24

The user requested a full workflow review before continuing the in-progress Jellyfin work. The direction is Curatarr as the sole household workflow coordinator, with Ryot storing confirmed catalogue intent/history. The detailed design and migration below are recommendations, not an executed integration migration. Older flow descriptions that route Jellyfin directly through Ryot describe the legacy path during this transition. Do not activate the removal draft or remove/disable live integrations merely because this review exists.

#### Ownership And Service Boundaries

| Component | Owns | Must not decide for another component |
| --- | --- | --- |
| User | Wantedness, watched confirmation, rating, deliberate removal, profile choice and explicit reactivation. | Silence is not a decision; one person's No is not a household veto. |
| Curatarr | Identity validation, policy, event correlation, pending questions, operation ordering, permissions, durable recovery and reconciliation. | Do not infer deliberate intent from absence, failure, a collection marker or cached metadata. |
| Ryot | Confirmed Library, Archived, Previously acquired, Deleted, native viewing events and reviews. Metadata enrichment and unavoidable native collection transitions remain Ryot behavior. | No direct acquisition/deletion policy or cross-service automation in the final model. Its API is a persistence boundary, not the workflow engine. |
| Radarr / Sonarr | Active execution set, native profiles, release scoring, monitoring, searches, imports, upgrades and managed-file facts. | They do not establish household watching, rating or archive intent. Curatarr must not duplicate their release-selection engines. |
| Jellyfin | Playback observations, explicit played/unplayed changes, indexed media/copies, source/audio evidence and removal hints. | An indexed item is not verified storage availability; ItemRemoved is not proof of deliberate physical deletion. |
| TMDB / IMDb / TVDB | Namespaced identity and metadata evidence. | A missing provider record does not prove cancellation or authorize removal. |
| Telegram chat and Mini App | Two views of the same authenticated decisions and operation state. | Neither interface may independently implement provider mutation rules or invent a separate household watch record. |

The resulting path is sources → Curatarr validation/decision journal → supported Ryot/Arr APIs → verified result. Ryot remains durable memory; Curatarr's SQLite database is equally operationally essential for unfinished work, deduplication, attribution and recovery. It is not disposable cache. Back up both, including a tested workflow restore procedure; a Ryot restore alone must not recreate old notifications or repeat destructive work.

“Dumb” means removing cross-service decision ownership, not bypassing the Ryot API or trying to disable its native database behavior. Native progress operations can alter Completed/In Progress/Watchlist/Monitoring; collection jobs can be asynchronous and reads cached. The adapter must contain these side effects, verify the account and read back fresh state. Preserve cinema/Trakt/imported history; do not rewrite it merely to standardize provenance.

#### Current Implementation Versus Target

Historical review snapshot, 2026-10-24, before subsequent implementation. For current capability/availability use the [Feature register](../maintainer/FEATURES.md); this table is rationale, not current status.

| Area | Current evidence | Required change |
| --- | --- | --- |
| Search and movie status | Movie search, exact-ID matching, history/status/profile reads and pure add/reactivate planning exist. Telegram acquisition buttons currently return “not available yet”; Mini App has no acquisition executor. | Add a real profile-selection/confirmation flow backed by one acquisition service shared by both interfaces. |
| Viewing | Observer polls Ryot completed-event IDs. Household confirmation audits an existing event, then optional private rating/linkage. | Ingest Jellyfin observations first, obtain household confirmation, then create one native Ryot viewing and link its rating. Preserve the legacy path for existing questions without creating a second event. |
| External deletion | A working-tree ItemRemoved receiver/verifier and Telegram Archive/Keep wanted draft now exist. It is disabled without a secret, not deployed or connected to Jellyfin. | Refactor it onto the common event/operation model before enabling. Add persistent Mini App request access, operation-specific recovery, stable webhook deployment and live commissioning. |
| Deletion after watching | Rating exists; post-rating file deletion/Keep-Archive execution does not. | Add a second entry point to the same removal operation; rating must not be required to archive, and unfinished/unwatched movies need direct access too. |
| Factual synchronization | Nine-scope read-only maintenance plus narrowly approved acquisition repair, Owned cleanup and viewing correction. | Add verified import-driven collection updates and controlled operation reconciliation; keep general technical repair in the repository. |
| Series | Sonarr read-only adapter exists; movie runtime is the implemented product surface. | Separate episode/season/show identity and intent before adding series writers. Do not reuse movie deletion semantics for one episode. |
| Curatarr marker | Requested but not implemented. | Add one neutral Curatarr collection after verified successful changes, with operation provenance in local audit. No retroactive whole-library label or policy triggered by the marker. |
| Historical watched import | Not provided by the playback webhook. | Separate reviewed import, matching and duplicate handling; baseline without a prompt flood. No implicit migration of old Jellyfin played flags. |

Working-tree verification during review: 481 unit tests passed, including 34 new removal tests. This validates offline behavior only, not a completed migration or live destructive flow. Removal draft constraints include a five-minute grace period; exact movie/provider checks; complete Jellyfin catalogue and direct absence checks; a physically readable companion file on the same library root before trusting missing-file evidence; alternate-copy and active-queue protection; durable claims; original-message callback binding; separate confirmation; and uncertain-write read-back. It still uses a specialized removal queue and does not implement playback or MarkPlayed ingestion. Native adapter compatibility and safe live operation need further commissioning. Do not generalize the draft to unsupported series or claim every edge case is solved.

#### Intake And Delivery Contract

Use one authenticated, bounded intake layer with source-specific adapters. Persist an accepted event before acknowledging receipt. Keep a fixed configured source/server identity; source payloads are evidence, not executable instructions. Do not expose credentials, raw payload paths or webhook secrets in chat/access logs. Invalid identity, conflicting namespace mappings, weak title-only matches or ambiguous records must block writes.

Normalize playback, manually marked watched, item removed, Arr import/upgrade, Arr file/entry removal and user commands into internal event types. Explicitly ignore unsupported events rather than routing them into a parser that requires nonexistent fields. Retain observation time separately from playback/watched time; receipt time must not fabricate a historical viewing date.

Correlate retries and playback updates by source event/session, exact movie/copy, account and operation identifiers. A movie ID alone is not a viewing ID; a date alone must not collapse genuine repeat watches. Correlate multiple household viewers and MarkPlayed/playback overlap before opening another question. A pre-existing watch is context, not proof a new signal is a duplicate. Where the source cannot establish whether a second viewing occurred, keep that uncertainty visible and require clarification instead of inventing another event.

Webhooks are wake-up signals, followed by fresh provider reads. They can be duplicated, reordered or lost. Support bounded retries and independent polling/reconciliation. A current played flag or LastPlayedDate cannot reconstruct every missed repeat watch; record an ingestion gap and use reviewed recovery rather than promising exact history from snapshots alone. Source outages must keep old findings/questions unverified, not clear them.

#### Reviewed User Workflows

1. **Add a new movie.** Search → select exact movie → check Ryot history/archive, Radarr presence, exclusions and identities → choose from fresh native Radarr profiles → confirm → persist operation → establish Library intent → add exactly one Radarr record with the selected profile/root and explicit search policy → verify → project active Radarr membership and Curatarr marker. A lost response is reconciled by exact identities before another add. Existing active movies open management instead of duplicate add. Previously acquired is added only after an actual verified import/file record, not on registration. Do not report a successful complete add while either service is pending.
2. **Reactivate a known movie.** Show prior viewing/deletion/archive context. Reuse the same add/profile service, with extra explicit confirmation for Archived and explicit handling of Curatarr-owned exclusion guards. Preserve Library, acquisition/deletion history and reviews. Do not clear an unrelated manually managed exclusion. Unsupported announced IDs remain pending or withdrawn, not guessed movies.
3. **Playback completion.** Jellyfin progress crosses the configured completion threshold → Curatarr holds a candidate viewing locally → household question. One No hides only that person's request; first Yes confirms for the household and clears other users' requests; all No moves the local candidate to Declined viewings without inserting a Ryot viewing. Silence stays pending. On Yes, create and verify one native Ryot viewing, then offer optional 1–5 rating and a separate Keep/Delete choice. Preserve source viewing time when known; retain unknown date honestly. Failed/uncertain creation must not silently resolve the candidate or issue a second create.
4. **Manually mark watched in Jellyfin.** MarkPlayed enters the same candidate workflow without requiring playback-position fields. Verify exact item, source account and the source's current played state. It does not automatically delete old viewings or mean “watched today.” If an old completion exists, distinguish confirming an existing fact from an intentional repeat. A checkbox cannot produce a fresh event while it remains checked; explicit repeat-watched input or playback sessions remain necessary.
5. **Manually mark unwatched.** MarkUnplayed never clears all shared Ryot history. It is at most a correction request for a specific candidate/event, particularly because other household users may have watched it. Do not expose a broad history reset as routine use; preserve the existing maintainer-only correction safeguards. Normal checkbox correction may cancel only a provably matching unconfirmed local candidate under the shared-answer policy, not a confirmed native event.
6. **Rate and retain/remove.** Rating is optional, attributed to the authenticated respondent, linked to the exact viewing and preserved through removal. Keep is the default absence of removal intent. A Keep/Delete decision must remain possible after skipping rating. Direct user deletion from Library status must also work without a completed watch, including abandoned movies.
7. **Delete in Jellyfin.** ItemRemoved → durable hint → allow indexing/upgrade settlement → revalidate exact identity, remaining versions, active downloads/imports and storage → ask Archive or Keep wanted. User-confirmed Archive removes active Arr intent to prevent re-download, preserves Library/acquisition/watches/reviews, records Archived, and records Deleted only after verified deliberate loss of a real media copy. Library-only movies need an evidence-supported branch too; lack of a Radarr record must not erase their history. Never interpret another version's removal or an unavailable mount as title-wide archive.
8. **Delete through Curatarr.** Same operation and invariants as external deletion, but an explicit preview/confirmation precedes physical deletion. Delete only the verified selected files after revalidation; account for all versions and an upgrade that arrives during the operation. Verify filesystem outcome and remaining sources, remove the Arr entry/active membership, then record final durable deletion evidence. File-only deletion while leaving a wanted Arr entry is not a completed archive. No viewing is created by abandonment/deletion.
9. **Restore / Keep wanted.** Confirm restoration intent and use the existing Radarr profile when present, or the normal profile-choice/add flow if no active entry exists. Reconcile stale file registration and separately authorize the applicable acquisition/search behavior. Preserve historical Deleted membership; hide its badge while a current copy exists. Monitoring alone does not prove restoration or an immediate search.
10. **Import and upgrade.** A verified successful first import adds factual Previously acquired and reconciles Library/active membership. Upgrades update availability/quality facts, not household watching or archive intent. An old file removal tied to a replacement must not open a whole-title delete prompt. No normal intent prompt is required for verified factual import updates. Unexpected external Arr additions/deletions require classified reconciliation, not silent acceptance of every external writer.
11. **Maintenance, manual Ryot edits and historical imports.** Existing Ryot history remains valid context, including outside-Jellyfin watches. A new direct Ryot edit/import is external evidence for reconciliation, never an instruction to acquire/delete media. Decide a supported manual-watch route through Curatarr; do not erase unrelated direct records. Technical inconsistencies surface as a compact Yes/No/incomplete System check, with diagnosis/repairs in the repository. Routine decisions stay in ordinary user use. Historical backfill is separate, scoped and does not turn every old record into a new confirmation request.
12. **Series.** Preserve show-level Library/wanted/archive intent while keeping episode identity/progress and file presence granular. Deleting one episode must not archive the whole show or clear its watch history. Sonarr continues to own season/episode monitoring and upgrade rules. Series confirmation frequency and scope need a separate design; do not activate movie shortcuts for all Sonarr events.

#### Persistence, Operation Ordering And UI

Replace the split responsibility of Ryot-first viewing observation and removal-specific execution with shared event intake, pending decisions and durable operations. Adapters translate provider APIs; workflows own policy; Telegram and Mini App invoke the same service. Keep it in the existing Python/SQLite application rather than introducing another broker or service solely for this change.

Use an operation ID, exact source/account/movie/copy identities, expected-state snapshot, saved user authorization and per-step outcome. Serialize conflicting operations on the same movie across viewing correction, rating, archive, restoration and acquisition—not just within a single worker. Ensure the asynchronous job may only modify the intended memberships. Before each consequential step revalidate fresh facts; after it verify the outcome. A job accepted response is not completion. On an uncertain response, reconcile the same operation; if its outcome cannot be proved safely, retain it for maintainer review instead of blindly repeating the mutation.

Ryot and Arr cannot form a single database transaction. Represent desired intent, work in progress and verified completion separately. The exact safe sequence depends on the action: removal may need a temporary, recorded acquisition hold while external loss is confirmed; failed completion must never restore a blanket “monitored” default. Account for queued downloads and late imports. Do not mark Deleted before file outcome is known or advertise a completed archive while an Arr entry can still reacquire it. The final confirmed durable facts belong in Ryot, while all intermediate execution/recovery evidence remains in Curatarr.

The Curatarr collection means “successfully added or updated by Curatarr,” not “all services permanently match,” “file owned,” “watched,” or permission for automatic cleanup. Add it only after verified successful work, never on browsing, receipt, a failed attempt or whole-library inference. Retain last operation/reason/time in Curatarr audit. It must not feed a Ryot-to-Arr rule. No timestamped collection per operation and no separate personal watch catalogue are needed.

Keep the Mini App small: profile choice when adding/reactivating and the approved explicit profile-edit action (D009); a common persistent request presentation for routine watch/removal input; direct keep/remove where useful; optional rating; simple System check for technical faults. Keep settings limited to the current name/verified automation choices and useful integration health, not a workflow-builder or repair console. Whether the existing Viewing inbox label becomes a broader Requests entry is a presentation decision, not permission for extra counters/tabs. All requests survive Telegram cleanup. The current 36-hour cleanup implementation covers viewing prompts only; extending it to other prompt types requires explicit implementation and testing. Uncertain sends must not be automatically duplicated. A failed message must not lose the underlying decision.

#### Live Integration Findings And Cutover Recommendation

Historical read-only review snapshot, 2026-10-24, before coordinated acquisition/profile/provenance and direct-intake implementation. Use F06–F10 and F19/F25 in the feature register for current capability and activation; these observations are not a current integration inventory.

Read-only configuration review on 2026-10-24 found:

- Jellyfin → Ryot sink is currently **enabled**, min5/max95, with six recorded missing-position errors, newest at 2026-10-24 06:51:44 UTC. Error history alone does not establish a new failure after correction. Jellyfin now sends only Play/Pause/Resume/Stop/Progress, Default format, all users. The receiving integration was re-enabled by the user's configuration work; do not incorrectly report it as still paused.
- Ryot → Radarr push is **enabled**, watching the native **Custom** collection. It is a competing acquisition path even though it is not the requested Curatarr marker.
- Ryot → Sonarr push is disabled. No active Sonarr import lists were returned.
- Radarr has enabled automatic IMDbListImport and TraktPopularImport integrations. IMDb has searchOnAdd=true; the Trakt integration has searchOnAdd=false. A subsequent settings inspection resolves its selected feed as Top Watched Movies by Month (rating60–100, years2015–2030, limit5), rather than assuming Popular from the class name. List sync can add titles outside Curatarr even when search on add is off. These paths require an explicit retain-as-input or disable decision, not an implicit configuration change during this review.
- Curatarr's new removal webhook has no configured token and no Jellyfin hook, so it is not active. Playback ingestion, MarkPlayed, actual add/profile execution and Curatarr marker remain unimplemented.

Recommended rollout:

1. Agree the authority boundaries and input policy. Inventory all acquisition/watch writers, including native Arr lists, other apps and future Trakt imports. Decide whether each is an approved Curatarr input, execution-internal automation or a bypass to retire. Keep movie scope first; design Sonarr separately.
2. Build the common event/decision/operation services and stable authenticated webhook endpoint. Add observation-only intake and health with durable backups; retain current production writers during read-only comparison. A temporary development tunnel is not a reliable long-term webhook address. Account for downtime/delivery gaps explicitly.
3. Implement and prove both playback and MarkPlayed confirmation-before-write, optional rating, deduplication/repeat-watch behavior and legacy question compatibility. Integrate the disabled removal draft into the shared executor; do not enable parallel independent mutation paths.
4. Deliver the requested add/profile button and Curatarr marker using that executor. These are user priorities, but neither may bypass identity, archive/exclusion checks, recovery or verified two-service outcomes. Complete existing cleanup first where needed; do not repeat historical imports or relabel the library.
5. Commission targeted synthetic/offline failures, then genuine authorized live movie workflows: playback, mark-watched, rating skip, one No/one Yes/all No, repeated watch, duplicates, upgrade/extra version, deliberate deletion, interrupted write and restoration. Verify unchanged unrelated records and restarts; do not fake household watch history or delete a real movie merely to test readiness.
6. Perform one controlled cutover: record the boundary/cursors/baseline, pause the Ryot Jellyfin sink before Curatarr native watch writing becomes active, retire/re-route Ryot Arr pushes and conflicting native lists as agreed, and verify only one coordinator writes. Do not leave both watch paths active. Reuse existing pending legacy questions without recreating their Ryot events. Do not replay already committed effects after restart.
7. Retain the paused old integration configuration for rollback initially, then delete only its configuration after acceptance. Rollback disables new writers first, reconciles uncertain operations/event overlap, then restores the old path; it is not an excuse to erase confirmed data or blindly replay buffered events. Keep the old provider behavior and version compatibility documented in adapters. Upstream Ryot improvements may remain useful, but do not maintain two simultaneous policy owners.

This review changes the next design priority from a standalone removal hook to a unified Curatarr coordinator. It does not authorize bulk watched-history import, retrospective marker backfill, live integration changes, deployment or media deletion during review. Current cleanup data and stable runtime remain preserved.


### Major Development Planning — Thresholds And Discovery Intake

The user requested analysis and the unanswered product questions before one larger coordinated development effort. This is planning, not authorization to start the expanded implementation or change live integrations. It extends the sole-coordinator review above. The desired scope includes configurable started/completed playback percentages and Curatarr-owned movie-list intake; Radarr should execute approved additions rather than automatically download list contents.

#### Settled Requirements And Proposed Model

- Preserve the existing catalogue, viewings, reviews and completed cleanup. This is an application/workflow upgrade, not a request to recreate databases.
- Curatarr receives observations and authenticated user decisions; Ryot retains durable confirmed facts. Keep native Arr release selection/import/upgrade execution rather than rebuilding it.
- Playback thresholds are configurable started/completed percentages with validation `0 <= started < completed <= 100`. Carry forward the currently chosen5/95 as the initial values; both remain admin-editable. Evaluate verified movie playback position/runtime, not download progress. Completion creates a candidate under the chosen confirmation policy, never automatic household truth merely because the player reached a percentage. Repeated progress updates, seeks, resumes and out-of-order messages must not multiply candidates. Threshold changes must not retroactively create a historical prompt flood. Manually marking watched has no playback percentage and needs its separately agreed policy.
- Distinguish observation, household confirmation, rating and retention. These are independent; ratings and completed-watch flags are not prerequisites for deleting an abandoned movie.
- Separate discovery collection from notification. Background source checks can run daily while a household selects on-demand review or a configurable digest period. Notifications-off applies to discovery only, not watch/removal questions or necessary integration health.
- This paragraph concerns unapproved discovery sources; the clarified personal wanted-list policy below is different. A discovery candidate is not yet a Ryot Library entry or Radarr request. Store proposals, source provenance, genre/identity metadata and decisions in Curatarr. Add to Ryot/Arr only after the agreed Yes action and profile confirmation.
- Merge identical movies across all feeds by verified typed identity and record all contributing sources. Re-check current Ryot/Radarr state before display/approval. Metadata identity failures go into a deferred state; title matching cannot authorize an add.
- Keep eligible pending candidates until Yes/No. A source dropping a title, refreshing its rank, becoming unavailable, or losing a genre field must not erase the candidate or manufacture a decision. Filtering is separate from an explicit No, so a later filter change can restore eligibility without undoing deliberate household rejection.
- Keep intake bounds, API pagination/cache/backoff and presentation caps separate. A digest should be one compact notification, not one poster message per queued movie. Limit each automatic presentation; manual Continue can expose more without expiring the rest. No repeated notification of unchanged backlog by default.
- Proposed unique/new policy: suppress automatic proposals for exact movies already in the household catalogue, active in Arr, historically watched, archived, explicitly declined, accepted or already pending from another source. Manual search can override a discovery rejection or enter the existing reactivation flow. No does not add Archived or delete an existing record.
- Proposed genre policy: exclude an automatic recommendation if any of its verified genres is blocked; do not block deliberate manual searches. Missing genre metadata defers recommendation rather than assuming the movie is allowed. Preserve the stored reason for exclusion.
- List removal, source filters or a later No must never delete movies previously approved and acquired. Disabling native Radarr lists at cutover means disabling their future automatic intake, not removing existing movie records.

Source inspection refines the earlier generic integration name: the existing `TraktPopularImport` implementation is actually configured as **Top Watched Movies by Month**, rating60–100, years2015–2030, limit5, no genre restriction. Its searchOnAdd=false is not an approval queue. The other configured integration is IMDbListImport. Preserve these source preferences unless changed by the answers below; do not mistake a five-result poll window for five proposals ever or a limit on persistent pending retention.

Direct source support needs a technical feasibility check, not an assumption that every Radarr importer has a stable public equivalent. [Trakt's official API reference](https://trakt.docs.apiary.io/) documents discovery and list endpoints; its [app setup](https://developer.trakt.tv/docs/create-an-app) requires a provider application/connection. Do not copy another application's OAuth tokens as portable Curatarr credentials. [IMDb's official list help](https://help.imdb.com/article/imdb/track-movies-tv/faq-for-the-list-feature/GNQMN47VZSE7KW38) documents user exports, which does not establish an unattended list API. Validate the exact configured IMDb source; offer a supported Trakt mirror or IMDb export import if reliable automatic access is unavailable. This is an unresolved adapter capability, not a promise to implement arbitrary website scraping.

#### Clarification: Personal Wanted Lists Versus Discovery Feeds

The user clarified that the existing personal IMDb list already expresses wanted intent. It must not pass through another discovery Yes/No review. A new eligible exact movie from that trusted source may enter the normal add operation using an agreed source/default Radarr profile. Missing profile or identity configuration, archived history or uncertain operations still require the relevant validation/decision; “trusted” does not bypass safety or authorize repeated downloads. Discovery genre exclusions must not silently veto a movie deliberately selected on the personal wanted list.

Treat the two source kinds separately:

| Source kind | Meaning of a new eligible item | Policy |
| --- | --- | --- |
| Personal wanted list | An explicit user acquisition request already made outside Curatarr. | Do not ask whether it is wanted again. Resolve identity and configuration, then carry out one verified add operation. |
| Discovery feed | A suggestion which has not been approved. | Persist as a unique candidate, apply discovery filters and wait for the shared Yes/No decision. No automatic acquisition. |

A wanted-list entry must not be modeled as a continuously asserted instruction to download whenever no file is found. Persist the originating wanted request and its processing/outcome, and evaluate it against current durable household intent. Once the movie is deliberately archived after watching or abandonment, its old wanted-list presence cannot reverse Archived or create another add. An explicit new reactivation decision is required. Keep the entry/decision association across restarts, polling, source disable/re-enable and migrations. Before a first import, reconcile existing list items with Ryot history/archive and active Arr state; do not interpret every pre-existing list item as a new request. Watched alone is not archived, and temporary file absence is not permission to reacquire a deliberately archived movie.

Arr exclusions can remain a derived defense for a native source path still active during migration, but Curatarr's durable request/intent history must be sufficient to prevent its own re-adds. Do not use a disconnected duplicate exclusion list as the only memory of household decisions. Do not automatically delete from or rewrite the personal IMDb list merely to make these semantics work. Whether removing and later re-adding an archived movie to that source constitutes a new explicit reactivation request remains a policy to confirm; the safe initial behavior is to request reactivation confirmation.

The Trakt source was an unsuccessful discovery experiment, not an obligation to retain Trakt or that particular feed. The currently selected monthly most-watched feed measures audience activity; it is not intrinsically a feed of recent, well-rated releases. [Trakt's official endpoint descriptions](https://trakt.docs.apiary.io/reference/movies) distinguish most-watched, popular, trending and anticipated. A recommended alternative to evaluate is Curatarr discovery using [TMDB Discover](https://developer.themoviedb.org/reference/discover-movie), combining a recent release window, rating threshold, minimum vote count and genre exclusions, followed by the same persistent review queue and quiet presentation policy. This is a recommendation, not a selected replacement or proof of personalised recommendation quality. Candidate defaults for discussion are a rolling12–18month release window, rating at least7/10 and at least100votes, with threshold tuning after sample review; these are product preferences, not guarantees that a movie suits the household. Avoid making all-new releases disappear merely because they have not accumulated enough votes; deferred candidates can become eligible later. Existing exact identities, wanted/archived/watched records, discovery No and pending decisions still suppress inappropriate repeated proposals.

This clarification narrows Q09 to selecting a discovery replacement, Q12 to discovery Yes behavior, Q13 to discovery rejection only, and Q16/source configuration to trusted-list execution profile/search defaults. The personal IMDb list itself needs no additional wanted approval. The user subsequently supplied the answers directly; see Answered Build Contract below, including the historical IMDb baseline exception.

#### Answered Build Contract — 2026-10-24

The user answered Q01–Q22 directly, specifying that Yes accepts the recommended default. These answers supersede the original unanswered status and the older general assumption that every list needs discovery review. They establish product scope for the planned coordinated build; implementation, migrations and production deployment remain separate execution steps. Q19 was subsequently explained and explicitly accepted: restoration occurs only after Restore confirmation, reusing the existing profile or offering selection if no Arr entry exists.

| Question | Answer / resulting build contract |
| --- | --- |
| Q01 | Accepted. Complete movie workflows; Sonarr remains observation/monitoring until series-specific behavior is scoped. |
| Q02 | Accepted. Movie completion threshold initially95%; present confirmation after playback stops, with bounded inactivity fallback. Keep minimum/start threshold initially5%, configurable with the completion threshold. |
| Q03 | Accepted with restriction: movie watch-in-progress is not a relevant product feature. No Ryot partial movie viewing, progress UI, progress prompt or persistent household “started movie” task. Retain only technical session evidence needed for threshold/correlation/recovery. TV progress may be useful when series scope is implemented later. |
| Q04 | Accepted. Manual Jellyfin MarkPlayed goes through household watched confirmation, just like detected completion; it is not direct authority to insert a new Ryot viewing. |
| Q05 | Accepted. Preserve an existing Ryot viewing when manual played changes merely restate an existing fact; require explicit repeat-watch intent rather than manufacture another event. Genuine later playback can still establish a repeat candidate. |
| Q06 | Accepted. Prepare a separate reviewed exact-ID historical Jellyfin/Ryot reconciliation/import, preserving credible dates and uncertainty, no prompt flood or blind replays. API commissioning batch execution still requires concrete preview/verification. |
| Q07 | Accepted: explicit Jellyfin household account whitelist; future/guest accounts are not automatically included. The general Yes did not specify admin's playback role. Conservative initial mapping is erje as playback input, admin maintenance-only, configurable and to be checked before enabling live ingestion. Do not represent that account distinction as explicitly answered. |
| Q08 | Accepted. Configured trusted household users may approve/reject discovery, add/archive/restore with explicit consequential confirmation. Admins control configuration/sources and technical repairs. Discovery Yes/No is shared; viewing No remains per respondent until firstYes or unanimousNo. |
| Q09 | Accepted. Use filtered TMDB Discover as the initial discovery direction, with editable recent release window, rating/vote thresholds and genres. Proposed initial window can be18months within the discussed12–18month range, rating>=7/10, votes>=100. Personal IMDb remains a separate already-approved wanted source. This does not guarantee taste or automatically acquire discoveries. |
| Q10 | Accepted fallback, but user notes IMDb is a simple public list. First test the exact public source directly; only use a Trakt mirror/export fallback if unattended direct access proves unsuitable. No assumption of private-list authorization or needlessly requiring a mirror. |
| Q11 | Explicit exception for existing personal IMDb contents. All existing entries were already processed by the previous setup; none may activate a new download at migration. Entries absent from Radarr are treated as handled/removed/unwanted for re-add prevention. Optionally register exact missing items as Ryot Library-only after preview; no acquisition, automatic reactivation, fabricated viewing or inferred deletion. Existing Ryot facts remain intact. First-fetch discovery recommendations still use the silent eligible-candidate queue default. |
| Q12 | Accepted, with required Catalogue only choice. Show it as a distinct acquisition-intent action rather than pretending it is a native Radarr quality profile. Catalogue only establishes Ryot Library/provenance without adding a Radarr entry or starting a search. Profile cancellation leaves a discovery candidate pending. If changing an already active/partially applied acquisition to catalogue-only, revalidate and use the controlled cancellation/retention workflow; never silently delete files or erase history. |
| Q13 | Accepted. DiscoveryNo suppresses repeated automatic proposals across sources; manual reconsideration remains possible. No does not write RyotArchived. This is separate from trusted personal-list intent and the protected historical baseline. |
| Q14 | Accepted. Provide the movie genre list as selectable exclusions in Settings. Any excluded genre suppresses discovery; deliberate IMDb wanted/manual searches bypass discovery filters. No actual genres were chosen in the answers: start with none excluded until configured, not a guessed taste profile. Missing genre metadata remains deferred. |
| Q15 | Accepted. Default discovery notifications off, collection daily, on-demand review. Optional user-configurable digest period/cap, initiallyweekly/max5new suggestions, manualpage10withContinue. Persist unanswered candidates; do not repeatedly announce unchanged backlog. Discovery silence does not suppress watched/removal workflows. |
| Q16 | Accepted. Show valid configured profile preselection and explicit Add and search final confirmation. Issue one recoverable search; no hidden search before confirmation or repeated search after an uncertain command. Catalogue only remains available. |
| Q17 | Accepted. Keep is implicit after watching/rating, with optional Delete. No compulsory extra Keep task. Movie deletion is also accessible for unfinished/unwatched titles. |
| Q18 | Accepted. Start with all verified managed movie copies, with explicit versions/count preview and confirmation. Do not delete unrelated files; remaining alternate copies block a false title-wide archive. |
| Q19 | Accepted after explanation. Restore requires explicit user confirmation, reuses the existing Radarr profile and initiates one recoverable restoration search. If no Radarr entry exists, offer normal profile selection/add first. No automatic restoration. Preserve viewing, reviews and historical acquisition/deletion facts. |
| Q20 | Accepted. Include persistent announced/awaiting-metadata requests identified by IMDb, keep-waiting/withdraw, no fake TMDB/Ryot records or automatic acquisition. Old historical IMDb baseline entries do not become new acquisition requests through this queue. |
| Q21 | Deployment target selected: Curatarr in its own Docker container on the existing media-services host alongside Arr containers. Exact host name belongs in private deployment/handover material. Persistent volumes, provider connectivity, stable webhook/HTTPS routing and one poller per token must be designed/tested. Do not continue treating production target as unspecified; routing detail remains to be resolved for deployment. |
| Q22 | Accepted with explicit hold. A final shared destination group already exists, but retain the existing test destination until a good working copy has been tested. No group move, new subscriptions or assumptions about ORION. Record destination mappings privately during explicit cutover. |

IMDb initial baseline is especially important: record an exact source membership snapshot and durable handled-request keys before enabling source processing. Do not consume a new “first snapshot” after every restart or source reconnect. Absence from Radarr on this approved historical list is enough to suppress automatic re-add, but is not by itself verified file deletion or a reason to fabricate Archived/Deleted history. Preserve existing user Library/acquisition/viewing/review facts. A genuinely new list entry after baseline is already wanted and needs no redundant Yes/No discovery review, but must still pass exact identity/archive/operation guards and the agreed profile policy. Removing/re-adding an already handled/archived entry must not evade those guards.

For Catalogue only, distinguish initial registration, cancellation before execution, and changes after native effects. An initial catalogue-only choice does not add Radarr, monitoring, downloaded/acquired, Archived or Deleted facts. After a native add/search has begun, the UI cannot relabel an incomplete cross-service operation as a successful catalogue-only outcome; it needs a freshly validated recovery/cancellation path. Do not equate catalogue-only with deliberate permanent rejection or an archive.

All Q01–Q22 product answers are now recorded, including the subsequent Q19 confirmation. The account whitelist detail remains a conservative deployment assumption requiring verification rather than a reason to re-ask all accepted defaults. Genre choices, digest cadence, profile defaults and source settings should be editable at setup rather than requiring all values in chat now. The exact external HTTPS address and storage/volume bindings are deployment design details still to establish on the selected host.


#### Questions Before The Larger Build

Historical questionnaire; answered by the Build Contract above. Do not treat these rows as unanswered implementation blockers.

The table retains the original questions and recommended defaults for reference. The Answered Build Contract above is now authoritative for accepted answers and exceptions, including the subsequently accepted Q19. The existing user-defined Settings name, shared-household history policy, optional 1–5 rating, first-Yes watch semantics, 36-hour viewing-message cleanup toggles, ignored native Owned meaning and maintainer-only technical repairs are settled and need not be asked again.

| ID | Question | Recommended default / reason |
| --- | --- | --- |
| Q01 | Does the larger release cover complete movie workflows with Sonarr read-only monitoring, or full series acquisition/watch/deletion too? | Complete movies first; series observation only until episode/season/show decisions are designed. One coordinated release does not require unsafe movie rules for series. |
| Q02 | After reaching the completed threshold, ask immediately or wait until playback stops? | Keep5%/95% as the existing initial settings. Present the question after stop, with a bounded inactivity fallback if stop is missed, to avoid interrupting the end of the movie. |
| Q03 | Should started/in-progress playback be stored only in Curatarr, or also written as Ryot progress before household confirmation? | Curatarr only initially; write confirmed completion to Ryot. This avoids unconfirmed shared native history and progress side effects. |
| Q04 | Does an authorized user manually marking watched in Jellyfin count as explicit confirmation, or should Curatarr ask Yes/No again? | Keep the same confirmation initially for consistency. If direct confirmation is preferred, retain duplicate/date safeguards and optional rating/removal; the checkbox cannot declare a new repeat watch unambiguously. |
| Q05 | When manual MarkPlayed meets existing Ryot viewing history, should it preserve that existing fact unless an explicit repeat watch is chosen? | Yes; never create another viewing merely because a played flag changed. Real later playback sessions can still produce genuine repeat-watch candidates. |
| Q06 | Should commissioning reconcile existing Jellyfin watched flags with Ryot, or start only with future events? | Prepare a separate exact-ID preview/import of genuinely missing historical facts, preserve dates where credible, leave uncertainty explicit and avoid normal confirmation floods. No blind replay or inferred repeat counts. |
| Q07 | Which Jellyfin accounts may create household viewing inputs? | Whitelist household accounts explicitly; current observed accounts are erje and admin. Decide whether admin is household playback or maintenance-only. Future/guest accounts are not automatically trusted. |
| Q08 | May every configured household user approve/reject discovery and add/archive/restore movies, with source/settings controls restricted to admins? | Yes for trusted household users, with explicit removal confirmation; admins control settings/sources and technical repairs. Discovery Yes/No is a shared final decision with actor recorded; watch No retains its separate per-respondent semantics. |
| Q09 | Which discovery source should replace or supplement the unsatisfactory Trakt experiment? | Evaluate a filtered TMDB Discover feed with recent release dates, rating/vote-count thresholds and genre exclusions. The personal IMDb list is trusted wanted input, not a discovery source requiring Yes/No approval. |
| Q10 | If unattended IMDb access is unavailable, is a Trakt mirror or IMDb export import acceptable for the first release? | Yes; do not let an unreliable importer undermine the whole migration. The technical team must first check the actual source, not make the user diagnose API details. |
| Q11 | On the first successful list fetch, queue its current eligible movies silently, or establish a baseline and offer only later discoveries? | Queue current eligible movies silently, with the same unique/filter rules and presentation cap. Keep all accepted pending candidates; do not flood chat. |
| Q12 | For discovery suggestions only, does Yes mean Library-only, or proceed to Library+Radarr acquisition with a chosen profile? | Open the standard add/profile confirmation. Only the final confirmed action permits adding/searching in Radarr; canceling profile selection leaves the suggestion pending. If catalogue-only is desired it needs an explicit supported choice. |
| Q13 | Should No permanently suppress that movie from all automatic list suggestions, while allowing manual search/reconsideration? | Yes; list rank/source changes must not repeatedly offer a rejected movie. Store it as a discovery decision, not Ryot Archived. |
| Q14 | Which movie genres should be excluded, and is matching any excluded genre enough? | User supplies genres; any matching genre suppresses discovery, manual search remains available. Excluded candidates are retained separately from explicit No and can be re-evaluated when filters change. |
| Q15 | Start discovery in manual-review mode, or enable a digest? If a digest, what interval and maximum number of proposals per presentation? | Default no notifications; collect daily. Optional one weekly digest, at most5 new proposals, with10 per manually opened page and explicit Continue. All unanswered proposals remain available; no repeated backlog reminders. User can set a different cadence/cap. |
| Q16 | After profile confirmation, start one search immediately or only register/monitor the movie for normal Radarr acquisition? | Offer explicit Add and search, with the still-valid configured default profile preselected and editable. Never search before the final confirmation; reconcile uncertain submissions before issuing another command. |
| Q17 | After watch confirmation/rating, require a Keep/Delete answer, or leave Keep as the default with a Delete action available? | Keep by default; optional Delete without another mandatory unanswered task. Deletion also remains accessible for unfinished/unwatched movies. |
| Q18 | For Delete through Curatarr, remove all verified managed versions of the movie or allow a selected-version operation? | Start with an explicit all-managed-copies preview/confirmation; never touch unrelated files. A remaining alternate copy prevents falsely completing a title-wide archive. Selected-version management can be separate scope. |
| Q19 | For Keep wanted after external deletion, should Curatarr restore using the existing profile and initiate one verified search, with profile selection when no Arr record exists? | Yes, after explicit restoration confirmation. Preserve historical Deleted and acquisition/viewing facts. Prevent duplicate searches and never pretend an absent/unavailable source proves success. |
| Q20 | Include announced projects lacking a supported TMDB movie record in this release, or defer? | Include a small persistent awaiting-metadata request identified by IMDb, with keep-waiting/withdraw; no fake Ryot movie or automatic acquisition. Actual user-facing design must stay minimal. |
| Q21 | Where will the always-on coordinator run, and what stable HTTPS address will Jellyfin/Telegram reach? | Establish a persistent server/container target and stable routing before cutover. Current Mac plus temporary tunnel remains development, not a reliable unattended event receiver. No production host, domain or deployment approval is inferred. |
| Q22 | At launch should the existing private test destination remain, or should routine prompts/digests move to a real shared household group? | Verify a shared group for multi-user daily use; configure explicitly, preserve pending decisions and do not silently move to ORION. Keep discovery-only silence separate from general workflow delivery. |

#### Developer-Owned Analysis And Readiness Work

The user should not have to choose internal locking, queue schema, retry algorithms or provider mutation details. Development must resolve these before enabling broad writes:

- A common persistent normalized event inbox, source/account identity, replay-safe operation journal and per-movie conflict protection across add/reactivate, viewing/rating/correction and removal/restore. Keep chat and Mini App thin clients of the same services. The disabled removal draft is reusable input, not a parallel policy owner to deploy unchanged.
- Source-specific playback/session correlation, provenance, threshold settings, explicit-mark semantics and time handling. Native watch creation needs an uncertain-outcome recovery design; merely retrying a GraphQL create risks duplicate viewings. A legacy Ryot-first question must never be replayed as a new watch.
- A discovery store that is separate from Library and preserves pending/declined/filtered/deferred/accepted-operation states across source churn, filter edits, app restarts, notifications-off and cancellation. Separate a presentation cursor from decisions; scrolling or displaying a card must not consume it.
- A bounded scheduler with cached provider reads, full pagination within configured source depth, exponential backoff/rate handling, source health and no notification amplification. Accurate new/pending counts and one compact digest, no silent expiry/drop of eligible unanswered candidates.
- Native API compatibility verification, including Ryot cached reads/asynchronous jobs/native collection effects, Radarr profiles/root/exclusions and physical-file validation on the actual server. Historical data and live unrelated state must remain preserved.
- One Curatarr marker applied after successful verified writes, never used as acquisition policy or evidence that every service is permanently synchronized. Separate failed/partial status in the local journal rather than another set of lifecycle collections.
- Durable deployment/backup/restore, secret handling, webhook authentication/body bounds and access-log redaction; explicit reporting of missed-event windows. Ordinary snapshots cannot reconstruct every repeat watch missed while Curatarr was offline.
- Focused failure tests, full regressions, mobile/desktop UI checks and genuine authorized live acceptance. No real media deletion or invented household watch is an implicit test case. A complete coordinated release should still be developed/tested in internal slices and deployed with staged writer cutover and rollback.
- A reviewed migration that preserves existing movies/history, reconciles known cleanup state, establishes event/list baselines, retires native Radarr list intake and Ryot cross-service writers only at cutover, and checks that only one coordinator writes each fact. Migration/config retirement requires its own execution scope; this questions document does not perform it.

Use the Answered Build Contract to produce one build specification with explicit included/deferred workflows, acceptance cases and migration plan. Implement the accepted Q19 only as an explicitly confirmed restoration operation. Do not restart into the draft or enable live acquisition/deletion merely because product answers are recorded.


### Coordinated Movie Release — Build Specification

This specification translates the Answered Build Contract into an implementation target. The user requested the specification and implementation plan after settling Q19. It is not a claim that the coordinated release exists or permission to change live infrastructure during specification work. It supersedes earlier standalone-hook rollout proposals; preserve the current working tree and deployed data. The corresponding work packages, gates and execution order are owned by maintainer/WORKFLOW.md.

#### Release Scope And Non-Negotiable Outcomes

The release delivers complete movie acquisition/catalogue-only, direct Jellyfin viewing intake, household confirmation/rating, controlled removal/restoration, trusted personal-list intake, reviewed discovery, announced-project requests, Curatarr provenance and minimal common user interfaces. Sonarr remains a read-only observation boundary. Existing cinema/Trakt/native history, ratings, Library/acquisition/deletion facts and completed cleanup are retained. There is no database reset, retrospective marker stamp or blanket removal of legacy entries.

After cutover, Curatarr is the single policy owner for these movie operations. Ryot persists confirmed household meaning through its supported APIs; Radarr retains its native acquisition and release-selection behavior for approved active movies. Direct external changes remain detectable evidence and do not silently override explicit archive intent. Ordinary technical repair remains repository work rather than a Mini App repair catalogue.

#### Shared Execution Model

Implement these boundaries inside the existing Python application:

1. **Source adapters:** authenticated Jellyfin/Radarr webhook parsing; trusted source polling; metadata/identity resolution; Ryot/Radarr/Sonarr API compatibility. Parsers accept only documented supported event kinds. Machine authentication is separate from signed Telegram user authentication; accepted events are durably committed before acknowledgment. Bound request size and source/account identity; never execute paths or URLs supplied as instructions in a payload.
2. **Event journal:** stable source identity, source event/session/copy keys, observation/effective times, payload version and bounded sanitized evidence. Persist received/validated/deferred/processed status, correlation and error class. Duplicate messages can be acknowledged without repeating their effects. Do not promise every repeat watch can be reconstructed from a latest-played snapshot after an outage.
3. **Decision service:** one versioned pending request shared by chat and Mini App. Record the intended movie, snapshot, user/role, applicable choices and household response state. Display/navigation is not a decision. A selected rating/profile is not provider mutation until final confirmation. Requests survive Telegram expiry/deletion.
4. **Operation service:** persist authorization/intent and a step plan before provider writes. Serialize conflicting work per logical movie across acquisition, viewing creation/rating/correction, removal and restoration. Keep desired intent, currently running steps and verified provider outcomes distinct. Use durable leases/fencing and revision checks; do not rely on a process-local lock alone.
5. **Provider writers:** narrow named actions with exact identities, account/collection ownership checks, protected-state snapshots and fresh read-back. Job acceptance is not completion. For uncertain creates, reconcile the same operation against the original and current state; if attribution cannot be proved, retain a blocked operation for investigation rather than create again. No distributed transaction or blanket exactly-once guarantee is assumed.
6. **Scheduler and delivery:** bounded workers, persistent cursors, retry/backoff, rate limits, source health and a delivery outbox. Telegram delivery claims are saved before sending; ambiguous sends are not blindly duplicated. A stopped destination pauses notifications without destroying pending work. An outage never proves absence or user intent.

The local SQLite journal is essential application data alongside Ryot. Backups/restore must retain source baselines, decisions, uncertain native outcomes and Telegram IDs. Reconciliation reads complete snapshots where absence is needed; cached/partial responses cannot clear unresolved work. Settings changes and provider-version changes must not reinterpret already-authorized operations silently.

#### Persistence And Migration Contract

Retain schema3 tables and current pending decisions, viewing-event ledger, binding, cursors, audit, settings and confirmed mappings. Use an additive, versioned next schema migration, tested against copies of real schema3 shape and fixtures. The migration must be transactional, repeatable and reject unsupported newer schemas. Do not downgrade an old executable against the migrated database. Final SQL/index details follow implementation, but these logical records are required:

| Logical record | Required purpose and uniqueness |
| --- | --- |
| Source registration / baseline | Stable source kind/identity, configuration revision, bootstrap state, poll health, external account scope. Renaming/reconnecting a source must not reset historical IMDb handling. |
| Event inbox / correlation | Source occurrence/session identity, exact movie/copy, observation time, effective time and processing status; deduplicate repeated deliveries without collapsing genuine repeat viewings. |
| Operation / step journal | Unique operation ID, logical movie, request revision, actor/source authorization, intended outcomes, expected-state snapshots, lease and per-step attempted/accepted/verified/uncertain/blocked outcomes. |
| Discovery candidate / source links | One logical candidate per resolved movie, multiple source provenance records, genre/eligibility metadata and persistent pending/declined/deferred/filtered/accepted-operation/fulfilled state. |
| Trusted wanted request | Stable source+movie occurrence/history and handled result. Preserve historical IMDb suppression independently of whether its source stays enabled or the movie file exists. |
| Delivery / digest cursor | Destination/bot identity, exact message/operation reference, attempted versus known sent state, presentation revision and per-candidate digest inclusion. Displaying a card cannot consume Yes/No. |

Reuse existing tables where their invariants fit; add dedicated records where shoehorning operations into unrelated viewing JSON would weaken uniqueness or recovery. Do not store personal watched catalogues: household input actors and their temporary votes are workflow provenance only.

#### Acquisition, Catalogue Only And Provenance

User search, accepted discovery, explicit reactivation and genuinely new trusted IMDb wanted entries feed the same acquisition service. Their authorization differs; their identity, profile, root, exclusion and recovery checks do not.

- **Add and search:** show history/archive/current Arr facts and current native profile choices. Preselect the configured profile only if still valid. Bind the final confirmation to movie identity, settings/profile revision and expected state. Persist Library intent, add one exact Radarr entry, verify the resulting profile/root/monitoring and active membership, then issue and verify one search under the saved authorization. If add succeeded but a later step failed, keep an intelligible pending result rather than silently adding another entry. A search command accepted is not a successful download.
- **Catalogue only:** establish exact Ryot Library membership and successful Curatarr provenance without adding Radarr, monitoring, search, Previously acquired, Archived or Deleted facts. It is a separate action, not a synthetic quality profile. A canceled profile dialog leaves a proposal pending. An already-started native operation cannot be declared undone by changing a local option; cancellation/conversion needs current-state checks, and acquired files require the ordinary retention/removal decision.
- **Known/active/archived cases:** no duplicate add. Show management for active movies; show previous history for reactivation; require explicit confirmation for Archived and handle only Curatarr-owned exclusion guards. Do not clear unrelated native exclusions. An old trusted-list item cannot reactivate an archived title.
- **Curatarr collection:** ensure one account-owned custom collection with a distinct configured name, initially Curatarr. Mark only successful verified additions/updates. Preserve every unrelated collection/history/review. Do not mark browsing, a failed request, the whole baseline or receipt of an event. Store last action/time/source locally; the collection is provenance, not lifecycle or a feed into Ryot-to-Arr automation. Marker repair can remain pending without repeating an already successful primary operation.

Existing-movie profile editing is implemented separately from Add: Library status → Radarr → Change profile. Actor-bound, ten-minute versioned choices include the current native profile; confirmation requires a different offered target. Shared movie reservations and native/catalogue snapshots fence changes. The native movie-editor request contains only movieIds and qualityProfileId; native read-back verifies the result while monitoring, root/path, tags, files and Ryot history/reviews are preserved. No search is requested. Lost responses recover the same durable operation rather than blindly repeat a write. Provenance follows verified success. See F10 / U32; genuine native-save acceptance remains pending.

For trusted new IMDb entries, the list addition already supplies wanted authorization; no redundant discovery Yes/No is required. Enable this source with an explicit default profile/search policy so missing configuration becomes a single setup problem rather than silent acquisition or repeated unwanted questions. A discoveryNo does not veto an explicit later wanted request, but existing archive/history/operation safeguards still apply.

Manual catalogue registration optionally includes an explicit past watched fact and /5 rating. This is separate from playback ingestion: never manufacture a Jellyfin event. The catalogue journal reserves the exact movie through registration, completion creation/reuse, optional private review creation/linking, observer-ledger association and provenance. Unknown dates stay unknown; an entered calendar date is explicit user evidence, not a measured playback timestamp. Preserve other watches/reviews and reject conflicting existing date/rating rather than overwriting. Existing completion is reused, not multiplied. Source wanted/discovery policies do not silently authorize past-viewing input. The minimal UI exposes two Add choices and a single final confirmation while retaining the internal immutable preview/fresh-state guard.

#### Viewing And Rating

Historical migration clarification (2026-10-25): the user confirms Jellyfin was commissioned after Ryot and its watches represent additional history. Historical import must append attributable Jellyfin occurrences even for movies already watched in Ryot/Trakt, preserving those older events. Idempotency applies to the same source occurrence/import, not to the movie or the mere existence of a completion. Reconcile source occurrence identity against any already-imported integration events; retain the agreed household account scope/coalescing rules. A PlayCount and latest date are not a full occurrence history: inspect available historical evidence before reconstructing repeats, and never invent dates. Per-occurrence preview/authorization, backups, journal/read-back and observer coordination remain required. This migration rule is separate from the manual catalogue form, which still reuses an existing completed fact unless a distinct repeat is explicitly recorded through an appropriate workflow.

Accept Play/Pause/Resume/Stop/Progress and explicit MarkPlayed through Curatarr's parser. MarkUnplayed is correction evidence, not permission to delete shared history. ItemRemoved is routed to the separate removal policy and never into a parser requiring playback position. Normalize data from configured household account IDs, initially conservative playback input only; configure the maintenance account's role before live intake.

Use configurable5/95 minimum/completion percentages with strict bounds and per-session evaluation. Movies have no public started/in-progress feature and no unconfirmed Ryot progress history. Retain technical evidence only as needed for reliable intake/recovery. Reaching completion arms a candidate; present it after stop or a bounded inactivity fallback. A resumed session, repeated progress signal, seek or out-of-order event must not multiply questions. Preserve the known viewing time; an unknown historical date stays unknown instead of becoming receipt date. Do not retroactively generate questions because thresholds change.

New candidate → shared household request → firstYes or unanimousNo. OneNo hides only that user's request; silence is pending; firstYes clears other inboxes and authorizes one native household viewing. Native creation/read-back and the local event link must complete before optional rating can target it. UnanimousNo moves the local candidate to Declined viewings without creating a Ryot viewing. An admin later confirming it enters that same native-create operation. Legacy requests whose Ryot event already exists stay explicitly tagged as legacy; confirming them does not create another event. Observe Curatarr-created native events without issuing a second follow-up.

Manual MarkPlayed also requires confirmation. Where it only restates a verified existing viewing, acknowledge/reuse that fact rather than create a duplicate; a repeat requires explicit intent or a distinct playback occurrence. A stale checkbox cannot prove when or how many times a movie was watched. Do not automatically propagate a shared household watched flag into all Jellyfin users' personal states.

Preserve optional1–5 rating behavior, explicit score confirmation, normalized native-scale handling and authenticated rater attribution. Link to the exact verified event; retain existing linked reviews. Keep is implicit. A Delete action is available after confirmation/rating skip and directly on movie details; no mandatory Keep task or completion/rating prerequisite for deletion. Existing36-hour prompt policies remain settings-controlled; when extending cleanup to new request types preserve unanswered decisions and explicit reopening, with separate tests for message age and uncertain sends.

Historical implementation note (2026-10-24; current availability is F19 in the feature register): W05 supports the installed general-purpose shemanaev Default webhook plus independent Jellyfin API verification. Ryot depends on that plugin format; Curatarr does not depend on Ryot's choice and may use another adapter later. The alternative official Webhook plugin has a different payload and is not accepted implicitly. This increment defaults off, uses no inferred playback accounts and remains off pending dedicated token/account setup and native commissioning; schema5 code is now locally deployed. Session/candidate evidence is additive schema5. Stop/15-minute inactivity, first-Yes journaled completion, unanimous-No local decline, optional rating and exact native-event linking have offline coverage. Uncertain repeat identity and failed publication/verification surface to read-only maintenance. A native completion reference supports lost-response recovery; no self-observer second question is emitted. Native writer retirement, genuine-event commissioning, historical reconciliation, common Requests presentation and retention remain separate gates.

#### Removal, Cancellation And Restoration

Both entry points use one durable operation:

- **External loss:** persist ItemRemoved → allow bounded index/upgrade settlement → verify exact identity, full source/copy scope, physical/storage evidence and Arr queue/import activity → present Archive/Restore. The event is not proof of deliberate physical deletion. Extra-version deletion, upgrade replacement, an index-only disappearance, failed scan and inaccessible mount cannot archive the title.
- **Curatarr Delete:** collect a fresh preview of all verified managed copies, versions/count and active acquisition effects → explicit confirmation → acquire the per-movie operation lock → stop/hold affected acquisition through supported exact Radarr actions → delete only the authorized media scope → verify absence and Arr removal → reconcile durable final facts.

A confirmed archive must prevent re-download, retain Library/acquisition/viewings/reviews, remove active Radarr membership and record Archived. Add Deleted only for a verified deliberately removed real copy; absence alone and canceling an unacquired request never add Deleted or Previously acquired. Existing Deleted is historical and survives later restoration. Do not mark a complete title-wide archive while a remaining alternate copy, active acquisition or uncertain mutation contradicts it. Preserve original settings when taking temporary holds; no blanket “monitored=true” on failed/canceled archive. If an incoming import cannot safely be settled/canceled, block with visible pending status and maintainer evidence rather than report success or delete a new replacement accidentally.

Catalogue-only movies outside Radarr need the same preservation/removal intent, without assuming an Arr record or imported-file history. Verify their own prior-copy evidence and scope. Lack of a Radarr record must not silently bypass safety or require inventing acquisition history. Unsupported evidence retains an actionable or technical pending result, not a fake clean archive.

**Restore**, explicitly confirmed, reuses the saved current native profile and creates one recoverable search. If Radarr no longer knows the movie, invoke the normal add/profile service. Profile and identity changes since preview invalidate stale confirmation. Preserve native history, ratings, historical Deleted and acquisition evidence; current verified presence hides the Deleted display badge. No restoration occurs from an old list, a missing file, a provider outage or a No on a different household watch prompt.

#### Sources, Discovery And Awaiting Metadata

**Personal IMDb:** validate the exact simple public list first; retain the supported fallback if direct unattended access proves unsuitable. Snapshot all current entries as already handled before new-entry processing. No current entry may activate a migration download; entries missing from Radarr are historical/removed/unwanted for intake purposes. An optional exact Library-only reconciliation is a separate preview, not an acquisition batch or fabricated archive/deletion backfill. Preserve handled state through restart, list reconnect and source configuration changes. Current old entries must not be moved through awaiting-metadata into new acquisition by accident. Truly new entries after the protected baseline enter trusted wanted intake; explicit reactivation is still required when current durable state is Archived.

**Discovery:** use TMDB Discover initially, editable18month release window, rating>=7/10, votes>=100 and admin-selectable genre exclusions (none selected until setup). Metadata without reliable identity/genres is deferred and can become eligible later. Suppress exact movies already known/watched/active/archived, previously rejected, pending or accepted. Merge source provenance, not duplicate cards. A candidate dropped from a source remains pending until answered; a filter edit does not erase an explicitNo. Discovery rejection has no RyotArchive effect and can be reconsidered manually. Personal wanted/manual searches bypass discovery genre filters.

Collect daily by default, independent of user presentation. Default notifications off. Optional configurable digest, initiallyweekly/max5new proposals in one compact message; manual browsing pages10 withContinue. Do not repeatedly announce unchanged pending items, mark them declined on timeout or discard excess backlog. A partial/failed poll cannot remove candidates or advance a successful-baseline cursor. A deliberate source-depth limit bounds fetching, not the lifetime of already collected unanswered candidates.

**Awaiting metadata:** persist an explicit IMDb request when no verified supported movie identity exists. Offer keep-waiting/withdraw, preserve interest through source churn, and revalidate when metadata becomes available. Do not invent a TMDB/Ryot movie, substitute TV metadata, claim cancellation from provider404 or initiate acquisition by title. Once eligible, return explicit requests to the appropriate normal profile/catalogue-only decision; a generic discovery candidate never turns into an approved request merely because metadata appeared.

#### User Interface And Permissions

Use one common normal-user Requests view for actionable watch/removal/awaiting-input requests, preserving existing household visibility rules and clear request-type banners. A unique actionable-movie count must not count silent discovery backlog or technical findings as unanswered watches. Requests remain recoverable when Telegram messages are gone. Keep technical status in Admin System check, including unknown/incomplete states; preserve hidden diagnostics and existing maintainer repair code.

Provide Suggestions as an on-demand browse view sharing the current poster/detail presentation, withYes/No, source reason and a clear route toAdd and search orCatalogue only. It does not create a second catalogue. Use existing search/detail flow for profiles, directDelete, restore and explicit reactivation. Use disabled grey actions with reasons where an operation is unavailable. Keep attribution and danger/scope explanations short and tied to consequential confirmation.

Settings retain installation name/defaultHousehold, Owned cleanup and36hour prompt policies. Add only necessary playback thresholds/account allowlist and Sources/Discovery configuration: source enable/health, personal-list profile policy, editable discovery filters/genre choices and notification period/cap. Credentials and raw event diagnostics stay private deployment data. No automation builder, generic workflow editor, personal watch catalogue or general repair dashboard is included.

Configured household users can perform normal decisions. Admin controls configuration and technical repair. Authenticate and authorize each action server-side, verify offered actions and current revisions, prevent stale/double clicks and rate-limit consequential actions. The existing test chat remains bound throughout development; the final group already exists and is migrated only after acceptance.

#### Runtime And Deployment Target

Build one dedicated application container for the selected media-services host, with a single explicit combined runtime entry point for HTTP, Telegram polling and durable workers. The current Docker scaffold only validates configuration by default and must be adapted/tested. Each worker/request owns an appropriate SQLite connection. No multi-worker web launch may silently duplicate pollers or mutation workers. Support graceful shutdown, durable leases, restart recovery and separate liveness/readiness/integration-health results without leaking secrets.

Persist configuration/secrets and workflow data outside the image, with validated UID/filesystem permissions and consistent SQLite backups. Keep provider data backups and a tested old/new image rollback procedure; database restoration alone cannot undo externally committed effects. Stable public HTTPS routing is required for Telegram/Jellyfin; use configured internal service addresses for provider calls. No direct host media-volume write mount is assumed: validate supported provider file APIs and scope before deciding any mount requirement. Exact host, paths, tokens and destination mappings remain private. Retain the Mac test runtime and current final-group hold until the good working copy is accepted.

#### Acceptance Catalogue

The AC rows below are the target acceptance contract, not a current test report. [Features](../maintainer/FEATURES.md) maps them to implementation/test gaps; [Workflow](../maintainer/WORKFLOW.md#living-user-acceptance-checklist) owns actual user reports. Scope changes are recorded in [Decisions](Decisions.md). Source implementation and local deployment do not establish native commissioning or release acceptance. All relevant offline/browser/container and explicitly scoped live gates must pass before release. Historical test counts are evidence only for their dated source baseline.

| ID | Required observable result |
| --- | --- |
| AC01 | Add and search writes one exact movie/profile/root, returns verified service state; duplicate clicks/replayed events do not duplicate records/searches. |
| AC02 | Catalogue only creates/preserves Library with no Arr add/search/acquisition flags; canceling a profile step preserves the pending suggestion. |
| AC03 | Existing active, archived, excluded and conflicting identities take correct branches; no title-only mutation or automatic historical reactivation. |
| AC04 | First IMDb import/restart/reconnect never starts a download for any baseline entry, including absent/unwanted titles; a genuinely new permitted entry processes once. |
| AC05 | Repeated presence of an archived personal-list movie cannot reacquire it; discoveryNo does not block an explicit later wanted request or erase existing data. |
| AC06 | Playback below threshold, crossing95, stop, missingstop, seek, resume, duplicate/out-of-order updates and multiple users yield the intended single candidate without movie-progress UI/Ryot partial history. |
| AC07 | Manual MarkPlayed without position works; existing history/repeat cases are distinguished; MarkUnplayed never clears household history automatically. |
| AC08 | OneNo/anotherYes, allNo, silence, legacy questions, admin confirmation and old callbacks preserve the policy; native viewing is created only when required and not followed by another duplicate question. |
| AC09 | Rating/no-rating, native scales, existing reviews, attribution, lost create/link replies and restart preserve exact event linkage and avoid duplicate writes. |
| AC10 | External ItemRemoved for a real deletion, upgrade, extra version, source outage, empty mount, index rescan and late import is correctly classified; only verified confirmed scope archives. |
| AC11 | CuratarrDelete previews versions, requires confirmation and removes only authorized copies/active intent; partial native/file outcomes remain pending with all unrelated data preserved. |
| AC12 | ExplicitRestore reuses/offers a profile and produces one recoverable search; no automatic restoration and no erasure of historical facts. |
| AC13 | Known/duplicate/rejected/filtered/deferred/accepted discoveries, overlapping sources and source disappearance preserve one accurate queue; pending items never expire on their own. |
| AC14 | Notifications-off still collects, capped digest sends no flood/repeated backlog, pagination/Continue doesn't decide, and failure/restart cannot consume or duplicate decisions. |
| AC15 | Awaiting-metadata request survives, can withdraw, cannot acquire from a guessed/TV identity and cannot reactivate a protected IMDb baseline entry. |
| AC16 | Curatarr marker is attached only after verified work, does not trigger acquisition, does not backfill the whole library and cannot erase protected memberships. |
| AC17 | Standard/admin access, webhook auth/body bounds, unknown account, stale revision, forged callbacks, source URL handling and secret redaction withstand negative tests. |
| AC18 | Upgrade from schema3, restart during every mutation/delivery boundary, expired lease and backup/restore preserve recoverable work; unknown effects stop for investigation instead of blind replay. |
| AC19 | Existing and new Mini App/chat flows work at320x640,390x844,844x390,1280x900 with clear banners, disabled actions and persistent unanswered requests. |
| AC20 | Dedicated image starts HTTP+one poller+workers with persistent volumes; shutdown/restart/health/backup work; final cutover leaves one coordinator and preserves provider history/catalogue. |

Live destructive/history acceptance needs exact approved examples and before/after evidence; synthetic fixture success is not permission to invent a household viewing or delete a real movie as a test. Native API feasibility must be proved early enough that a blocked IMDb adapter or ambiguous viewing-create recovery is resolved before broad UI polish or migration promises.


### Durable-State Adapter Principle

Curatarr should treat Ryot as the first durable-state backend, not as an inseparable internal model.

The internal Curatarr domain should use neutral concepts such as `Library`, `Watched`, `Archived`, `Previously acquired`, `Deleted`, `Radarr active`, and `Sonarr active`. The Ryot integration maps those concepts to Ryot records and collections according to [Ryot Collection Contract](Ryot%20Collection%20Contract.md). If another durable media database later supports equivalent mechanisms such as collections, tags, labels, or status flags, adding it should primarily require a new adapter rather than rewriting Curatarr workflows.

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
| Title deliberately belongs to the household catalogue | Explicit Ryot Library membership |
| Title was watched | Ryot viewing history, with `Completed` as supporting classification |
| Title is archived or canceled | Ryot `Archived` collection/label |
| Household has had a managed copy | Ryot `Previously acquired` |
| A previous managed copy was deliberately deleted and verified | Ryot `Deleted`; detailed execution evidence in Curatarr audit |
| Title is active in movie workflow | Ryot `Radarr` overlay plus Radarr verification |
| Title is active in series workflow | Ryot `Sonarr` overlay plus Sonarr verification |
| File exists, profile, quality, monitored state | Radarr or Sonarr |
| Native Arr watchlist re-add prevention | Arr exclusion list derived from Ryot `Archived` |
| Pending questions, webhook state, scan cursors | Curatarr internal workflow state |
| Jellyfin reception check status | Curatarr internal operational state |

### Control Rule

- Ryot is the durable source of truth.
- Presence in Radarr/Sonarr means the title is wanted on the share, whether it is missing, retained unwatched, retained watched, monitored, or unmonitored. Monitoring controls further acquisition, not wantedness. A separate Wanted collection is not required by the current model; active memberships should reconcile with Arr.
- Deliberately ceasing to want a title uses controlled archival: preserve Ryot history, mark Archived, delete managed files, remove the Arr entry and active Ryot membership, and verify completion. Deleting only files is not a completed removal because the remaining Arr entry can request them again. Unexpected file/entry removal remains drift requiring clarification, not automatic archive intent.
- Curatarr is the orchestrator that reads Ryot and Arr state, asks for missing intent, then writes the required changes.
- Radarr and Sonarr are execution systems. Their direct changes are operational facts that Curatarr reconciles back into Ryot before deciding follow-up actions.
- Once Curatarr is operational, users should make stack changes through Curatarr rather than directly in Ryot. Direct Ryot edits are tolerated for commissioning and exceptional cleanup, then detected by periodic scan.

## State Model

The underlying states below are independent facts, not user-facing lifecycle steps. [Ryot Collection Contract](Ryot%20Collection%20Contract.md) owns the exact collection mapping and setup baseline; the model below summarizes it. [Media Status and Display Rules](Media%20Status%20and%20Display%20Rules.md) owns the compact availability/watch/audio vocabulary and progressive disclosure. Library is explicit catalogue membership, not mere metadata existence or proof of a deliberate prior lookup. Previously acquired is historical acquisition, distinct from native Owned and current availability. Any confirmed ready version remains Ready to watch while upgrades are sought. The resolved `UI-QUALITY-01` decision keeps quality in expanded details and uses only confirmed Monitored/Unmonitored as a compact execution modifier.

The household overview presents Available, Wanted, Archived, or Recorded, with explicit Unknown/unavailable states. Available/Wanted/Recorded are derived summaries, not new Ryot collections. Getting ready is omitted from the overview. A secondary Deleted badge requires verified historical deletion and confirmed current absence of a replacement; neither Owned nor missing files supplies that evidence. The read-only adapter now maps explicit Library, Previously acquired, and Deleted memberships, without Owned-based acquisition inference. Durable deletion is read from Ryot, with exact-identity verified local audit evidence retained as a fallback and a synchronization note for missing Ryot membership. Controlled deletion capture and automatic synchronization remain unimplemented. No history panel is exposed. The collection contract records this boundary; the status catalogue owns display precedence.

The current model separates durable Ryot state from Arr runtime state.

### Durable Ryot State

| State | Meaning |
| --- | --- |
| `Library` | Explicit membership in the custom Library collection: deliberately retained in the household catalogue, including after archival/deletion. Metadata existence alone is insufficient. |
| `Watched` | Actual Ryot viewing history, regardless of where or how; native Completed is supporting classification. Never invent counts/dates from collection membership. Does not imply archive or deletion. |
| `Archived` | The title is no longer wanted in active processing but should remain known. This includes watched-and-deleted titles, unwatched canceled titles, and abandoned titles. |
| `Previously acquired` | The household has had a managed copy at least once; explicit custom membership backed by acquisition evidence, retained after deletion. Native Owned is not this fact. |
| `Deleted` | A previous managed copy was deliberately deleted and verified; explicit custom membership retained historically, even after reacquisition. Not inferred from absence or archive intent. |
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

- `Library = true` when the title has explicit Ryot Library membership; a metadata-only match is not sufficient.
- `Watched = true` from Ryot completed viewing evidence or supporting Completed membership; actual viewing events determine counts and dates.
- `Previously acquired = true` from explicit Ryot membership backed by acquisition evidence; a successful Arr import can establish that fact.
- `Deleted = true` from explicit verified-deletion membership; current replacement/absence controls badge visibility, not the historical membership.
- `Arr active = true` when the matching Ryot active overlay and the matching Arr application agree after reconciliation.
- `quality_meets_profile = true` when the current file quality satisfies the chosen profile.

### Native Owned And Completed Boundaries

- Native Owned remains Ryot physical/digital inventory, outside Curatarr's operational status model. Preserve it by default without renaming or repurposing it. An optional admin Settings policy removes movie Owned memberships daily when switched on, preserving series, all other memberships, viewings, reviews and files. The Ryot Collection Contract owns this narrow exception; it is not acquisition evidence or a general collection writer.
- Previously acquired replaces Curatarr's former historical interpretation of Owned. Do not migrate uncertain historical memberships without evidence.
- Completed and actual viewing history remain relevant to watched status, not acquisition or archive intent. Do not create a custom Watched collection or fabricate viewing events.
- Watchlist, In Progress, and Ryot Monitoring are not substitutes for active Arr intent or Arr monitoring.
- Exact mapping, native automation cautions, commissioning evidence, and the maintainer handoff belong to [Ryot Collection Contract](Ryot%20Collection%20Contract.md).

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

Planned removal entry points must also cover a movie abandoned partway through and an unwatched movie the household no longer wants. Users must be able to mark these titles for removal directly, without waiting for a completed-watch question. Preserve actual partial progress and any prior completed viewings/reviews; abandonment, cancellation and an optional rating must never invent a completed viewing. Removal intent or a pending task is not evidence for `Deleted`: that requires verified deliberate deletion. Preserve Library and Previously acquired history throughout. This controlled workflow is not implemented; its execution needs exact-identity validation, explicit destructive confirmation, verified outcomes, audit and recovery from partial failures.

### Cancel Before Download

Cancel is archive-like for an unwatched or not-yet-downloaded title. The title remains known in Ryot, active Arr state is removed, and `Archived` may be added to record that the user is done with it.

## Core Workflows

### Flow 1: Search, Add, Reactivate

Trigger:

- user searches for a movie or show through Curatarr
- user selects a title and wants to add or manage it

Curatarr checks:

- Ryot identity, explicit Library membership, viewing history/Completed, Archived, Previously acquired, Deleted, and active overlays
- Radarr or Sonarr presence, file presence, monitored state, profile, and quality
- exclusion-list or blocked-add conditions where applicable

Branches:

| Branch | Handling |
| --- | --- |
| No Ryot record and not active in Arr | Offer add flow. |
| Ryot record exists, not archived, not active in Arr | Show full history/context and ask for reactivation with profile selection. |
| Ryot record exists, archived, not active in Arr | Require explicit confirmation before reactivation and profile selection. |
| Ryot record exists and Arr active | Do not duplicate-add; show management options. |

Planned announced-project branch: a project may be announced on IMDb before a supported TMDB movie record exists, or its provisional TMDB record may later disappear. Treat this as awaiting catalogue metadata, not evidence of cancellation or a service outage. Preserve the IMDb identifier and the user's interest in a persistent pending request until normal durable catalogue registration is possible. Offer simple keep-waiting or withdraw choices; a later verified movie identity can return the request to the normal add/profile-selection flow. Removing an obsolete Arr registration must not silently discard that pending interest. Never invent a provider ID, use a TV-series match as a movie, transfer viewing history by title, or start acquisition solely because a same-title result appears. This is a future workflow requirement; no automatic polling, registration, acquisition or UI is implemented here.

Write order:

1. Resolve the intended title.
2. Reconcile obvious drift if needed.
3. Write durable intent to Ryot.
4. Execute the Arr change.
5. Verify resulting Ryot and Arr state.

### Flow 2: Watched / Completed

The implemented flow below observes existing Ryot events. [Coordinator Transition Review](#coordinator-transition-review--2026-10-24) proposes direct source intake and confirmation before creating a native event; migration must preserve existing questions and avoid duplicate viewings.

Trigger:

- Jellyfin marks a title watched in Ryot
- user manually marks a title watched in Ryot

Rules:

- Ryot `Completed` means `Watched`.
- Watched does not imply archived, deleted, or no further action.
- Whenever a movie is watched, Curatarr should ask what to do with it.
- The prompt must live in persistent Curatarr workflow state, not only in an old Telegram message.
- Newly detected movie completions should produce one shared Telegram message visible to the household, asking for confirmation of watched status and a rating, followed by keep/archive intent. Use the current test channel during commissioning; move to ORION after testing, not implicitly during UI work. Deduplicate delivery and persist the outstanding questions so unanswered or partially completed prompts remain available in Viewing inbox. Channel destination/permissions must be verified before sending; never assume individual bot chats are a shared channel.
- The first viewing slice is implemented and explicitly opt-in through an admin's `/followups_here`: baseline existing Ryot completed-event IDs without historical prompts, observe new completions, persist shared questions, then collect watched confirmation and a 1-5 rating through Telegram. A separate polling worker uses its own database connection and reader. It does not enable Jellyfin watch ingestion or create duplicate native viewing events. Retention execution remains pending. Do not turn every downloaded or historically watched title into a new unanswered follow-up merely to fill the queue.
- A request snapshots participating configured users and records temporary individual answers. A No hides only that person's request and never changes shared watch history. The first Yes confirms the existing Ryot event, clears all other users' inboxes and offers the confirming user an optional rating. All No answers move the record to Admin → Declined viewings for occasional review; silence is not No. Admin confirmation can recover that record after fresh validation. No automatic watch deletion is permitted. Missing/changed native events and pending history corrections are monitored and alerted; their retained corrective controls appear only in maintainer diagnostics; Declined viewings retains existing events the household declined. Verified mismatches leave the normal inbox/count without an added notice. Outages do not establish a mismatch. Admin correction offers Review again for the same existing event, local-only Clear stale request for a missing event, and explicitly confirmed Clear viewing history for all events of one exact movie. The latter preserves reviews and other intent memberships, clears watched flags, and uses durable snapshot/claim/read-back recovery; pending operations block competing answers. Native collection side effects that would alter existing Watchlist/Monitoring intent block deletion for manual review. The local answers/audit do not form a personal watch catalogue. New private review text names the rater using authenticated Telegram identity; the shared Ryot account remains author. Normalized rating conversion, existing-review preservation, CAS and uncertain-write recovery remain unchanged. Legacy unassigned disputes return for confirmation; old votes are never invented.
- Telegram follow-ups include optional exact-ID TMDB artwork and the recorded viewing date. Rating selection uses five buttons in one row, without a default selection, followed by a separate confirmation; changing the selected score writes only local workflow state. Repeat watches keep independent event-specific prompts and rating links. Poster failures fall back to text, and replies preserve the recorded message ID rather than replacing the question.
- Caption banners distinguish watch-confirmation requests, rating requests, confirmations and disputes from announcements. Two independent off-by-default Settings switches can remove answered and/or unanswered Telegram prompts 36 hours after sending, within Telegram's deletion window. Deleting a message never expires/resolves its question. Removed unanswered questions remain in Viewing inbox and can be explicitly reopened into the verified Telegram destination, preserving event identity, answers and uncertain rating recovery. The development guide owns timing, outage and legacy-message details. Settings also owns the persistent installation display name, default Household; no Telegram rename command is provided.
- The event ledger and question creation commit atomically (SQLite schema 3); shared answers use optimistic context checks. Sending/uncertain deliveries are not blindly resent, because Telegram send and local commits are not atomic. A genuine authorized callback can recover a missing message ID; otherwise maintenance retains the question for external investigation. Rating requests are marked before writing; same-score retries reconcile an existing marked review without another create. Unknown outcomes remain pending, and changing/declining a rating is blocked until a prior write is verified. The development guide owns activation, limits and commissioning steps.

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
- A completed-watch or rating prompt does not detect deletion. Unfinished movies may never produce that prompt, so external removal detection must operate independently of viewing completion.
- User clarification (2026-10-23): confirmed external file loss must open a user confirmation workflow similar to watch confirmation. Ask whether the removal was intentional and whether to archive the title or keep it wanted for restoration; do not silently accept the deletion as household intent. This confirmation does not establish that the movie was watched.
- Confirm service/storage availability and distinguish indexing delay from actual file loss before presenting a deletion fact. Missing-file detection alone must not mark a title watched, archived or deliberately deleted.
- The future workflow must address the race with Arr re-download while establishing restore/keep-wanted versus archive intent. No current external-deletion safeguard or automatic repair is guaranteed by this design.

Handling:

| Case | Handling |
| --- | --- |
| Expected deletion after Curatarr archive | Verify file gone, active overlay removed, Ryot archive state correct, close task. |
| Unexpected deletion | Reconcile visible facts and open or resume clarification prompt. |

Prompt options for unexpected missing files:

- `Archive`: preserve Ryot history, add Archived, remove active overlay.
- `Keep monitored`: keep Arr active, set monitored, require or offer profile selection.

No third branch is needed.

#### Planned Jellyfin Library Cleanout

The user may browse Jellyfin and delete movies the household no longer wants. This is a supported entry point for the planned removal workflow: Curatarr should detect the external removal and complete the remaining ecosystem work after the intent confirmation above. It must work for watched, abandoned and unwatched movies alike, without requiring a rating or completed-watch trigger.

After validating exact title/copy identity and actual removal, ask whether the household deliberately discarded the movie or wants it restored. For confirmed discard, remove the active Radarr entry and Ryot Radarr membership to prevent re-download, retain Library and existing acquisition/viewing/review history, record Archived intent, and record Deleted only with verified deliberate-deletion evidence. Verify and audit all steps, retaining a recoverable pending task when only part of the operation succeeds. If the title remains wanted, use the restoration branch instead. Deleting an extra version, replacement during upgrade, or a temporarily unavailable library must not silently archive the whole title. This is a future requirement; external-removal detection and completion are not currently enabled.

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
- A scan should cover the collections and viewing evidence defined in the collection contract: Library, Archived, Previously acquired, Deleted, Radarr, Sonarr, and native Completed/viewing history. Owned is not an acquisition source. The current adapter's broader user-library scan remains an implementation boundary, not this target contract.
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
| First successful import | Ensure Library and Previously acquired in Ryot; do not write Owned. |
| Direct profile change | Usually accept as execution-layer state. |
| Direct Arr add, non-archived title | Auto-reconcile unless policy requires confirmation or contradiction exists. |
| Direct Arr add, archived title | Never silently reactivate; require Telegram confirmation. |
| Direct file delete | Reuse missing-file flow. |
| Direct title removal | Ask whether to archive/cancel or reactivate unless caused by known Curatarr action. |
| Monitored change creating active + missing + unmonitored | Prompt for archive/cancel or re-monitor with profile selection. |

Direct Arr removal prompt:

- watched title: `Confirm Archive` or `Reactivate`
- unwatched title: `Confirm Cancel / Archive` or `Reactivate`

If archive/cancel is confirmed, Curatarr keeps Ryot Library, preserves viewing history and Previously acquired if present, adds Archived, removes the active overlay after verified completion, and leaves the title absent from Arr. Add Deleted only if an actual managed-copy deletion was verified. Preserve native Owned unchanged; missing files or a canceled never-downloaded request are not deletion evidence.

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
  - the show has explicit household Library membership
- `Watched`
  - Ryot has completed/watched state for the show or tracked episode progress
  - for series, watched state may be partial
- `Archived`
  - the user is done with the show as an active Sonarr target
  - archiving a show removes the show from active Sonarr processing
- `Previously acquired`
  - at least one managed episode has been acquired; this does not establish complete-series acquisition
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

- First successful episode import ensures Ryot Library and Previously acquired for the show if missing; it does not write native Owned.
- Episode or season upgrades update runtime facts only.
- Each import may start a Jellyfin reception check for the imported episode, but failures are admin-only operational issues.

Archive behavior:

- Archiving a show means the user is done with the show as active processing.
- Curatarr removes the show from Sonarr and removes the Ryot `Sonarr` overlay.
- If files exist, archive deletes the show files unless policy requires admin confirmation.
- Viewing progress, Library, and Previously acquired remain in Ryot; native Owned is untouched. Record Deleted only for verified deliberate removal, with scope sufficient to avoid mistaking one removed episode for removal of the whole series.

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
- First successful import should ensure Ryot Library and Previously acquired if missing, without writing native Owned.
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

Current implemented movie monitoring runs daily in a dedicated bot worker and on explicit Mini App Check now. It retains the eight Ryot/Radarr reconciliation categories and adds whole-catalogue identity/membership checks, Jellyfin indexed-source consistency, native Radarr health/queue warnings, exact viewing-event mismatches and unresolved local operations. Independent scope failures retain prior evidence and prevent a clean result; no provider mutation is performed. The Mini App exposes a compact Yes/No/incomplete status and ordinary household viewing review; technical viewing repairs are hidden and reserved for maintainers; detailed categories and the acquisition repair UI are hidden, retained behind maintenance.show_diagnostics for later review. Full reports remain available through repository --check-maintenance. The development guide owns coverage, lease/retry details and known exclusions; this is not exhaustive series, filesystem or end-to-end playback monitoring. Broader target flows below remain separate from implemented repair authority.

Before publishing playback inconsistencies, the monitor directly re-reads flagged Jellyfin item IDs and the copies associated with flagged logical movies. It also revalidates prior warnings before clearing them, using current identifiers and indexed-source facts rather than trusting a healthy-looking bulk response. Bounded validation failures and contradictory presence evidence keep that scope incomplete and retain its previous findings. Consistent absence from both complete reads can resolve an obsolete item warning. This validation neither edits Jellyfin metadata nor verifies the physical files; the development guide owns request limits.

Implementation boundary: `--reconcile-movies` runs one read-only movie scan and returns a JSON admin report. It reads complete Ryot movie/user-state and Radarr snapshots, uses exact typed external identifiers, and persists deduplicated findings in the existing pending-decision queue with `reconcile:` types and the `admin` audience. Repeated unchanged findings retain their decision IDs; changed evidence supersedes the old finding, and a successful complete scan resolves findings that no longer apply. Unrelated user decisions are untouched. Job records and provider technical issues use the existing local workflow tables; a newer scan prevents an older overlapping scan from publishing stale results.

Implemented findings cover reported missing/unmonitored records, Archived titles still in Radarr, missing Library or active Radarr membership, reported managed files without Previously acquired membership, and active memberships lacking an exact Arr match. Unknown, conflicting, duplicated, or title-only identity evidence is routed to review rather than allowing collection correction. This first slice does not cross-resolve IDs or apply confirmed local identity mappings; a missing exact match is not proof of absence. Archived intent suppresses ordinary acquisition suggestions.

Provider failure or an incomplete snapshot leaves existing findings unchanged and unvalidated, recording a deduplicated technical issue instead of mass absence findings. Every CLI invocation scans again before reporting. Saved reports retain an observation time and are not evidence of indefinitely current state. The scanner performs no writes to providers, filesystem/media inspections, Jellyfin readiness checks, deletion assertions, or mount-health probes. In particular, Radarr's `hasFile` is a reported execution fact, not independent filesystem evidence. It suffices for historical acquisition repair under the policy below. Admin review, skip/resume and the narrow confirmed acquisition repair are implemented below; scheduling and other corrective actions remain future work. The existing user search `Keep as is` callback is not a reconciliation action.

Admin maintenance implementation:

- Only authenticated configured admins can access the maintenance API; standard users do not see its navigation and receive HTTP 403 for direct requests.
- The header groups brand + subtle circled Info on the left, and Viewing inbox eye/count + admin-only gear on the right. Counts reflect unique movies needing that user's input. First Yes removes the request for other users immediately, while only its respondent retains optional rating input. The open inbox checks versions every 15 seconds while visible and on foreground return. Admin contains Sync issues, Declined viewings and Settings; Settings info icons sit directly beside their titles.
- Viewing reads genuine open `viewing_followup` decisions for `shared_household`, grouped into one list by movie. Unanswered questions remain even if an old expiry timestamp has passed; resolved questions disappear. Question context lists the outstanding `watched`, `rating`, and/or `retention` keys; movie keys are exact Ryot IDs. Fresh Ryot facts are shown before displaying details. Only navigation position persists under `viewing_progress`, isolated from sync skip/filter state. There is no postponement or dismissal action; previously postponed questions return to the list. Identity/read failures hide current movie details without changing the question.
- Viewing inbox displays freshly validated questions with separate dates/actions per native event. Mini App and Telegram share the same persisted response core. Web answers require current movie cursor, exact context version, an offered action and fresh account/event evidence. CAS prevents stale overwrites; another person cannot rate a confirmed respondent's viewing. Unanimous declines are excluded from ordinary inboxes and reviewed through admin-only APIs, with versioned explicit confirmation. The same Admin page lists stale requests for removed native events. Clear stale request requires fresh exact-account/movie evidence, a complete duplicate-free history without the original event, no pending rating operation and an unchanged request version; it closes only Curatarr workflow state and never confirms or deletes a native watch. Caption/button edits retry against recorded message IDs, including resolved prompts, without replacement sends. The observer keeps one explicitly verified Telegram destination; shared-chat buttons cannot be hidden per member, so a personal No only clears that user's Mini App request and leaves the shared prompt for others. No private-message broadcast subscription system is introduced.
- The summary groups open findings by movie and issue type. It is a historical overview, not freshly validated movie evidence. Previous/Next cycle through the filtered queue; several reasons for one movie appear together.
- Acquisition identity review takes Radarr's IMDb/TMDB identity as the operational reference, because it controls which release Radarr searches for. Show the specific mismatch and candidate Ryot title/year/IDs. A same-title record with different IDs is not a match; prefer finding or adding the Ryot record for Radarr's target, without rewriting the unrelated record or its viewing history. Missing/duplicate/conflicting identifiers still block corrections; this preference does not make Radarr metadata infallible or override Ryot's intent/history authority.
- Before showing a movie, re-read Radarr's validated snapshot and the matching Ryot user record. Established matches use exact Ryot IDs. Missing/ambiguous Ryot matches, disappeared known records, and newly conflicting identities require a complete scan instead. Existing identity limitations from the scan still apply; no title-only evidence is promoted to a confirmed match.
- Fresh evidence can supersede changed findings or resolve obsolete ones. Partial movie updates compare the expected queue state under a database write lock; concurrent changed findings require a refresh rather than overwriting newer evidence. Failed checks hide the old movie detail, preserve unresolved findings, and show an unavailable-check message.
- Sync Skip parks a movie for that admin only, without resolving shared findings or changing intent. Skipped is an option in the issue dropdown; all other options show pending findings only, with counts excluding skipped titles. There are no separate Pending/Skipped buttons. Cursor, issue filter, skipped keys, and mode persist in the existing state_snapshot table under a per-admin maintenance_progress source. Reopening resumes with revalidation, not stale stored detail.
- Refresh runs a complete read-only scan and revalidates the selected movie. Navigation uses authenticated POST requests because it writes local progress/queue state. Review and correction requests are serialized within the web process and rate-limited.
- The first provider correction is an explicit admin-confirmed, single-movie addition of Previously acquired. A full fresh identity scan, unchanged finding, non-archived match, and Radarr imported-file record are required. Radarr database evidence is sufficient for historical acquisition; no filesystem verification is required or claimed. The writer targets the configured user's existing custom collection, reads back membership, and audits requested, verified, submitted or unconfirmed outcomes. Job acceptance is not completion. No general state-write interface is enabled.
- Findings show a bold conclusion, plain-language evidence, and proposed correction. Correct is offered only for the supported acquisition-history repair; other workflows remain guidance, not misleading executable buttons. No batch correction is implemented in the Mini App. Commissioning batches are separate outside-app operations requiring their own authorization.
- Scheduling, monitor/archive/other collection corrections, and the persistent Telegram notification/home message remain pending. Maintenance is movie-only; no Sonarr or Jellyfin filesystem/deletion inference is added.

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
| `Safe auto-fix` | Factual mismatch with low ambiguity. | Active non-archived Arr title missing from Ryot Library, Previously acquired missing while a managed copy is verified, active overlay missing for a non-archived Arr title. |
| `Policy auto-fix` | Safe only if installation policy allows it. | Direct Arr add of non-archived title, direct Ryot add that should activate downstream. |
| `Needs clarification` | Intent cannot be inferred safely. | Archived in Ryot but active in Arr, unexpected file missing, unmonitored Arr title without a downloaded file, title removed from Arr but still active in Ryot, watched active title with unclear keep/archive intent. |

Guided daily maintenance example (planned; UI work is complete for now, while the on-demand scan foundation is implemented):

```text
Library maintenance
8 of 1,250 movies require attention
1 of 8: Spider-Man: Brand New Day (2026)

Movies registered in Radarr are classified as wanted.
This movie has no downloaded file and monitoring is disabled.
A wanted movie without a file must be monitored so Radarr can acquire it.

Enable monitoring: keep the movie wanted and allow Radarr acquisition.
Cancel / Archive: no longer wanted; remove the Radarr entry and any
remaining managed files, while preserving Ryot history.
Skip: leave unchanged for later review.

[Enable monitoring] [Cancel / Archive] [Skip]
```

Radarr permits unmonitored records without files, but this combination is inconsistent with Curatarr's wanted-content policy and requires clarification. Accidentally disabling monitoring is only a possible explanation, not grounds to enable it automatically. Archived intent remains authoritative: an archived title still present in Arr requires an archive-drift decision, not an ordinary enable-monitoring prompt. Archive confirmation must explicitly disclose file deletion and Arr-entry removal.

Before presenting missing-file decisions, distinguish confirmed absence from unavailable services or storage. A service or mount outage must produce a technical issue rather than a batch of assumed missing-file decisions. Persist and deduplicate unresolved decisions, and revalidate them before display and correction as described in Flow 10.

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
Radarr/Sonarr import -> Curatarr ensures Library / Previously acquired
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

- Curatarr's read-only Ryot mapping is defined in [Ryot Collection Contract](Ryot%20Collection%20Contract.md): explicit Library, Archived, Previously acquired, Deleted, active overlays, and native viewing history/Completed. Writes and automatic maintenance remain pending.
- Curatarr writes durable intent to Ryot before executing downstream changes.
- Ryot `Completed` must not be overloaded to mean archived, deleted, or no further action.
- Curatarr is the intended operational writer of its custom memberships once controlled writes exist; it must not write native Owned as acquisition history.
- Review integrations for competing collection writers and acquisition triggers before enabling Curatarr writes. Native Owned changes alone are not commands to acquire, archive, or change monitoring.
- Direct Ryot edits are allowed during setup or exceptional cleanup, but Curatarr remains the intended operational control surface.
- Ryot changes outside Curatarr are detected by configurable periodic scan, not by hacked Arr push integrations.

### Telegram

The preferred long-term interactive surface is a Telegram Mini App, with the bot retaining entry points, notifications, and lightweight confirmations. Both consume the same Curatarr backend services; the browser does not own identity, acquisition policy, or service credentials. The first Mini App implementation is read-only: movie poster/list browsing, collapsed descriptions, explicit shared-history status definitions, and current selected-movie facts. Native chat workflows remain a fallback. HTTPS hosting and server-side validation of Telegram launch data are required before external commissioning.

The current household deliberately uses the configured shared Ryot account for watch history. Telegram authentication identifies the authorized caller; it does not imply per-person watched tracking. The UI must label this scope explicitly and distinguish no recorded completion, no matching Ryot entry, an uncertain identity, and an unavailable lookup. Backend failures must not be interpreted as absence or permission to acquire. The Mini App reads completed-event counts/dates from Ryot, current quality/size, selected profile, native file score and compact upgrade goals from Radarr, and indexed source/audio evidence from Jellyfin on demand. Exclusions remain pending. Goal summaries are presentation of native facts, not a parallel acquisition engine or proof of active searching. User-facing facts follow the status catalogue, not provider vocabulary; this does not enable background reconciliation or acquisition writes.

- Telegram should expose both action flows and pending decision recovery.
- The home/control message should summarize system state and outstanding decisions.
- Completed actions should reach a terminal completed state and should not leave stale action buttons open.
- `/help` remains command reference; `/start` should become the interactive entrypoint.
- Search results default to a poster carousel with forward/back navigation and explicit title selection. A list-view toggle remains available, retaining the current carousel position when returning. Artwork and synopsis are presentation data, not identity evidence or acquisition criteria.
- Technical operational issues, such as Jellyfin reception failures after Arr import, should be routed to admin users by default.

Current implementation boundary: an opt-in Telegram long-polling runtime exposes movie search, explicit title selection, and persisted decision review in private chats for configured users. Callbacks validate ownership, open status, a 24-hour search-prompt expiry, offered actions, and freshly read Ryot/Arr facts. Changed decisions are superseded and replaced before further user action. Detail refresh and closing a question with `Keep as is` are implemented; acquisition/profile/archive actions remain unavailable until controlled writes and profile selection exist. Polling offsets and title choices persist in SQLite. Live integration commissioning, pending-question recovery, the home/control message, and series commands remain outstanding. This does not advance the deferred NFO roadmap.

## Configuration Model

Curatarr should use a YAML configuration file for the initial implementation.

The repository should provide a heavily commented template that explains every section and the operational impact of each option. The template should be understandable without reading the source code.

Required sections:

| Section | Purpose |
| --- | --- |
| `users` | Telegram user IDs, admin IDs, standard users, and optional per-user permissions. |
| `telegram` | Bot token, default chat behavior, home/control message behavior, and pending-question display rules. |
| `tmdb` | API key for metadata search; required by the Telegram runtime, optional for existing configuration-only checks. |
| `ryot` | URL, API key, scan interval, and six custom collection names under the Ryot Collection Contract. New Library/Previously acquired/Deleted keys have canonical defaults; legacy owned is ignored. Distinct custom names are required. |
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

Example target policy shape (not the current runtime configuration contract):

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
      first_import_acquisition_sync: auto_apply
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
| First import ensures Ryot Library / Previously acquired | `auto-apply` | Factual sync from verified Arr import; no native Owned write. |
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

Historical planning taxonomy. Current execution order and scope are owned by [Roadmap](../maintainer/ROADMAP.md); obsolete broad repair/UI ideas below are superseded by D003 in [Decisions](Decisions.md). Sonarr writers remain outside the coordinated movie release.

Priority: complete and verify the original phases below before starting optional NFO audio-evidence enrichment. The [NFO Audio Evidence Evaluation](NFO%20Audio%20Evidence%20Evaluation.md) preserves the findings and deferred roadmap options: first manual NFO-assisted release selection with explicit approval and monitoring disabled, then potential automatic enrichment before native Arr evaluation. Neither stage is an MVP prerequisite or implemented capability.

### Phase 1: Search And Decision MVP

- TMDB movie search
- Ryot lookup
- Radarr lookup
- decision summary message
- action buttons
- watched warning

### Phase 2: Curatarr State Writes And Movie Reconciliation

- Ryot custom collection writes and native viewing-history integration under the collection contract
- local Ryot state snapshots
- Radarr active overlay synchronization
- profile changes
- import handling that ensures Library and Previously acquired
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

Historical design decomposition, not a current build checklist. See [Features](../maintainer/FEATURES.md) for implemented versus pending capability and [Decisions](Decisions.md) for scope changes.

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
| `RyotAdapter` | Resolves metadata and reads native viewing history plus six explicit custom memberships under the Ryot Collection Contract. Collection writes remain unimplemented. |
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

Earlier planning questions and decisions are retained below for traceability. The answered Build Contract, current feature register and D001–D010 supersede conflicting older scope/status. Do not reopen answered questions or activate broad automation from this section; F36–F39 make retained gaps/proposals explicit.

There are no known architecture blockers at this stage. This section tracks implementation-sensitive decisions and risks that must remain visible while building the MVP.

### Download / Import Flow Detail

- Decision: import events update facts and do not create normal user-intent prompts.
- First successful import ensures Ryot Library and Previously acquired if missing; no native Owned write.
- Upgrade import updates runtime facts only.
- Redownload after missing-file recovery closes or revalidates the recovery task.
- Import starts a Jellyfin reception check; timeout creates an admin-only operational issue.

### Collection Automation Boundaries

- Decision: custom Library, Archived, Previously acquired, Deleted, Radarr, and Sonarr memberships follow the Ryot Collection Contract.
- Decision: native Owned is not acquisition history and is preserved by default, with the explicit optional movie-membership cleanup exception in the collection contract. Completed/viewing events remain relevant to watched status.
- Decision: the read-model migration is implemented; automatic membership maintenance remains pending despite completed Ryot setup.
- Decision: preserve the unresolved acquisition exceptions; catalogue approval does not prove acquisition or deletion.

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

An earlier design replaced Downloaded with native Owned and interpreted it as historical inventory. That decision is superseded: custom Previously acquired now represents verified historical acquisition, while native Owned keeps its Ryot inventory meaning. Existing Owned memberships remain preserved; uncertain acquisition histories are not automatically converted. See the Ryot Collection Contract for the accepted model and implemented read-only mapping.
