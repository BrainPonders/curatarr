"""Human-facing availability, independent from durable history and user intent."""


AVAILABILITY_LABELS = {
    "ready": "Ready to watch", "not_available": "Not available", "unknown": "Check unavailable",
}


def availability(playback: dict) -> dict:
    """Jellyfin readiness only; acquisition and monitoring belong to Arr."""
    if playback["status"] == "ready":
        code = "ready"
    elif playback["status"] == "not_present":
        code = "not_available"
    else:
        code = "unknown"
    return {"code": code, "label": AVAILABILITY_LABELS[code]}


def library_status(history: dict, playback: dict, execution: dict, *, deletion_confirmed=False) -> dict:
    """Ryot intent first, enriched by live facts until durable status sync exists."""
    states = history.get("states", [])
    note = ""
    if history["status"] in {"unavailable", "needs_review"}:
        code = "unavailable" if history["status"] == "unavailable" else "needs_review"
    elif "archived" in states:
        code = "archived"
        if playback["status"] == "ready" or execution["status"] == "matched":
            note = "Archived in Ryot, but still present in the library or Radarr. Removal needs review."
    elif playback["status"] == "ready":
        code = "available"
        if history["status"] == "unknown":
            note = "Available, but not yet recorded in Ryot."
    elif playback["status"] != "not_present" or execution["status"] not in {"matched", "not_present"}:
        code = "unavailable"
        note = "Current availability could not be confirmed."
    elif execution["status"] == "matched" or "radarr" in states or "sonarr" in states:
        code = "wanted"
        if execution["status"] == "not_present":
            note = "Active in Ryot, but not found in Radarr. Synchronization needs review."
        elif history["status"] == "unknown":
            note = "Present in Radarr, but not yet recorded in Ryot."
        elif execution.get("state") == "active_missing_unmonitored":
            note = "Wanted, but acquisition is disabled in Radarr."
    else:
        code = "unknown" if history["status"] == "unknown" else "recorded"
    known_history = history["status"] not in {"unknown", "unavailable", "needs_review"}
    if known_history and "library" not in states:
        note = " ".join(filter(None, (note, "Known to Ryot, but not in Library. Synchronization needs review.")))
    if known_history and deletion_confirmed is True and "deleted" not in states:
        note = " ".join(filter(None, (note, "Deletion verified locally, but not yet recorded in Ryot. Synchronization needs review.")))
    labels = {"available": "Available", "wanted": "Wanted", "archived": "Archived", "recorded": "Recorded",
              "unknown": "Unknown", "unavailable": "Status unavailable", "needs_review": "Identity needs review"}
    # A prior deletion is not current absence, and unknown reads cannot prove absence.
    deleted = (("deleted" in states or deletion_confirmed is True) and code in {"wanted", "archived", "recorded"}
               and playback["status"] == "not_present" and execution["status"] in {"matched", "not_present"}
               and (execution["status"] == "not_present" or execution.get("file_present") is False))
    return {"code": code, "label": labels[code], "deleted": deleted, "note": note, "derived": True}
