# Project Workflow

Updated 2026-10-25. This file owns how work is planned, changed, verified and handed over. Product rules belong in the owning contracts; current implementation status belongs in the feature register.

## Start Here

1. Read local agent policy, inspect `git status --short`, and preserve modified/untracked work. Never reset, clean, stage, commit or push by implication.
2. Read [Feature register](FEATURES.md), [Roadmap](ROADMAP.md), [Decisions](../documentation/Decisions.md), and the relevant architecture/collection/display contract.
3. Use [Development](development/README.md) for setup/testing/commissioning and [Release](release/README.md) for readiness. Private installation values may supplement these documents but must not be required to understand the product.
4. Before execution, distinguish approved implementation from native commissioning, exact data batches, automation activation, release publication and destination changes. A roadmap entry authorizes none of those operational actions.

## Ownership And Traceability

| Record | Owns | Does not own |
| --- | --- | --- |
| [Features](FEATURES.md) | Stable F IDs, scope, implementation, availability, evidence links and remaining gaps | Detailed product rules or user acceptance results |
| [Roadmap](ROADMAP.md) | W package order, dependencies, exit gates and DATA tasks | Duplicate feature completion claims |
| [Decisions](../documentation/Decisions.md) | Material changes, rationale, authority and superseded choices | Session narration or credentials |
| [Architecture](../documentation/System%20Architecture.md#coordinated-movie-release--build-specification) | Target behavior and AC requirements | Current deployment/process health |
| [User acceptance](#living-user-acceptance-checklist) | U scenarios, explicit user reports and retest needs | Proof from automated tests |
| Development / release guides | Repeatable execution, recovery and release gates | Private configuration or batch datasets |

Link F → W/AC → tests → U where applicable. Infrastructure features without a user-facing test must say why. Do not create a second status spreadsheet or tracked session log. New requirements receive an F ID before implementation; IDs are never reused or silently deleted. Deferred, rejected and superseded items remain discoverable.

## Change Control During Development

Lessons learned may change scope; they must not silently rewrite the plan.

1. Identify the affected feature and existing contract. Record a material change in the decision register, including the prior choice, replacement, reason, authority/date and affected F/AC/U/DATA IDs. Minor wording/layout corrections need only update their feature/test record.
2. Mark whether the new scope is approved, proposed or deferred. Separate retrospective evidence from newly granted permission. Preserve later restrictions alongside approval.
3. Update the owning contract and roadmap dependencies before or with implementation. State which old instruction is superseded; current guidance must contain only the current rule.
4. Implement the bounded change. Preserve unrelated work and native household data.
5. Update tests and evidence, implementation/availability state and any affected acceptance row. A fix to accepted behavior retains the old report and marks Retest needed with a reason.
6. Complete the reconciliation before calling the feature finished or moving to another slice. If interrupted, record the exact remaining gap and next action in the feature register.

## Definition Of Done And Evidence

A feature is implemented only when its stated scope exists and relevant recovery/authorization checks pass. It is accepted only when the applicable AC and explicitly reported U outcomes are satisfied. Deployment and user acceptance are independent states; do not collapse them into Done or a completion percentage.

Each verification claim needs a date, source revision or dirty-source identity, command/procedure, result and limitations. Source/test links are shareable; sanitized run summaries can be tracked, while raw reports, screenshots, native mappings and backups stay private. An external team must be able to reproduce synthetic checks without private files. Saved PIDs, old test counts and an old zero-issue report are historical evidence, not current health.

For a code change run focused checks and the full unit suite; UI changes require affected real-component browser journeys at 320×640, 390×844, 844×390 and 1280×900. Migration/write changes additionally require fixture preservation, interrupted/lost-response recovery and the relevant deployment gate. Documentation-only changes require record/link/diff checks, not a redundant runtime suite. Commands are in the development guide.

## Resuming In A New Chat

Start with the records above, then inspect relevant code. A private handover may supply installation context, answered questions and evidence locations; reconcile its current summary rather than reconstructing the product from chat history. Trust fresh repository/provider evidence over remembered claims. Do not inspect sibling projects.

Current source contains schema5 and the coordinated movie runtime. The latest recorded Mac deployment is 2026-10-25, with profile editing available and direct Jellyfin intake still off pending setup/native commissioning. This is development availability, not a released Docker deployment or a fresh health assertion. See F10, F19 and F24–F26. D011 authorizes a reviewed Git baseline and fresh-checkout verification. Use the development guide for its recorded result and revision; do not assume a checkout matches a running deployment without that evidence.

## Preparing A Handover

Before handing work to another team or ending a workstream:

- Reconcile every affected F row, decision, contract, U result and roadmap dependency. No unresolved contradictory current instructions.
- Identify branch, HEAD, uncommitted source identity, schema/runtime compatibility, test commands/results, unfinished operations, limitations and next approved action.
- Supply a reproducible clean-checkout setup and synthetic test path. Note any missing Git baseline explicitly; do not imply untracked work is in a commit.
- Separate source implementation, automated evidence, live verification, deployment and user acceptance. Do not promote one into another.
- Keep secrets, host mappings, backups and chat quotations private. Verify `.local/` and local policy files are neither tracked/staged nor dependencies of new project files.
- Run `python maintainer/test/check_project_records.py` and `git diff --check`; use the release guide's clean-checkout gate before declaring external handover ready.
- Update the existing private handover in place with a short current summary. Older checkpoints are history and must not override it.

## Living User Acceptance Checklist

Updated 2026-10-25. This is the user-facing test queue, maintained alongside implementation. Add a stable test ID whenever a new function or separately testable behavior is introduced; do not treat this as a frozen release checklist. Keep the test and expected result short enough to perform from Telegram/Mini App. Update acceptance only from an explicit user report, recording the date and its scope. Automated verification is separate evidence, never user acceptance. Preserve earlier acceptance when unrelated features change; for an affected accepted function, retain the earlier result and mark **Retest needed** with the reason. Split a row if only part of a function is accepted. Record reported failures and their reproduction details before fixing them.

Readiness: **Ready** means available in the Mac development increment; **Deploy first** means source implementation exists but is not yet available there; **Setup first** means configuration or a controlled commissioning step is needed; **Planned** means not implemented. Readiness is not a fresh service-health claim. **Pending** means no explicit acceptance of this exact behavior has been recorded. Hold writer/automation tests marked Setup first until their specific setup is verified; this checklist does not authorize bulk imports, provider writes, integration cutover or cleanup activation.

| ID | Function | What to try / expected result | Readiness | User result |
| --- | --- | --- | --- | --- |
| U01 | Settings appearance | Check grouped sections and the overall Curatarr visual style on your phone. | Ready | Accepted — visual style only; user report before this checklist. |
| U02 | Household or space name | Save a name, reopen Settings and browse a movie. Saved name appears in household headings. | Ready | Pending |
| U03 | Search and navigation | Search a title, then title + year; use Previous/Next and List/Posters. Movie, year and description stay together. | Ready | Pending |
| U04 | Public movie ratings | Read Description row: IMDb logo + /10, TMDB logo + percentage, horizontally. Missing score shows —; another movie never inherits the previous score. | Ready | Retest requested — earlier missing-score report retained; backend fix deployed and live scores verified read-only on 2026-10-24. Unreleased status remains independent. |
| U05 | Library status | Check one watched and one unwatched movie. Status, watch history, Radarr facts and audio are understandable and consistent. | Ready | Pending |
| U06 | Quick Add / cancel | Open Add movie: one fixed-size sheet has Add to download / Register in catalogue tabs. Switch freely without losing inputs; no automatic picker; date field fits; Cancel adds nothing. | Ready | Accepted — user reported the single-sheet layout is much better on 2026-10-24. Subsequent profile/title/watch-control refinements have separate pending checks below. |
| U07 | Catalogue only | For an intentionally selected movie, confirm Catalogue only. It appears in Ryot Library with Curatarr provenance, without a Radarr download request. | Ready | Pending |
| U08 | Add and search | For an intentionally wanted movie, confirm profile/root and add/search. Verified outcome appears; repeat taps do not create duplicates. | Ready | Pending actual download test — on 2026-10-25 the user said the layout seems to work and will select a movie for download soon. Earlier Ghost Soldier addition was reported successful; current native add/search/download outcome is not yet accepted. |
| U09 | Already active / reactivation | Active Radarr movies cannot be added again. Archived movies require explicit reactivation confirmation. | Ready | Pending |
| U10 | Viewing inbox / household answers | On a genuine request, No hides it only for that respondent; first Yes clears other inboxes; all No moves it to Declined viewings. | Ready — requires a genuine pending request and multiple participants for shared behavior | Pending |
| U11 | Five-point rating presentation | Open an optional rating question. Five choices and confirmation are easy to use. | Ready | Accepted — user reported five-point UI works; not acceptance of subsequent native write changes. |
| U12 | Rating saved in Ryot | Confirm a rating on a genuine completed viewing. Ryot shows the same /5 score and readable rater attribution; existing history is preserved. | Ready — requires a genuine rating question | Pending |
| U13 | Declined viewings | Review a household-declined request: poster remains visible; confirm or Review again has the described effect. | Ready — requires a genuine declined request | Pending |
| U14 | Maintenance indicator | Open Sync issues and run Check now. A completed check shows Yes/No with meaningful count/time; unavailable checks cannot claim healthy. Technical repair categories remain hidden. | Ready | Pending |
| U15 | Owned cleanup | After explicitly enabling, verify only movie Owned memberships are removed and last-run summary appears. Watch history and other collections remain. | Setup first — deliberate policy activation | Pending |
| U16 | Telegram prompt cleanup | Enable either/both independent switches. After 36 hours, only the selected answered/unanswered prompts are removed; inbox records remain accessible. | Setup first — deliberate policy activation and elapsed-time observation | Pending |
| U17 | Discovery source settings | Verify TMDB Discover is explicitly named. Save release window, Min. rating, Min. votes and excluded genres; settings persist. | Ready | Pending |
| U18 | Suggestions | After source setup, collect/browse suggestions. Yes opens normal add choices; cancel preserves the proposal; No moves to Passed; Reconsider returns it. | Setup first — source collection and intended choices | Pending |
| U19 | Personal IMDb watchlist | Import a complete export as the historical baseline. Old entries do not trigger downloads; new entries follow the explicitly saved policy. | Setup first — reviewed export/baseline and wanted policy | Pending |
| U20 | Wanted requests | Review a genuinely held wanted request. Reason and available choices are clear; withdrawal stops that request. | Setup first — configured source and genuine held request | Pending |
| U21 | Jellyfin watch intake | With the approved account/thresholds configured, a genuine playback completion or MarkPlayed creates one household question. First Yes saves/verifies one native completion. | Setup first — schema5 runtime deployed; dedicated webhook token/account setup and native commissioning remain pending; intake stays off | Pending |
| U22 | Historical watched-data import | Review an exact occurrence-based batch; append additional Jellyfin watches even for already-watched movies, preserving older history and preventing repeat import. | Planned — occurrence importer required, then native commissioning and exact batch approval; no batch executed | Pending |
| U23 | Unified deletion and restoration | Confirm an external deletion or choose deletion after a viewing; restore through the common acquisition flow. | Planned — implementation/commissioning still required | Pending |

| U24 | Carousel preloading | Browse forward/back after viewing a result. Adjacent posters/status load in the background; no previous-movie facts flash; actions still validate current facts. | Ready | Retest requested — prior per-movie delay addressed by adjacent poster/status preloading and a 30-second per-search cache; cold reads still wait. |
| U25 | List status | Switch search to List. Each movie has a compact status indicator on the right, with explicit loading/unavailable states. | Ready | Accepted — user reported the corrected layout looks good and seems to work on 2026-10-25; acceptance covers observed List presentation. |
| U26 | Release-state label | Distinguish announced/unreleased titles using explicit provider release metadata, independent of missing scores. | Planned — later roadmap item requested 2026-10-24 | Pending |

| U27 | Register a past viewing | Choose Register in catalogue → I've watched this. Date input fits the sheet; leave it blank if unknown. Switch tabs without losing it. Confirm once; one completion is created/reused without download or duplicate prompt. | Ready — use only a genuine movie/history fact | Retest requested — reported date-field overflow addressed. |
| U28 | Optional catalogue rating | In that route, optionally choose one of the five numbered 1–5 buttons; tap again to clear. The rating is privately linked to the completion with rater attribution. Existing different ratings are preserved and explained; leave rating empty to keep them. | Ready — use only an intended genuine rating | Retest requested — dropdown replaced with the requested viewing-style buttons. |

| U29 | Profile buttons and title | Open Add movie. Native profiles appear as one vertical stack of selectable buttons (four in the current installation); selected profile persists across tabs. Heading has no blue focus frame, while interactive keyboard focus remains visible. | Ready | Accepted for observed layout/selection — user report 2026-10-25. Actual download remains U08 pending. |
| U30 | Optional watch-control visibility | In catalogue, leave I've watched this off. Date/rating remain visible at half opacity, grey and disabled. Turn it on to enable; turn it off to omit viewing/rating from saving. | Ready | Pending |
| U31 | Library status actions | Open Library status: duplicate Add and search / Catalogue only buttons are absent. Suggestions Yes/Review choices opens the shared Add sheet directly after fresh checks. | Ready | Pending |
| U32 | Radarr profile change | Library status → Radarr → Change profile: select a different native profile and confirm. Verify the profile changed while monitoring, folder and files stayed unchanged; no search is requested. | Ready — implemented and locally deployed; genuine native save acceptance pending | Pending |

Use IDs such as “U04 works” or “U06 fails: …” in user reports. A broad style approval does not accept data persistence or provider writes. Expand this table as additional agreed functions reach development; keep proposed scope and deployment blockers visible rather than silently dropping them.
