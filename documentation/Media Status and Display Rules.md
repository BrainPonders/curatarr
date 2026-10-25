# Media Status and Display Rules

## History

- `2026-10-21 | Codex | Defined the human-facing status catalogue and progressive disclosure rules`
- `2026-10-21 | Codex | Resolved quality placement, added confirmed monitoring badges, and defined shared safe-area-aware bottom controls`

## Scope & Boundaries

This document owns user-facing media labels, their evidence requirements, display priority, and presentation decisions. [System Architecture](System%20Architecture.md) owns workflows; [Ryot Collection Contract](Ryot%20Collection%20Contract.md) owns durable memberships and native collection boundaries. Display summaries are not a single lifecycle or automatically one collection per label. Movie browsing is the initial implementation; series roll-ups are not implemented.

## Authority (Owned Facts)

- compact and expanded display vocabulary
- required evidence and unknown-data rules
- availability precedence
- pending presentation decisions

## Authoritative Inputs (Consumed Facts)

- Ryot completed viewing history, collections, and durable memberships
- current Arr file, monitoring, quality, and acquisition facts
- Jellyfin exact-provider-ID reception and media-source metadata
- confirmed Curatarr workflow outcomes, when implemented

## Change Rules

- Keep the compact view human-readable; provider names and technical details belong in expanded details.
- Never infer intent from file presence, ownership, or watch history.
- Never convert an unavailable lookup, weak identity, or omitted metadata into a negative fact or zero.
- Labels can be defined before all required integrations exist. Mark unsupported evidence explicitly; do not simulate it in production.

## Information Hierarchy

1. Movie identity: large uncropped poster, title/year, description collapsed by default.
2. Browse and household summary: stable Previous/Next, result count, Watched, household Status, audio, and Library status.
3. Expanded Library status: household status, viewing count and last completed date where recorded, provider evidence, and technical details. Use distinct Ryot, Radarr, and playback-library sections. Do not add a history panel, timeline, inventory row, or collection-membership rows at this stage; preserve their underlying data.

Library status places a single blue-accent summary beneath the movie title: **Watched · Active in Radarr** when both facts are verified. Show only supported facts; confirmed Radarr absence may say Not in Radarr. Do not infer an unwatched status from missing history or availability from a failed check. Routine summary text fits one line at narrow mobile widths, and no duplicate decision narrative appears below the provider sections. Archive/reactivation, identity conflicts and failed history/Radarr checks retain separate attention warnings. Clear the summary and warnings before each refresh so unavailable checks cannot leave stale claims visible.

The browse bottom bar groups Previous, Library status, and Next. With coordinator controls enabled, it also offers a full-width Add movie action above that row, with the result counter above the actions; archived titles use Reactivate movie. Otherwise the result counter remains immediately above the navigation buttons. The detail bottom bar groups Back to results and Refresh. Both button rows share the same lower-edge offset, accounting for device and Telegram content safe areas, and the page reserves the measured active-bar height so its final content can scroll fully above the controls. This supersedes the earlier counter-below-buttons arrangement, which raised the browse controls unnecessarily. List mode retains a result total without a hidden carousel counter dependency. Information is accessible from the header on every screen; About & Credits owns the TMDB logo, link, and attribution notice rather than a browsing footer.

Direct Add reuses the selected result's existing status, without requiring another Library status read. Its separate Catalogue only / Add and search choices preserve watched/archive warnings, fresh native profile/root options and final confirmation. An active Radarr movie, uncertain/conflicting source facts or unresolved own operation disables the shortcut; receipt or browsing is never authorization for a provider write. The existing result/detail navigation and default-off presentation remain available.


### Monitoring Modifier

Beside Available, show `Monitored` or `Unmonitored` only when an exact/confirmed matching Radarr record supplies a recognized runtime state. Omit the badge when Radarr is absent, unavailable, or uncertain; absence is not Unmonitored. This badge is a title-level execution fact, not a quality assessment or an assertion that an upgrade is downloading. It does not change readiness. Monitoring alone does not establish profile upgrade eligibility or whether a cutoff has been met. Actual current quality and size stay in expanded provider details.

Search accepts a movie title with an optional trailing year, with or without parentheses. A numeric-only title remains a title. Quoting the whole title suppresses year extraction. If a year-filtered search finds nothing, retry the literal input so numeric suffixes in movie titles remain discoverable.

Search poster results show public ratings alongside the Description disclosure, on one horizontal row: IMDb as /10 with its yellow wordmark and TMDB as a percentage with its logo. TMDB comes from the search response; IMDb comes only from a strongly matched Radarr movie carrying an IMDb identity. Public scores require a finite 0–10 value and positive vote count; absent/unrated/malformed evidence displays —. A new result clears the previous IMDb score immediately, and late status responses cannot change the current result. These are audience scores, independent of household ratings and Radarr custom-format scores. This display adds no provider lookups; IMDb may be unavailable for movies not in Radarr.

Add movie opens one stable-size sheet with Add to download and Register in catalogue tabs at the top, initially showing download. Header/tabs/footer remain in place; only the shared middle area adapts and scrolls. Tab switches retain profile/folder and watched/date/rating drafts, including during option loading. Opening this sheet must not automatically open a native profile selector. Download shows a vertical stack of native profile-choice buttons and the folder control, with one explicit Confirm add and search button; catalogue shows no download fields and optionally accepts I've watched this, a known past date constrained to the container width, and five numbered 1–5 rating buttons matching Viewing inbox. Tapping a selected score again clears it. When watched is unchecked, its date/rating fields remain visible at half opacity, grey and natively disabled; their retained draft is omitted from saving. Empty date/rating remains unknown/unrated. The internal versioned preview is retained without a separate Review choices screen. A registered past viewing reuses existing completion without inventing repeats or replacing history/ratings. Confirmed writes retain their exact saved request when a reply is lost.

Carousel preloads adjacent poster/status facts in the background and reuses a short per-search cache; do not flash a previous movie's facts. List mode displays a compact status badge to the right of each poster/title, with Checking… and Unavailable when appropriate. Neither cached status nor missing ratings authorizes a write or establishes release state. Release-state labels remain a later roadmap item.

Library status no longer duplicates Add and search / Catalogue only actions: search Add owns registration. Source Yes/Review choices opens the shared Add sheet directly after fresh eligibility/pending checks, rather than requiring acquisition buttons on Library status. The noninteractive Add heading has no visible programmatic-focus outline; focus indication remains on actual controls. The user clarified profile editing as the desired Radarr management action. Change profile is implemented beside the current profile, with native vertical choices, current selection, explicit confirmation, versioned server validation and verified read-back. No search is requested; monitoring, path and files are preserved. Inapplicable controls remain visible disabled. Unknown outcomes remain pending against the same operation. F10/U32 track closure and genuine native-save acceptance.

## Household Status

The Ryot section is the first-level household overview: Status, Watched, and Last watched are always visible. Show a dash for an unrecorded date and Not confirmed when a failed or uncertain lookup prevents confirmation. Library record, Inventory history, and Workflow record are implementation concepts and are no longer displayed. The adapter reads explicit Library, Previously acquired and Deleted memberships; Owned no longer supplies acquisition state. A known record without explicit Library membership remains known, with a synchronization note instead of automatic enrolment. No memberships are removed or renamed by presentation work.

Keep ordinary detail fields visible when empty, rather than changing the table structure between titles. Radarr retains Status, Profile, Upgrade goals, Format score, Current quality, File size, and Movie ID even without a matching entry or downloaded file. A confirmed absent entry/file uses a dash for its inapplicable facts; incomplete, failed, or identity-uncertain evidence uses Not confirmed. Preserve real zero and negative scores. Jellyfin retains an Audio row when no copies exist, using a dash for confirmed absence and Not confirmed for uncertain evidence; existing copies retain their separate audio rows. Warnings, diagnostic Library check rows, conditional badges, and action-specific notices remain conditional, not empty permanent rows.

| Code | Label | Meaning |
| --- | --- | --- |
| `available` | Available | A ready matching Jellyfin copy is confirmed; green |
| `wanted` | Wanted | Present in Arr or recorded as active in Ryot, without a confirmed ready copy; blue, independent of monitoring |
| `archived` | Archived | Explicit Ryot intent that the title is no longer wanted; neutral grey |
| `recorded` | Recorded | Known in Ryot without active acquisition/retention or archive intent; darker neutral |
| `unknown` | Unknown | Required checks succeeded but no matching Ryot record, ready copy, or active Arr record was found |
| `unavailable` / `needs_review` | Status unavailable / Identity needs review | Required evidence is unavailable or cannot be safely matched, not proof of absence |

This is currently a derived overview (`library_status` in the API), explicitly identified as such in the UI; it does not pretend that Available/Wanted collections or reconciliation writes already exist. A successful Arr/Jellyfin observation without a Ryot record is surfaced with a synchronization note. Ryot lookup failures do not become unknown records. Archived intent takes precedence over live availability: if an archived title still has a copy or Arr entry, retain Archived and show the conflict for review. A live Arr entry means wanted even when unmonitored. A durable active overlay with no live Arr entry similarly requires review, not silent archival. Getting ready is omitted from the household overview; the title remains Wanted until readiness is confirmed. Detailed provider availability remains separate below.

### Secondary Deleted Badge

Deleted is a secondary historical fact, never the primary status or an inference from Owned, Previously acquired, Archived, or missing files. Display it beside Archived, Wanted, or Recorded only if verified historical deletion exists for an exact stable movie identity and current successful Arr/Jellyfin checks establish no copy. Hide it while a ready copy or imported replacement file exists, or absence is uncertain. Its meaning is "a previous copy was confirmed deleted," not "this title is forgotten." The target durable source is Ryot Deleted membership under the collection contract; hide the badge without erasing historical membership after reacquisition.

The existing internal audit store has a typed `media_file_deleted` evidence contract: verified success from Curatarr or explicit admin verification, recorded only after deletion has actually been checked. Arbitrary missing-file observations and failed/unverified events do not qualify. The badge now reads Ryot Deleted membership as durable evidence and retains exact-identity verified audit evidence as a fallback. A known Ryot record without matching Deleted membership receives a synchronization note when only local evidence exists; no collection is written. A matched Arr record must explicitly confirm no file before the badge can show. The read-only UI provides no endpoint to assert deletion or change media. Controlled-removal capture and automatic collection maintenance remain pending, so older missing/deleted files are not backfilled or labelled by guesswork. No user-facing history screen is introduced.

## Provider Availability Catalogue

| Code | Compact label | Required evidence | Implementation |
| --- | --- | --- | --- |
| `ready` | Ready to watch | Matching movie indexed in Jellyfin with a non-placeholder media source | Read-only Jellyfin lookup; not an independent playback or filesystem probe |
| `not_available` | Not available | Successful Jellyfin check confirms no ready playback copy, regardless of Arr monitoring or file presence | Implemented; does not mean never owned or never watched |
| `unknown` | Check unavailable | Jellyfin evidence unavailable, incomplete, or identity uncertain | Implemented; the Library check row distinguishes a failed check from identity review |

Priority:

1. Any confirmed ready version wins, regardless of quality, profile compliance, upgrade monitoring, another version downloading, or an Arr outage.
2. Unresolved Jellyfin evidence yields Check unavailable, not a negative assertion.
3. A successful Jellyfin check with no ready copy yields Not available, even if Arr has an imported file, is monitoring the movie, or is unavailable.

Jellyfin does not acquire movies. Searching and Getting ready are not Jellyfin availability labels; acquisition and monitoring belong in the Arr section. The household overview remains Wanted until readiness is confirmed.

Deleted is now a secondary household badge governed above, not a provider availability value.

A stalled preparation is not silently converted into a deletion or intent change. Reception timeout detection and admin issue routing remain reconciliation work. The initial display does not promise an ETA or infer activity from a recent search/add click.

## Watched Catalogue

History is household-wide, from the configured shared Ryot account, not a per-Telegram-user viewing record. Cinema and external-home viewings can be recorded without ownership or current availability.

| Evidence | Compact display | Expanded display |
| --- | --- | --- |
| Complete history with zero completed events | Watched: 0 recorded times | In-progress events are not completions |
| Completed events | Watched: 1 time / N times | Last recorded completion as `dd-mm-yyyy` where known |
| Completed membership or legacy watched evidence without an event count | Watched; count unknown | Explain that the history proves a viewing, not its count/date |
| No matching household record | No viewing record | Not proof nobody watched it |
| Lookup failure | History unavailable | Never show zero |
| Unconfirmed identity | Identity needs review | Do not expose an uncertain match as the movie's history |

Only `finishedOn` on completed viewing events supplies a completion date. Last-edited time is not a viewing date. If completion dates are missing, do not claim the most recent known timestamp is necessarily the last viewing. A Completed membership with no completed events is not a zero count.

## Audio Catalogue

| Evidence | Compact display | Expanded display |
| --- | --- | --- |
| Current indexed media-source audio tracks | Audio language badges | Preserve source/version grouping and original language codes |
| Multiple ready versions | Language badges plus explicit "Across N copies" qualifier | List tracks per version; their union does not prove a single bilingual copy |
| Missing/undetermined track language | Audio not confirmed, or an Unknown track badge alongside known languages | Do not infer Original, English, or French |
| No current source / failed lookup | Audio not confirmed | Do not reuse a desired profile or deleted copy's tracks |

Subtitle languages are never audio evidence. NFO tracks are advertised pre-download evidence and must not populate current-copy audio badges. NFO enrichment remains deferred.

## History And Intent Catalogue

| Fact | Meaning and placement | Implementation |
| --- | --- | --- |
| Library membership | Explicit deliberate household catalogue membership, retained after archival/deletion; not proof of a prior lookup | Explicit custom collection read; known records outside Library produce a synchronization note |
| Previously looked up | Explicit deliberate Curatarr interaction, with a date; never implied for all search results | Pending interaction tracking |
| Previously acquired | Verified historical managed-copy acquisition; retained after deletion, without asserting current availability | Explicit custom collection read; no acquisition inference from Owned |
| Deleted | Verified deliberate removal of a previous managed copy; durable history with conditional badge visibility | Explicit custom collection read, with verified exact-identity local audit fallback and synchronization note |
| Native Owned | Ryot inventory, not Curatarr acquisition history or operational intent | Raw provider membership retained; not mapped to operational durable state |
| Native Completed / viewing events | Watched classification; actual events supply counts/dates, independent of ownership or acquisition | Existing read; supporting collection membership must not invent event counts or dates |
| Archived | No longer wanted in active processing, watched or unwatched; requires explicit reactivation | Existing Ryot membership read |
| Radarr / Sonarr overlays | Durable active membership, not a standalone user-facing workflow label | Reads contribute to household status; reconciliation is separate work |

Archive confirmation must explicitly say that it deletes any current managed file and removes the title from Arr while preserving history. The archive write workflow is not enabled by this UI work. Potential reasons such as Not interested, Finished with it, and Cancelled are not new core states; reason capture remains pending.

## Watch Check-in

Viewing inbox is everyday input for every configured user. Show Yes, I watched this / No, I did not watch this. A personal No hides that request from that user; the first Yes clears everyone else's pending request immediately. Offer optional 1-5 rating/confirmation or No rating only to the confirming user. The eye badge counts unique movies still needing that respondent's input, including legacy disputes with no proven collective answer; hide it at zero while retaining the eye. Unanimously declined requests appear only under Admin → Declined viewings, where a verified confirmation can recover the viewing without deleting history. Each repeat viewing has its own date/actions and shared Telegram state. These temporary answers do not create per-user watch histories.

## Admin Maintenance Presentation

Maintenance is a separate admin-only view. The default Sync issues surface has a compact System check with Maintenance needed: Yes/No, last checked and Check now; unknown/incomplete evidence cannot claim No. Known findings remain Yes with an incomplete-check explanation after an outage. The tab badge uses ! for attention, a check mark for verified healthy and ? for uncertainty. Technical Viewing requests is hidden in normal mode; its findings still affect the alert. Declined viewings retains only household confirmation and Review again. Declined viewings and Settings remain separate. Keep the header's brand/info group, eye and conventional gear; standard users do not see Admin. No detailed library categories, movie-by-movie diagnostic cards, skip controls or acquisition Correct button are visible by default.

The following diagnostic layout is retained behind maintenance.show_diagnostics, default false. It is a maintainer capability, not a household Settings switch. Existing categories, reports, filters and progress remain available for future selective exposure; hidden findings still affect the system check.

Show one freshly validated movie with a compact uncropped poster beside its title/year, identifiers and check time, followed by explanations and text-only suggested next steps. Reserve a stable 2:3 artwork area (96px wide on narrow screens, 112px on wider layouts); missing or failed artwork uses a same-size placeholder. Load artwork separately by the subject's exact TMDB movie ID, never by a title search. Poster lookup or image failure must not block factual review, and delayed responses must not replace the next movie's artwork. Posters are recognition aids, not identity or availability evidence. Keep Previous, Next, Skip, and Refresh in a stable bottom row with Movie X of Y above it. Skipped movies remain unresolved and available separately; per-admin progress survives reopening. During checks and provider failures, hide prior movie evidence rather than presenting it as current. Empty/filtered queues have an explicit message. No monitor, archive, collection-write, or deletion buttons are enabled. The API enforces admin access even when someone bypasses hidden navigation.

## Presentation Decisions

| ID | Question | Current rule | Status |
| --- | --- | --- | --- |
| `UI-QUALITY-01` | Add a compact quality badge beside Ready to watch, or show quality only after expanding Library status? | Quality stays in expanded Library status. Every ready copy qualifies regardless of quality or upgrade profile. The compact modifier is confirmed Monitored/Unmonitored, not Upgrading/Final. Multiple versions must not be merged into a fictional quality/language combination. | Resolved: user agreed |
| `UI-OWNERSHIP-01` | Rename or repurpose native Owned as historical acquisition? | No. Preserve native Owned; custom Previously acquired stores verified historical acquisition. Follow the Ryot Collection Contract; do not infer deletion or availability. No inventory/history panel. | Resolved; read-only mapping implemented |

## Provider Details And Colours

Use neutral charcoal/grey surfaces and Radarr-yellow primary controls. Provider headings keep neutral text; only their vertical accent lines identify the provider: Ryot grey, Radarr yellow (`#FFC230`), and Jellyfin purple (`#AA5CC3`). Yellow and purple follow the respective official logo assets ([Radarr](https://github.com/Radarr/Radarr/blob/develop/Logo/Radarr.svg), [Jellyfin](https://github.com/jellyfin/jellyfin-ux/blob/master/branding/SVG/icon-transparent.svg)). History, intent, overlays, and audio labels stay neutral and are not recoloured by provider. Curatarr deliberately uses independent availability and monitoring colours, not Radarr's combined four-state colour scheme. The same blue/grey monitoring rule applies to the compact modifier beside readiness.

All three expanded provider sections use a single label/value table, without a separate badge strip. Ryot shows household Status with conditional Deleted, viewing count, and a persistent Last watched row; no technical membership rows or history panel. Radarr shows a single Status row with two adjacent badges: green Downloaded or grey Not downloaded, then blue Monitored or grey Unmonitored. These are independent facts: disabled monitoring is not a failed download, and enabled monitoring is not proof that an upgrade is allowed or in progress. Keep the pair on one line, including narrow mobile viewports; do not reintroduce separate Presence, Monitoring, and File rows. Jellyfin shows availability and audio per copy; diagnostic library-check rows appear only for unavailable or uncertain evidence, not as a repetition of ready availability. The Information legend explains each independent label.

| Curatarr label | Badge colour | Evidence |
| --- | --- | --- |
| Downloaded | Green | Matching Radarr record confirms a file, regardless of monitoring |
| Not downloaded | Grey | Matching Radarr record confirms no file; not proof of deletion or failure |
| Monitored | Blue | Matching Radarr record confirms monitoring enabled, regardless of file presence |
| Unmonitored | Grey | Matching Radarr record confirms monitoring disabled; not proof of final quality |
| Ready to watch | Green | Matching Jellyfin media source confirmed, regardless of Radarr state |
| Confirmed informational facts, e.g. English / French audio | Darker neutral | Known current-copy information, not an inactive setting |
| Matched upgrade goal | Green with check mark | Relevant Radarr file classification or matched custom format |
| Unmatched / unknown upgrade goal | Light grey with dash / question mark | Not matched versus unavailable evidence; do not conflate these |

Ready to watch stays a Jellyfin fact and remains green even when Radarr is unmonitored, missing, or unavailable. Neither an Arr file nor monitoring alone establishes readiness. Never infer queue/release status from a missing file; these unimplemented states are omitted from the current legend. The Information page includes household status, Deleted evidence requirements, and the colour legend. Text labels always accompany colours. Unknown or unavailable evidence must not become Not downloaded or Unmonitored. Orange/red remain reserved for preparation, attention, or genuine failures rather than ordinary absent-file/disabled-monitoring combinations.

Expanded Jellyfin facts use provider availability, with separate neutral audio badges for each copy. Expanded Radarr details include current quality and size plus a Profile row in the same style as other facts. Preserve the profile name verbatim, including `>` separators. Omit the routine Upgrades enabled sentence now that goals are visible; retain exceptional Upgrades disabled or unavailable-policy notices. The exact quality cutoff remains on the profile tooltip. Never infer language or release requirements from the profile name. The cutoff is not a claim of Final quality or overall profile compliance: custom-format scores and all acquisition protections remain Radarr's responsibility. Missing profile evidence is explicit. This additional read runs only for details; its failure does not invalidate movie/file or readiness evidence. No profile, monitoring, or Ryot collection writes are enabled.

### Informational Badges

Use darker neutral badges for confirmed playback audio languages in both browsing and details, and factual durable overlays. Reserve the lighter neutral style for off/unavailable/unmatched states. In dark mode the informational surface stays darker with a visible inset border and readable light text. Unknown track and Not confirmed remain explicit light-neutral labels, never styled as confirmed languages. Colours supplement text rather than replace it.

### Upgrade Goals And Format Score

Expanded Radarr facts now include Upgrade goals and Format score. These are a quick indication of current versus preferred attributes, not a full eligibility audit, proof of active searching, an explanation of a manually selected monitoring policy, or a promise of Final quality. Show goals even for unmonitored movies; opening Radarr remains necessary for complete scoring and release decisions.

- Read the file's current native `customFormatScore` from `GET /api/v3/moviefile/{id}`, not the embedded movie-list file payload (which omits score/formats on the commissioned instance). Preserve zero and negative scores. Show score/selected-profile upgrade target on one line, e.g. `175/10.000`, using dot grouping. The denominator is `cutoffFormatScore`, not a maximum, percentage or movie rating. Omit the denominator if the score/target is unknown or upgrades are disabled. This is a current-profile assessment, not a historical grab-time score. Targets can exceed attainable scores; never invent a maximum.
- Verify both file ID and owning Radarr movie ID before exposing file details. Keep the returned payload limited to score and classification facts, not filesystem paths. New file/profile/custom-format reads happen only for expanded details, not poster browsing.
- Derive resolution goals from structured cutoff-quality metadata. A uniform 2160p cutoff yields 4K; a mixed-resolution group instead uses Radarr's native Quality target assessment. A strict Remux-only cutoff yields a non-bonus Remux goal; a group allowing non-Remux qualities does not.
- The profile's explicit language yields an audio goal. Original audio uses the movie's typed original-language ID; absent original-language or incomplete file-language evidence stays unknown. This is Radarr classification, not independent video-track inspection.
- Add supported, positive-score core custom formats as bonuses. Recognize simple resolution, Remux-modifier, and non-negated language definitions from their structured specifications, not format/profile names or household-specific IDs. Match these goals using the file's native matched-format IDs, without evaluating release rules or recalculating scores. Do not duplicate a native cutoff/language goal as a bonus. Zero/negative weights, unsupported composite/title rules, and operational exclusions do not become wanted features.
- Language conjunctions remain whole format goals (e.g. English and French bonus); do not split them into separately asserted tracks. Additional-audio bonuses remain the format's complete classification. Desired preferences or CF matches never populate Jellyfin's current-audio badges.
- Green/check means matched; grey/dash means not matched; grey/question mark means unknown. A confirmed absent file leaves goals unmatched, while a failed file lookup leaves them unknown. If format definitions fail, preserve native goals and expose Format goals as unknown. Failed profile/file/format reads must not invalidate independently established runtime state, history, or readiness.

These reads do not change profiles, monitoring, Ryot collections, acquisition, or the deferred NFO roadmap.

### Maintenance Corrections

New viewing questions use **1-5**, with no default selection and a separate rating confirmation. Legacy questions retain **0-10**, including zero. Backend conversion/read-back uses canonical 0-100 values and respects the supported Ryot OUT_OF_FIVE or OUT_OF_HUNDRED preference without changing it. Telegram and Mini App confirmation refer to the same existing shared viewing event, not a per-user or duplicate watch. Viewing inbox submits the same validated answers as Telegram. Observation/delivery is inactive until explicitly bound; disputed records and uncertain writes have distinct notices. The first Yes immediately clears watch confirmation for all other respondents. The confirming user's optional rating closes after verified submission, acceptance of an existing rating, or No rating. Unanimous declines move to Admin review. Admins can also explicitly clear a stale request only after fresh verification that its original native event is absent, without claiming a watch confirmation or changing Ryot history. Retention is not enabled by this slice.

The retained legacy diagnostic layout used compact Viewing follow-up / Sync issues tabs. The current default has a user-facing Viewing inbox and admin Sync issues / Declined viewings / Settings. These are separate queues, not issue-category filters. Viewing shows genuine pending household questions and freshly read Ryot watch facts with a recognition poster, in one list without Pending/Postponed buttons or a Postpone action. Navigation cannot clear a question, and unanswered questions do not disappear on expiry. Previously postponed questions return to the main list. Empty and unavailable states are distinct, and the current not-yet-enabled delivery/write boundary is explicit. Do not populate viewing from technical findings or assume downloaded means watched. Sync has one issue dropdown: All pending issues and each issue category exclude skipped titles; the Skipped option shows parked findings. Separate Pending/Skipped buttons are removed, while the sync Skip command remains. Branch switches must ignore late responses and artwork from the previous branch. Keyboard tab navigation and the same safe-area bottom docking apply to both views.

The compact Admin toggle sits in the right-hand header tools beside Information, separated by a small gap. Identity findings show the specific reason and a provider comparison of title, year, and typed identifiers. Label Radarr as the acquisition reference; label Ryot records as possible matches until exact identity is established. A title-only match reads Same title, different catalogue record. Guide the admin toward the Ryot entry corresponding to Radarr's target, not changing Radarr to fit an unrelated Ryot entry. No automatic merge, identity rewrite, or history transfer is offered.

Each finding uses three lines: a bold conclusion with the existing attention border, a plain-language explanation of the evidence, and the proposed correction. Avoid generic headings such as Acquisition evidence as the main conclusion. The single-movie Correct button is available only for missing Previously acquired membership; a confirmation names the movie and explains the exact additive Ryot change. Fresh Radarr imported-file data is sufficient acquisition evidence; do not ask the admin to inspect the network drive. It is not Jellyfin readiness evidence. Unsupported corrections remain explicit guidance and can be skipped. Confirmation cancellation performs no provider write. No batch controls exist. After a request, distinguish verified membership from accepted/pending or unconfirmed outcomes; failed requests must not claim that nothing could have changed. Browsing remains read-only; maintenance is labelled separately.

The maintainer diagnostic Declined viewings and Sync issues → Viewing requests use compact uncropped exact-ID posters with a fallback. Confirm household watched, Review again, Clear viewing history and Clear stale request stay visible in that mode; unavailable buttons are grey with readable reasons. Review again reopens the existing event for household answers. Clear viewing history opens a separate movie-specific confirmation counting all viewing records, including repeats, and explains that ratings/reviews and library intent remain. A No answer never deletes history.

Sync issues keeps a Viewing requests control (a filter group in the opt-in diagnostic UI) for missing original events, events no longer completed and pending history resets. Keep existing household-declined events under Declined viewings. Positively verified mismatches leave the viewing inbox/count without an extra notice. Sync badges include these request issues; decline badges exclude them. Current exact validation, not a provider outage or identifier conflict, establishes a mismatch. Restored records rejoin the appropriate queue; no question closes automatically.

The normal Mini App detects and alerts technical inconsistencies but does not present their repairs. The Viewing requests group and technical correction controls remain hidden behind maintainer diagnostics. Ordinary Declined viewings offers confirmation and Review again only; no stale-close or history-reset controls are shown. Technical repair POSTs are also blocked in normal mode, not merely hidden. All findings remain stored and monitored.
