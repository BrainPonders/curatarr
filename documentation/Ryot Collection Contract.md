# Ryot Collection Contract

## Scope And Status

This document owns the Ryot collection mapping, membership semantics, and maintainer handoff for Curatarr. [System Architecture](System%20Architecture.md) owns orchestration and corrective workflows; [Media Status and Display Rules](Media%20Status%20and%20Display%20Rules.md) owns presentation.

### Single-Movie Acquisition Repair

For acquisition-related identity reconciliation, prefer Radarr's configured IMDb/TMDB identity as the operational reference: those identifiers determine what Radarr searches for, independently of Ryot. Find or add the corresponding Ryot record; do not retarget an unrelated existing Ryot record or transfer its viewing history merely because the title is the same. This is an operational preference, not proof Radarr metadata is infallible. Missing, duplicate, or internally conflicting identifiers still require review. Ryot remains authoritative for durable catalogue intent and viewing history.

The retained, default-hidden admin diagnostic Correct action can add only Previously acquired to an existing, unambiguously matched Ryot movie. A complete fresh Ryot/Radarr scan must still show a non-archived exact-ID match and a Radarr imported-file record. Per the household policy, that Radarr database evidence is sufficient historical acquisition evidence; no network-drive inspection is required or claimed. It does not establish current Jellyfin readiness.

The action requires explicit confirmation, checks the existing custom collection belongs to the configured Ryot user, adds one metadata entity, reads back membership, and audits requested/verified/submitted/unconfirmed outcomes. Ryot's job acceptance alone is not verified completion. Other memberships, viewing history, Arr policy, and media remain untouched. General adapter state writes remain disabled. Automatic maintenance and other corrections remain unimplemented. No in-app batch correction exists; any commissioning batch is a separate, explicitly authorized operation outside the app.

The Ryot maintainer reports setup completed under ORION-104 on Ryot v10.5.0 Community. Custom collection creation and membership writes were verified through the normal API on that installation. This supersedes the earlier uncertainty about Community support; it is not a compatibility guarantee for other versions.

Curatarr's read-only adapter implements the six custom collection mappings below. Library requires explicit membership; native Owned remains visible as raw provider data but supplies no acquisition state. Deleted membership supplies durable deletion evidence, with exact-identity local verified audit evidence retained as a fallback and a synchronization note if that fact is not recorded in Ryot. An explicit on-demand movie reconciliation command persists findings but makes no provider changes. The separate diagnostic acquisition repair is one narrow writer. Coordinated catalogue/add/reactivation and Curatarr provenance writes are now implemented; see F06–F10 in the [feature register](../maintainer/FEATURES.md). An optional daily native Owned movie-membership cleanup is described below; unified controlled deletion and general automatic custom-membership maintenance remain incomplete. A daily read-only movie maintenance check now audits these records without repairing them.

Configuration supports `library`, `archived`, `previously_acquired`, `deleted`, `radarr`, and `sonarr` under `ryot.collections`. Newly introduced keys default to the canonical names for compatibility with existing local configurations. A legacy `owned` key is ignored, never treated as an alias for Previously acquired. All six names must be distinct custom collections; mappings to native collections are rejected. Names are resolved case-insensitively; installation-specific IDs are not required by this read adapter. Missing or malformed collection evidence fails the read rather than silently clearing memberships. A known user record without Library remains known but carries a catalogue synchronization note; it is not automatically enrolled.

## Collection Mapping

Use these exact custom collection names. Memberships are independent facts, not mutually exclusive lifecycle stages.

| Custom collection | Durable meaning | Membership rules |
| --- | --- | --- |
| Library | Deliberately retained in the household catalogue. | Explicit membership, not mere existence of provider metadata or appearance in search results. Keep after archival or deletion. |
| Archived | Explicitly no longer wanted on the household media share. | May be watched/unwatched and acquired/never acquired. Requires confirmed reactivation before renewed acquisition. Does not prove deletion. |
| Radarr | Active movie acquisition or retention intent. | Reconcile with Radarr presence regardless of monitoring. Remove after completed archival; not a record of historical acquisition. |
| Sonarr | Active series acquisition or retention intent. | Reconcile with Sonarr presence, with show-level semantics. Remove after completed archival. |
| Previously acquired | The household has had a managed media copy at least once. | Add after verified acquisition/import evidence; a fresh Radarr imported-file record suffices, without network-drive inspection. Preserve after deletion. Arr registration or viewing history alone is insufficient. For a series, at least one acquired episode qualifies; this does not imply the whole series was acquired. |
| Deleted | A previous managed copy was deliberately deleted and the deletion was verified. | Record only after a verified successful removal, not on the basis of missing files, Archived, Owned, or Previously acquired. Preserve as a historical fact after reacquisition. Routine upgrade replacement does not qualify. |

In the intended maintained model, each title in a Curatarr-managed collection also belongs to Library. Archived and active Radarr/Sonarr memberships must not coexist after a completed archive; temporary or external conflicts require reconciliation rather than silently choosing an action.

## Native Ryot Collections And Viewing History

The separate Telegram viewing workflow now observes stable native completed-event IDs. Confirming an observed watch audits household agreement in Curatarr; it does not create another completion or rewrite dates. New questions offer an explicitly selected and confirmed 1-5 rating, created as a private review and linked to the existing event through supported APIs with read-back. The adapter supports native OUT_OF_FIVE and OUT_OF_HUNDRED input, preserving Ryot's normalized 0-100 stored values; the development guide owns the exact conversion and recovery procedure. New reviews use readable movie/date provenance and an event-specific reference; existing reviews and legacy uncertain-operation markers remain unchanged. Legacy 0-10 questions retain their original scale; historical ratings are not reinterpreted. Disagreement removes the normal Telegram buttons but retains an open maintenance question without deleting native history or creating a rating. Existing linked reviews are preserved; accepting their current rating requires no mutation. No completion is inferred from Owned, download state or a rating. This legacy observer path adds no custom collection or collection-driven archive/deletion. Direct Jellyfin intake is a separate implemented, default-off path requiring setup/native commissioning (F19); it is not activated by observing legacy events. See the development guide for opt-in activation and uncertain-write handling.

- **Owned:** native physical/digital inventory, outside Curatarr's operational status model. Do not rename/delete the system collection or infer acquisition history from it. Preserve memberships by default. The optional admin Settings policy can remove movie Owned memberships daily, including existing movies, when explicitly switched on; no first-run review is required. Series/other entities, other collections, watches, reviews and files remain untouched. This narrow policy does not establish or migrate Previously acquired history. The development guide owns its execution/recovery procedure.
- **Completed:** remains relevant as supporting watched classification. Actual completed viewing events supply watch counts and dates. Completed membership without usable events must not produce an invented count or date. Watched does not imply ownership, acquisition, availability, archive, or deletion.
- **Watchlist, In Progress, and Monitoring:** remain under Ryot's consumption/notification semantics. Do not substitute them for Curatarr's active Arr memberships or Arr monitoring. Their automatic transitions must not initiate acquisition or archive actions.
- Do not create a custom Watched collection. Preserve native viewing events, including cinema and other external viewings, and retain the accepted shared-household scope.

Ryot's documented automatic collection rules are described in [Collections](https://docs.ryot.io/concepts/collections). The installed API verification above is the deployment evidence for this setup.

## Derived Display And Authority Boundaries

Available, Wanted, Recorded, and Unknown are display summaries, not additional Ryot collections. Archived is durable intent and takes precedence over conflicting live state; report the conflict rather than concealing it. Jellyfin establishes current readiness and audio. Arr establishes profiles, scores, monitoring, and managed-file state. Neither Previously acquired nor Deleted establishes current absence or presence.

The Deleted badge combines durable Ryot Deleted membership with successful current checks establishing no ready or imported replacement. Hide the badge while a replacement exists or current evidence is uncertain; do not erase the historical membership. Curatarr's execution audit retains identity, provenance and verified outcome details. It supplements rather than replaces Ryot's durable fact. Exact-identity verified local deletion evidence can supply a fallback, with a synchronization note when a known Ryot record lacks Deleted membership. This is not permission to write memberships. Contradictory evidence requires review; an absent membership is not proof that no deletion ever occurred.

## Controlled Operation Rules

- Successful first import: ensure Library and Previously acquired, and reconcile the active Arr membership. Do not write Owned or fabricate viewing history.
- Controlled archive: record Archived intent, delete managed files, remove the Arr record, verify each outcome, then remove active membership. Add Deleted only if a managed copy was actually deleted and that outcome was verified. Keep Library, Previously acquired, existing Deleted, and viewing history.
- Archive without a downloaded copy: keep the same intent/history rules but do not add Deleted or Previously acquired.
- Partial removal failure: retain enough workflow state and evidence to resume and report the incomplete action. Do not report a completed archive or deletion solely because intent was recorded.
- Unexpected missing files or an unavailable mount/service: report drift or a technical issue, not inferred deletion or archival intent.
- Reactivation: require explicit confirmation for Archived titles, retain historical memberships, and reconcile active intent with Arr. A replacement hides the Deleted badge without clearing its durable history.

These are target contracts. Catalogue/add/reactivation/provenance operations implement their narrow memberships through shared confirmed operations; the diagnostic acquisition repair is retained. General import/active-membership maintenance, unified archive/deletion and series writers remain incomplete. Native viewing correction is a separate maintainer-only exception below. Strong typed identity matching and fresh evidence are required before changes. Series removal needs scoped evidence; one removed episode does not prove whole-series removal or unavailability.

## Accepted Commissioning Baseline

The following is the maintainer-reported completion snapshot, not live counts or an independent Curatarr audit:

| Collection | Members at completion |
| --- | --- |
| Library | 1,380 |
| Archived | 0 |
| Radarr | 658 |
| Sonarr | 0 |
| Previously acquired | 678 |
| Deleted | 0 |
| Native Owned | 1,351, preserved |

All 1,687 viewing events were reported preserved; a restore-tested backup was retained and temporary API sessions revoked. The reviewed exact Library set was approved: 678 verified managed-copy records, 673 additional records with uncertain acquisition but existing Owned membership, and 29 records with active/viewing memberships. Library approval establishes catalogue intent only. The 673 uncertain acquisition histories remain unresolved; do not backfill Previously acquired, Deleted, or Archived from that approval.

Stable collection IDs are installation-specific mappings supplied by the Ryot maintainer. Keep them in local deployment material, not as portable defaults. Confirm the configured account, names, IDs, and memberships when updating the adapter; never rely on the counts above as a health check.

Integrations were reported unchanged: Radarr push watches Custom; Sonarr push and Jellyfin sink are disabled. Do not describe viewing synchronization as enabled merely because its design exists. Before Curatarr writes are enabled, review any collection-to-Arr automation for competing writers or unintended acquisition. Do not change integrations implicitly as part of a mapping update.

## Ryot Maintainer Briefing

1. Preserve the six custom collection names and their agreed meanings. Report name/ID changes before Curatarr mappings are changed.
2. Leave native Owned and Completed system collections intact. Owned is not Curatarr acquisition history; only the optional explicit movie-membership cleanup policy may remove Owned memberships. Completed/viewing history is preserved except for the explicit admin correction below.
3. Preserve approved Library membership and all viewing history outside that explicit admin correction. Keep unresolved acquisition exceptions private and unresolved until evidence is available.
4. Do not equate Archived with Deleted, or either with missing files. Never backfill Deleted from absence alone.
5. Treat the existing integrations as unchanged until explicitly reviewed. No collection setup or documentation change authorizes downloads, Arr removal, monitoring changes, or media deletion.
6. Coordinate future writers with Curatarr: Ryot stores durable intent/history; Curatarr performs confirmed, verified operational workflows. Automatic custom-collection maintenance is not yet active; native movie Owned cleanup is an independent, off-by-default installation policy.

## Curatarr Follow-Up

The browsing UI and read-model migration are implemented and covered by adapter, configuration, API, and browser regression tests, preserving native viewing history semantics. Existing pending decisions are revalidated against fresh state as before; changed membership fingerprints supersede stale decisions without rewriting historical snapshots. On-demand read-only movie reconciliation persists findings in the shared pending-decision workflow, exposed through admin maintenance with fresh validation and per-admin skip/resume progress. The confirmed acquisition repair above is implemented; daily read-only movie monitoring is now implemented; other controlled corrections remain pending. No background job may infer intent from absence alone. System Architecture, Flow 9, owns scan/review coverage and limitations.

### Explicit Admin Viewing Correction

With maintainer diagnostics explicitly enabled, an admin may explicitly confirm Clear viewing history for one exactly identified movie. This technical repair is hidden and rejected by normal Mini App mode. This removes all its native viewing records and Completed/InProgress classification, preserving reviews/ratings, Library, custom memberships and other intent. Ryot deletion may generate progress flags; those are read back and cleared without changing pre-existing intent. Existing Watchlist/Monitoring membership with history blocks this action because native deletion side effects could alter it. The default system collections themselves remain intact. This narrow correction is not automatic maintenance, archival, acquisition, media-file deletion or batch permission. Review again reopens the same existing native event without deleting history. The development guide owns confirmation, durable intent and partial-write recovery.
