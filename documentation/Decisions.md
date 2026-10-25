# Product And Delivery Decisions

Updated 2026-10-25. This register owns material changes in direction. It records settled decisions retrospectively from user instructions and owning contracts; it does not grant new operational permissions. Detailed rules remain in [Architecture](System%20Architecture.md), [Collections](Ryot%20Collection%20Contract.md) and [Display](Media%20Status%20and%20Display%20Rules.md). Current build state belongs in [Features](../maintainer/FEATURES.md).

## Recording A Change

Append a stable D ID with date, authority, status, previous choice, replacement, rationale and affected records. Dates below are established decision dates or explicitly labelled record dates; do not invent historical approval dates. Retain superseded decisions and link their replacement. Update affected contracts/features/tests in the same workstream. A proposed alternative remains proposed until accepted. Private conversation quotations and installation evidence stay outside this register.

## Decisions

| ID | Date / authority / status | Previous choice → current decision | Reason and affected records |
| --- | --- | --- | --- |
| D001 | 2026-10-24 user accepted Q01–Q22; Approved | Direct service automation → Curatarr coordinates policy, Ryot stores durable facts; Jellyfin/Arr supply evidence and execute supported actions | One policy owner, shared durable operations and confirmation/recovery. F06–F25; W01–W09; AC01–AC20. Retiring native integrations requires separately verified cutover, not this planning approval. |
| D002 | Recorded 2026-10-25 from prior explicit user choice; Approved | Proposed /change_name → editable Settings name only, default Household | Keep simple admin configuration; F27, F35, U02. Chickenhouse is this installation's chosen value, not a hardcoded product default. |
| D003 | 2026-10-23 user direction; Approved | General Mini App repair categories → routine household actions plus comprehensive read-only alerting; technical repairs hidden for maintainers | Rare drift is investigated through repo; preserve categories/code rather than deleting. F13–F14; U13–U14. Recurrent simple cases may receive separately scoped UI later. |
| D004 | 2026-10-23 user direction; Approved | One person's No could settle a shared watch → individual answers, first Yes settles household; all participants No yields declined review | Shared history without a personal catalogue. F11–F13; AC08; U10–U13. Unanswered users stop seeing confirmed requests. |
| D005 | 2026-10-24 user answers/list clarification; Approved | Treat every personal-list presence as a download command → historical first snapshot handled; only genuinely new eligible wanted entries enter durable policy | Prevent re-download after watch/removal. Personal IMDb wanted entries bypass discovery voting; discovery remains reviewable. F15–F17; AC04–AC05, AC13–AC15; U18–U20. Complete CSV fallback pending real export; automatic access unresolved. |
| D006 | Recorded 2026-10-25 from user-selected timeout; Approved | Requested 72-hour cleanup → two independent answered/unanswered toggles at 36 hours | Agreed feasible timeout; message removal does not resolve requests or erase watches. F29; U16; AC08, AC19. Toggles remain off until deliberately enabled. |
| D007 | 2026-10-25 explicit user clarification; Approved | Import only Jellyfin movies missing Ryot completions → append attributable additional Jellyfin occurrences including already-watched movies | Jellyfin commissioned after Ryot; preserve older Ryot/Trakt history. F20; DATA01; AC07, AC09, AC18; U22. Supersedes the 25-title missing-only selection. Source occurrence dedup remains mandatory; counts/latest dates do not reconstruct full history. Exact batch execution still unapproved. |
| D008 | 2026-10-24–25 user feedback; Approved | Separate/long Add pages and selectors → single stable two-tab Add sheet, final confirmation, vertical native profile buttons; List status on right | Catalogue may optionally register past watch/rating without download. F03, F05–F08; U06–U09, U24–U31. Layout acceptance does not establish actual native download acceptance. |
| D009 | 2026-10-25 user go-ahead; Approved | Proposed Library status management → explicit existing-movie Change profile beside Radarr profile | Bind actor/movie/native options/state; confirm one target and verify without requesting search or changing monitoring/path/files. F10; U32; AC03, AC17–AC19. Native save acceptance pending. |
| D010 | 2026-10-25 user go-ahead after workflow review; Approved | Status reconstructed from narrative checkpoints → feature/roadmap/decision/test ownership and reproducible handover gates | Capture lessons learned in the same change; separate implementation, deployment and acceptance. F26; all W/AC/U records. Does not authorize staging/commit/push, historical batches, deployment cutover or publishing. |

| D011 | 2026-10-25 explicit user instruction; Approved | Earlier workflow task withheld Git execution → complete baseline review, commit, fresh-checkout verification and synchronization to the existing development branch | User requested full completion including Git sync before further development. F26; W01/W09. Preserve private files and unrelated remotes/branches; no force push, production cutover, native batch, image publication or merge to main implied. This supersedes D010's execution restriction for this scoped Git task. |

## Decisions Still Needed

- F17: supported automatic personal-list access and whether to replace discovery provider; current TMDB Discover/CSV paths remain explicit.
- F19/F25: actual playback account allowlist and commissioning/cutover sequence; conservative saved-preview account is an assumption, not an accepted multi-account batch.
- F20: evidence for earlier occurrences in multi-play source items; decide treatment of unresolved history only after investigation.
- F24–F26: final release identity and concrete deployment commissioning remain execution gates; the development Git baseline is being verified under D011.
