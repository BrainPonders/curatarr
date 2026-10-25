# NFO Audio Evidence Evaluation

## Status And Priority

Deferred architectural evaluation, recorded 2026-10-21. No automatic NFO enrichment or NFO-assisted acquisition is implemented.

Complete the original Curatarr roadmap first: Telegram search/add/reactivation, controlled durable-state writes, pending-decision handling, import and Jellyfin reception checks, reconciliation, exclusions, and the planned Sonarr and user/admin workflows. The phases in [System Architecture](System%20Architecture.md#implementation-phases) remain authoritative. Neither the prototype below nor an indexer proxy is a prerequisite for that roadmap.

After the original workflows are working and verified, revisit the optional stages described here. This evaluation does not authorize changing deployed household profiles or indexer settings.

## Problem And Verified Evidence

The household investigation used Radarr `6.4.4.10685` with omgwtfnzbs. Adults prefer English/original audio with French as a bonus; children prefer French with English fallback. The four deployed profiles were reported as verified and remain unchanged.

Findings supplied by the household investigation, not independently reproduced in the Curatarr workspace:

| Layer | Finding |
| --- | --- |
| Search API | Many multilingual releases advertise only `language=Multi`, without explicit audio languages. |
| Interactive search | French title markers recover French, while English can remain unrecognized. |
| NFO API | A separate `t=getnfo` request exposes detailed advertised audio tracks before downloading the movie. |
| Imported-file audit | All 24 audited MULTi files contained their metadata original language according to stored file-language information. This is sample evidence, not a general guarantee. |

For the exact release `Supergirl.2026.MULTi.VF2.HDR.DV.2160p.WEB.H265-SUPPLY`, the search API reported Multi and Radarr displayed French only. Its API-retrieved NFO explicitly advertised French (France), French (Canada), and English, each Dolby Digital Plus 5.1. The missed English classification prevented the bilingual bonus and could affect English/original-language acceptance.

This establishes an information gap for that release. It establishes advertised NFO tracks, not independently verified video contents. Parsing reliability across other NFO formats remains unproven. Extraction excluded subtitle sections and did not request an NZB download or change settings.

French-first ranking, English fallback, strict premium-remux requirements, and protected exclusions worked in the household tests. Temporarily assuming Original only for MULTi recovered English but removed French from 16 of 33 matching releases; previous settings were restored. Globally assuming Original + French would falsely classify unrelated multilingual releases and is not the proposed solution. Original language must be resolved per title, not equated with English.

## Architectural Boundary

Radarr should remain the authority for release acceptance, ranking, profiles, quality, size, exclusions, and download execution. Curatarr may supply factual evidence and coordinate explicit user choices. Copying profile configuration does not copy Radarr's evaluation engine, and a custom-format score is not the complete acquisition decision.

Evidence belongs to the exact indexer and release GUID, not merely a movie or title string. Retain provenance, retrieval time, NFO content/hash, parser version, extracted supporting text, normalized languages, regional variants, codec/channels, and ambiguity. Distinguish extraction confidence from verification of the actual file. Missing or unparseable evidence means unknown, not absent. Audio and subtitle evidence must remain separate.

Apply selective retrieval, caching, rate limits, bounded retries, and credential redaction. Candidates rejected only because of uncertain language metadata must remain eligible for evidence inspection.

## Integration Options And Limits

Source inspection of the version-specific Radarr code identified the following; these are not end-to-end integration test results:

| Option | Evidence and limitation |
| --- | --- |
| Manual release grab | The override path can accept languages but retains the cached custom-format score and proceeds to download without rerunning the decision engine in that controller. It is not an enrichment-and-rescore API. |
| Release push | `/release/push` invokes native evaluation, but the request-to-release mapper does not copy submitted languages into the release model. Posting a languages field alone is insufficient. Push also changes GUID identity and evaluates an individual pushed candidate rather than a normal search batch. |
| Indexer response enrichment | The Newznab parser reads explicit language attributes. A proxy could enrich responses before native evaluation for search and RSS routed through it. Survival through language aggregation, resulting scores, and adapter compatibility remain to be tested. |
| Upstream Arr enhancement | A native evidence hook before language/custom-format evaluation could preserve Arr's decision authority. No accepted implementation was established by this evaluation. |
| Curatarr evaluation | Reading deployed profiles and custom formats could support an advisory comparison, but matching semantics and protection rules would require implementation and validation. It must not be represented as native Radarr evaluation. |

Do not rewrite release titles as a substitute for structured evidence. Do not silently clear unrelated rejection reasons, mutate global MULTi assumptions, or assume orchestration changes independent Arr RSS decisions.

## Deferred Stage 1: Manual NFO-Assisted Selection

The preferred first optional stage addresses occasional exceptions through the Telegram frontend:

1. User opens an NFO-assisted release review for a movie.
2. Curatarr reads current Radarr candidates, profile/custom-format configuration, scores, and rejection reasons, then retrieves NFO evidence for selected candidates.
3. Show Radarr's assessment alongside advertised audio evidence and an explained NFO-informed comparison. Any local calculation must be labelled a Curatarr estimate until validated; it is not a guaranteed Radarr score or proof of acceptance.
4. User explicitly selects the exact release with a combined action: download this release and stop monitoring. Preserve quality, size, unwanted-release, premium-remux, and exclusion protections. NFO discovery must not silently authorize bypassing them.
5. Revalidate the release, profile, evidence, existing file, and queue state. Record the approval and previous monitoring state. Set the movie unmonitored and verify it before submitting the selected release through Radarr. Test this sequence in isolation before enabling it.
6. Track the accepted download and import. Verify monitoring remains off, compare imported audio metadata with the NFO, and close the workflow or raise an admin issue.

Unmonitoring is a mandatory part of this proposed action, intended to prevent subsequent routine automatic upgrades from replacing the manually selected movie. Curatarr reconciliation must preserve that deliberate state and must not silently re-enable monitoring. It is not a file lock: already queued downloads, manual searches/grabs, and external automation require separate handling.

The unmonitor and grab requests are not atomic. Persist progress and distinguish a confirmed failure from an ambiguous timeout before retrying, to avoid duplicate submissions. If submission is not confirmed after unmonitoring, report the partial outcome and offer retry or explicit restoration of monitoring. Do not silently restore monitoring or report the download as successful.

Before implementation, choose and validate how the NFO-informed comparison is calculated. Prefer native reevaluation if the enriched-input experiment succeeds; otherwise scope and label any local advisory evaluator explicitly. Do not assume adding a language bonus reproduces custom-format matching or complete acceptance rules.

## Deferred Stage 2: Automatic Native Evaluation

Explore an indexer enrichment layer or upstream Arr enhancement that supplies evidence before Radarr performs its normal evaluation. Curatarr should not need to duplicate the full scoring and rejection engine or force downloads as the automatic solution.

An enrichment layer only covers search/RSS traffic routed through it. Verify the actual omgwtfnzbs adapter path, or compatibility with a Newznab endpoint, and extend the assessment separately to Sonarr. Cache failures and unavailable NFOs must have explicit behavior; unknown evidence must not invent language tracks or silently relax household preferences.

## Validation Before Activation

Use an isolated Radarr `6.4.4.10685` with the relevant household configuration, a fixture-backed test indexer, and no usable download client for evaluation tests. No production settings need to change.

- Reproduce the exact Supergirl baseline with original metadata before testing enrichment.
- Replay the same release with only explicit NFO-derived English/French language metadata changed. Check native language classification, matched custom formats, score, approval, and rejection reasons.
- Verify size-limit, quality, premium-remux, and protected-exclusion cases still fail as expected. A better score must not imply permission to download.
- Include subtitle-only French, unavailable/malformed NFO, conflicting evidence, changed profiles, and multiple NFO layouts.
- Test RSS separately from interactive search; verify native selection among competing candidates, not just one candidate's score.
- In a separate controlled execution test, verify unmonitor-before-grab, successful import while unmonitored, queue conflicts, timeouts, restarts, and partial-failure recovery.

## Post-Import Reconciliation

Correlate indexer/GUID evidence to download history and the actual imported file. Compare advertised tracks with file-probed audio metadata; preserve both observations. File metadata is stronger evidence of stream properties, but missing or incorrect tags and unconfirmed regional variants remain possible.

Route missing required tracks and technical ambiguity to admins. Do not automatically delete the file, repeatedly acquire the same release, or re-enable monitoring. Household preference changes and otherwise unacceptable fallbacks remain explicit intent decisions.

## References

- [Radarr #10070: Multi-Language mismatch](https://github.com/Radarr/Radarr/issues/10070): directly reports indexer/NFO versus interactive-search language discrepancies; closed as not planned/support. This does not establish a technical rejection of the specific enrichment design proposed here.
- [Sonarr #5598: Improve CF Comparison Between Release and File](https://github.com/Sonarr/Sonarr/issues/5598): related metadata disagreement and scoring-loop discussion, not a dedicated pre-download NFO integration proposal.
- [Community omgwtfnzbs NFO viewer](https://greasyfork.org/en/scripts/556834-radarr-sonarr-interactive-search-nfo-viewer-only-supports-omgwtfnzbs): manual presentation and caching; no automatic scoring integration established.
- Radarr `6.4.4.10685` source: [release controller](https://github.com/Radarr/Radarr/blob/v6.4.4.10685/src/Radarr.Api.V3/Indexers/ReleaseController.cs), [release push](https://github.com/Radarr/Radarr/blob/v6.4.4.10685/src/Radarr.Api.V3/Indexers/ReleasePushController.cs), [release mapping](https://github.com/Radarr/Radarr/blob/v6.4.4.10685/src/Radarr.Api.V3/Indexers/ReleaseResource.cs), [Newznab parser](https://github.com/Radarr/Radarr/blob/v6.4.4.10685/src/NzbDrone.Core/Indexers/Newznab/NewznabRssParser.cs), [decision engine](https://github.com/Radarr/Radarr/blob/v6.4.4.10685/src/NzbDrone.Core/DecisionEngine/DownloadDecisionMaker.cs), and [custom-format calculation](https://github.com/Radarr/Radarr/blob/v6.4.4.10685/src/NzbDrone.Core/CustomFormats/CustomFormatCalculationService.cs).
