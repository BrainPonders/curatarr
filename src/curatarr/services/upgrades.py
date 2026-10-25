"""Compact profile goals from native Radarr facts, not a release decision engine."""


def upgrade_goals(profile: dict, file: dict | None, formats: list[dict] | None,
                  *, has_file: bool, original_language_id: int | None = None) -> list[dict]:
    goals = []
    file = file or {}
    quality = file.get("quality") or {}

    def add(key: str, label: str, met: bool | None, *, bonus=False, category="quality", evidence="Radarr file classification"):
        goals.append({"key": key, "label": label, "bonus": bonus, "category": category,
                      "status": "not_matched" if not has_file else "unknown" if met is None else "met" if met else "not_matched",
                      "evidence": evidence})

    qualities = profile.get("cutoff_qualities") or []
    resolutions = {q.get("resolution") for q in qualities}
    if len(resolutions) == 1 and (resolution := next(iter(resolutions))) is not None and resolution > 0:
        actual = quality.get("resolution")
        add(f"resolution:{resolution}", "4K" if resolution == 2160 else f"{resolution}p",
            actual >= resolution if type(actual) is int else None)
    if qualities and all(q.get("modifier") == "remux" for q in qualities):
        add("remux", "Remux", quality["modifier"] == "remux" if quality.get("modifier") is not None else None,
            category="remux")
    if not goals:
        below = file.get("quality_cutoff_not_met")
        add("quality_cutoff", "Quality target", not below if type(below) is bool and profile.get("cutoff_quality") else None,
            evidence=f"Radarr quality cutoff: {profile.get('cutoff_quality') or 'Not confirmed'}")

    language = profile.get("language") or {}
    language_id = language.get("id")
    if type(language_id) is int and (language_id > 0 or language_id == -2):
        expected = original_language_id if language_id == -2 else language_id
        languages = file.get("language_ids")
        known = isinstance(languages, list) and bool(languages) and all(type(x) is int and x > 0 for x in languages)
        met = (True if expected is not None and isinstance(languages, list) and expected in languages
               else False if expected is not None and known else None)
        add(f"language:{language_id}", "Original audio" if language_id == -2 else language.get("name") or "Profile audio",
            met, category="language", evidence="Radarr stored file languages; not independent track verification")

    scores = {f["id"]: f["score"] for f in profile.get("format_items", [])}
    matched = file.get("format_ids")
    for definition in formats or []:
        if scores.get(definition["id"], 0) <= 0 or any(g["key"] == definition["key"] for g in goals):
            continue
        add(definition["key"], definition["label"], definition["id"] in matched if isinstance(matched, list) else None,
            bonus=True, category=definition["category"], evidence="Radarr matched custom format in the selected profile")
    if formats is None and any(f["score"] > 0 for f in profile.get("format_items", [])):
        add("format_unknown", "Format goals", None, category="language", evidence="Custom format definitions unavailable")
    order = {"quality": 0, "remux": 1, "language": 2}
    return sorted(goals, key=lambda g: order[g["category"]])
