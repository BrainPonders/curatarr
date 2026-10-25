"""Admin review and explicitly requested, single-movie factual corrections."""

from dataclasses import dataclass
from datetime import datetime, timezone

from curatarr.adapters import AdapterError
from curatarr.domain import MediaType, parse_identity
from curatarr.services.reconciliation import MovieReconciliationService, movie_findings


CATEGORIES = {
    "missing_unmonitored": "Missing and unmonitored",
    "archived_active": "Archived but active",
    "ryot_match_missing": "Ryot match to review",
    "library_missing": "Library membership",
    "radarr_membership_missing": "Radarr membership",
    "previously_acquired_missing": "Acquisition evidence",
    "radarr_record_missing": "Radarr record to review",
    "identity_review": "Identity review",
}

# Presentation is separate from persisted scan context so existing findings gain
# clearer wording without rewriting their evidence or decision IDs.
PRESENTATION = {
    "previously_acquired_missing": (
        "Downloaded copy not recorded in Ryot",
        "Radarr reports a downloaded movie, but Ryot's acquisition record is missing.",
        "Add Previously acquired in Ryot using Radarr's imported-file record as evidence."),
    "missing_unmonitored": (
        "Wanted movie is not being monitored",
        "Movies registered in Radarr are classified as wanted, but this movie has no downloaded file and monitoring is off. Radarr itself permits this combination.",
        "Enable monitoring if still wanted, or Archive through the controlled removal process."),
    "archived_active": (
        "Archived movie is still active in Radarr",
        "Ryot says this movie is no longer wanted, but its Radarr record remains.",
        "Review the archive: deletion of remaining managed files and removal from Radarr require confirmation."),
    "ryot_match_missing": (
        "Ryot match could not be confirmed",
        "No exact external-ID match was found. The movie may already be recorded under another identity.",
        "Confirm the correct Ryot record before changing collection membership."),
    "library_missing": (
        "Movie is missing from the catalogue",
        "A matching Ryot record exists, but it is not in Library. Radarr indicates it is wanted.",
        "Review and confirm Library membership."),
    "radarr_membership_missing": (
        "Radarr registration is not recorded in Ryot",
        "This movie is registered in Radarr, but the matching Ryot record lacks Radarr membership.",
        "Update the active Radarr membership after checking the match."),
    "radarr_record_missing": (
        "Ryot expects a Radarr record that was not found",
        "Ryot records active Radarr intent, but no exact matching Radarr record was found.",
        "Check identity and intent before restoring the record or archiving. Missing is not proof of deletion."),
    "identity_review": (
        "Movie identity needs checking",
        "Identifiers are missing, conflicting, duplicated, or only a title match is available.",
        "Confirm the correct movie match before making any corrections."),
}


def _display_finding(finding, writer_available):
    kind, context = finding["kind"], finding["context"]
    conclusion, detail, correction = PRESENTATION[kind]
    comparison = []
    if kind == "identity_review":
        reasons = context.get("identity_reasons", [])
        descriptions = {
            "missing_external_ids": "Radarr has no supported external identifier to match.",
            "duplicate_radarr_ids": "Multiple Radarr records share an external identifier.",
            "multiple_ryot_matches": "Radarr's identifiers match more than one Ryot record.",
            "conflicting_external_ids": "Some identifiers match, but another identifier conflicts between Radarr and Ryot.",
            "title_only": "Ryot has a same-title record, but no matching external identifier. These records must stay separate.",
        }
        if reasons:
            conclusion = "Same title, different catalogue record" if reasons == ["title_only"] else "Movie identifiers need checking"
            detail = " ".join(descriptions[r] for r in reasons if r in descriptions)
        correction = ("Use Radarr's IMDb/TMDB identifiers as the acquisition reference. Find or add the corresponding Ryot record; "
                      "do not replace a different title's record or viewing history. Review missing or conflicting Radarr IDs before proceeding.")
        comparison = [{"provider": "Radarr · acquisition reference", "title": context["title"],
                       "year": context["year"], "identities": context["identities"]}] if context["radarr_state"] != "not_active" else []
        comparison.extend({"provider": "Ryot · possible match", **candidate}
                          for candidate in context.get("identity_candidates", []))
        if context["radarr_state"] == "not_active":
            detail = "The Ryot record has no supported external identifier for an exact Radarr match."
            correction = "Establish the intended Radarr IMDb/TMDB identity before linking or changing the Ryot record."
    return {"id": finding["decision_id"], "kind": kind, "label": CATEGORIES[kind],
            "conclusion": conclusion, "detail": detail, "correction": correction,
            "identity_comparison": comparison,
            "correctable": kind == "previously_acquired_missing" and writer_available,
            "explanation": context["explanation"], "suggested_actions": context["suggested_actions"]}


@dataclass(frozen=True)
class MaintenanceService:
    reconciliation: MovieReconciliationService
    acquisition_writer: object | None = None

    @property
    def storage(self):
        return self.reconciliation.storage

    def summary(self, user_id: int) -> dict:
        report = self.storage.reconciliation_report()
        groups = self._groups(report)
        progress = self.storage.maintenance_progress(user_id)
        skipped = set(progress["skipped"]) & groups.keys()
        return {"movie_count": len(groups), "finding_count": report["finding_count"],
                "scan_status": report["status"], "last_scan_at": report["observed_at"],
                "technical_issues": report["technical_issues"],
                "categories": [{"key": key, "label": label,
                                "count": sum(movie_key not in skipped and any(f["kind"] == key for f in group)
                                             for movie_key, group in groups.items())}
                               for key, label in CATEGORIES.items()],
                "skipped_count": len(skipped),
                "progress": progress, "movie": None, "position": 0, "total": 0,
                "validated": False, "actions_enabled": self.acquisition_writer is not None,
                "read_only": self.acquisition_writer is None}

    @staticmethod
    def _groups(report):
        groups = {}
        for finding in report["findings"]:
            groups.setdefault(finding["media_key"], []).append(finding)
        return groups

    def _revalidate(self, key: str, findings: list[dict]) -> None:
        if any(f["kind"] in {"identity_review", "ryot_match_missing"} for f in findings):
            report = self.reconciliation.scan()
            if not report["revalidated"]:
                raise AdapterError("Complete identity validation is unavailable.")
            return
        radarr = tuple(self.reconciliation.radarr.scan_movies().value)
        identities = set()
        for finding in findings:
            context = finding["context"]
            identities.update(context.get("history_identities", []))
            identities.update(context["identities"])
        if key.startswith("ryot:"):
            identities.add(key)
        history = {}
        known = sorted(value for value in identities if value.startswith("ryot:"))
        # Exact Ryot IDs avoid rescanning the catalogue for established matches.
        lookups = known or sorted(value for value in identities if value.startswith(("tmdb:", "imdb:")))
        for value in lookups:
            item = self.reconciliation.ryot.get_item(parse_identity(value).identity).value
            if (item is None and known) or (item is not None and item.media_type is not MediaType.MOVIE):
                report = self.reconciliation.scan()
                if not report["revalidated"]:
                    raise AdapterError("Complete identity validation is unavailable.")
                return
            if item is not None:
                identifier = next((str(i) for i in item.identities if i.namespace == "ryot"), None)
                if identifier is None:
                    raise AdapterError("Fresh history is missing provider identity.")
                history[identifier] = item
        replacements = [finding for finding in movie_findings(tuple(history.values()), radarr)
                        if finding["media_key"] == key]
        if any(f["kind"] == "identity_review" for f in replacements):
            report = self.reconciliation.scan()
            if not report["revalidated"]:
                raise AdapterError("Complete identity validation is unavailable.")
            return
        self.storage.refresh_movie_findings(key, findings, replacements)

    def review(self, user_id: int, action="open", *, category=None, expected_cursor=None) -> dict:
        if action not in {"open", "next", "previous", "skip", "refresh", "filter", "skipped", "pending"}:
            raise ValueError("Unknown maintenance navigation action.")
        if category is not None and category not in {"all", "skipped", *CATEGORIES}:
            raise ValueError("Unknown maintenance category.")
        progress = self.storage.maintenance_progress(user_id)
        if action in {"next", "previous", "skip"} and expected_cursor != progress["cursor"]:
            raise ValueError("Review position changed. Reopen maintenance to continue.")
        if progress["parked"]:
            progress["category"] = "all"
        if action == "refresh":
            report = self.reconciliation.scan()
            if not report["revalidated"]:
                result = self.summary(user_id)
                result["notice"] = "The scan could not be completed. Existing findings were kept; retry when services are available."
                return result
        if action == "filter":
            progress["parked"] = category == "skipped"
            progress["category"] = "all" if progress["parked"] else category or "all"
            progress["cursor"] = None
        if action in {"skipped", "pending"}:
            progress["parked"] = action == "skipped"
            if progress["parked"]:
                progress["category"] = "all"
            progress["cursor"] = None
        def choices():
            groups = self._groups(self.storage.reconciliation_report())
            progress["skipped"] = sorted(set(progress["skipped"]) & groups.keys())
            return groups, sorted(key for key, group in groups.items()
                                  if (key in progress["skipped"]) == progress["parked"]
                                  and (progress["category"] == "all" or
                                       any(f["kind"] == progress["category"] for f in group)))

        groups, keys = choices()
        position = keys.index(progress["cursor"]) if progress["cursor"] in keys else 0
        if keys and action == "skip" and progress["cursor"] in keys:
            progress["skipped"] = sorted({*progress["skipped"], progress["cursor"]})
            groups, keys = choices()
            if progress["parked"]:
                position += 1
        elif keys and action in {"next", "previous"} and progress["cursor"] in keys:
            position = (position + (1 if action == "next" else -1)) % len(keys)
        notice = ""
        for _ in range(3):
            if not keys:
                break
            key = keys[position % len(keys)]
            progress["cursor"] = key
            try:
                self._revalidate(key, groups[key])
            except AdapterError:
                self.storage.save_maintenance_progress(user_id, progress)
                result = self.summary(user_id)
                result["notice"] = "Current movie facts could not be checked. Nothing was changed; retry Refresh findings."
                return result
            groups, keys = choices()
            if key in keys:
                self.storage.save_maintenance_progress(user_id, progress)
                result = self.summary(user_id)
                current = groups[key]
                context = current[0]["context"]
                result.update(movie={"key": key, "title": context["title"], "year": context["year"],
                                     "identities": context["identities"],
                                     "findings": [_display_finding(f, self.acquisition_writer is not None) for f in current]},
                              position=keys.index(key) + 1, total=len(keys), validated=True,
                              checked_at=datetime.now(timezone.utc).isoformat(), notice=notice)
                return result
            notice = "The previous finding changed or no longer applies. Showing the next current movie."
        progress["cursor"] = keys[position % len(keys)] if keys else None
        self.storage.save_maintenance_progress(user_id, progress)
        result = self.summary(user_id)
        result["continue_review"] = bool(keys)
        result["notice"] = ("Findings changed. Continue to check the next movie." if keys else
                            "No movies in this review queue. Skipped findings remain available separately.")
        return result

    def correct(self, user_id: int, *, media_key: str, finding_id: int) -> dict:
        if self.acquisition_writer is None:
            raise ValueError("Acquisition correction is unavailable.")
        if self.storage.maintenance_progress(user_id)["cursor"] != media_key:
            raise ValueError("Review position changed.")
        group = self._groups(self.storage.reconciliation_report()).get(media_key, [])
        target = next((f for f in group if f["decision_id"] == finding_id
                       and f["kind"] == "previously_acquired_missing"), None)
        if target is None:
            raise ValueError("Finding changed. Refresh before correcting.")
        # Writes require a complete fresh identity comparison, not only the
        # selected record lookup used for inexpensive navigation.
        if not self.reconciliation.scan()["revalidated"]:
            raise AdapterError("Complete correction validation is unavailable.")
        fresh = self._groups(self.storage.reconciliation_report()).get(media_key, [])
        if not any(f["decision_id"] == finding_id for f in fresh) or any(
                f["kind"] in {"identity_review", "archived_active", "ryot_match_missing"} for f in fresh):
            raise ValueError("Movie facts changed. Refresh before correcting.")
        identities = [parse_identity(i).identity for i in target["context"]["history_identities"]
                      if i.startswith("ryot:")]
        if len(identities) != 1:
            raise ValueError("An exact Ryot identity is required.")
        details = {"finding_id": finding_id, "ryot_identity": str(identities[0]),
                   "evidence": "radarr_imported_file", "collection": "previously_acquired"}
        self.storage.append_audit_log(actor=f"telegram:{user_id}", action="acquisition_correction_requested",
                                      media_key=media_key, details=details)
        try:
            verified = self.acquisition_writer.record_acquisition(identities[0])
        except AdapterError:
            self.storage.append_audit_log(actor=f"telegram:{user_id}", action="acquisition_correction_unconfirmed",
                                          media_key=media_key, details=details)
            result = self.summary(user_id)
            result["notice"] = "The Ryot update could not be confirmed. It may have been submitted; refresh before retrying."
            return result
        self.storage.append_audit_log(actor=f"telegram:{user_id}",
                                      action="acquisition_correction_verified" if verified else "acquisition_correction_submitted",
                                      media_key=media_key, details=details)
        result = self.review(user_id)
        result["notice"] = ("Previously acquired was added in Ryot and verified. No Radarr settings or files changed."
                            if verified else "Ryot accepted the update, but membership is not yet confirmed. Refresh to check; the finding remains until verified.")
        return result
