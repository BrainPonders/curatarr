"use strict";
const $ = (id) => document.getElementById(id);
const tg = window.Telegram?.WebApp;
$("definitions").append($("status-legend").content.cloneNode(true));
const state = { items: [], index: 0, searchId: null, view: "poster", detail: false, epoch: 0, detailEpoch: 0,
  maintenance: false, maintenanceBranch: "sync", viewingData: null, viewingBusy: false,
  maintenanceBusy: false, maintenanceData: null, maintenanceEpoch: 0, maintenancePosterEpoch: 0,
  showSyncDiagnostics: false, health: null, displayName: "", settings: null, settingsBusy: false };
state.acquisitionEnabled = false;
state.acquisitionContext = null;
state.acquisitionFacts = null;
const headers = { Authorization: `tma ${tg?.initData || ""}` };
let authenticated = false;

function updateDockSize() {
  const height = Math.max(0, ...[...document.querySelectorAll(".bottom-bar")].map((node) => node.getBoundingClientRect().height));
  document.documentElement.style.setProperty("--bottom-bar-height", `${height}px`);
}
function updateSafeArea() {
  for (const side of ["bottom", "left", "right"]) {
    for (const [name, source] of [["safe", tg?.safeAreaInset], ["content", tg?.contentSafeAreaInset]]) {
      const value = source?.[side];
      document.documentElement.style.setProperty(`--app-${name}-${side}`, `${Number.isFinite(value) && value >= 0 ? value : 0}px`);
    }
  }
  updateDockSize();
}
if (window.ResizeObserver) {
  const dockObserver = new ResizeObserver(updateDockSize);
  document.querySelectorAll(".bottom-bar").forEach((node) => dockObserver.observe(node));
}
window.addEventListener("resize", updateDockSize);
window.visualViewport?.addEventListener("resize", updateDockSize);
for (const event of ["safeAreaChanged", "contentSafeAreaChanged", "viewportChanged"]) tg?.onEvent?.(event, updateSafeArea);
updateSafeArea();

async function api(path, options = {}) {
  const response = await fetch(path, { ...options, headers: { ...headers, ...options.headers }, cache: "no-store", credentials: "omit" });
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Request failed. Please try again.");
  return data;
}
function message(text) { $("message").textContent = text; }
function setMode(mode) { if (mode === "Read only" && state.acquisitionEnabled) mode = "Movie library"; document.querySelector(".mode").textContent = `${state.displayName || "Shared library"} · ${mode}`; }
function setDisplayName(name) {
  state.displayName = name;
  $("scope-heading").textContent = name ? `In ${name}` : "In our library";
  document.querySelector(".household-summary").setAttribute("aria-label", `${name || "Library"} status`);
  $("feedback-heading").textContent = name ? `Awaiting ${name} feedback` : "Awaiting feedback";
  setMode(state.maintenance ? (state.maintenanceBranch === "settings" ? "Settings" : "Maintenance") : "Read only");
}
function watchText(history) {
  if (Number.isInteger(history.watch_count)) return history.watch_count === 0 ? "0 recorded times" : `${history.watch_count} ${history.watch_count === 1 ? "time" : "times"}`;
  return { watched: "Watched · count unknown", no_completed_watch: "No completion recorded", unknown: "No viewing record", unavailable: "History unavailable", needs_review: "Identity needs review" }[history.status] || "Checking...";
}
function badge(label, kind = "") {
  const node = document.createElement("span");
  node.className = "badge"; node.textContent = label; node.dataset.kind = kind;
  return node;
}
function audioBadge(label) {
  return badge(label, ["Unknown track", "Not confirmed"].includes(label) ? "" : "information");
}
function libraryKind(summary) {
  return { available: "ready", wanted: "monitored", recorded: "information" }[summary.code] || "";
}
function showDeleted(summary) {
  return summary.deleted === true && ["wanted", "archived", "recorded"].includes(summary.code);
}
function libraryBadges(summary) {
  const nodes = [badge(summary.label, libraryKind(summary))];
  if (showDeleted(summary)) {
    const deleted = badge("Deleted");
    deleted.title = "A previous copy was explicitly confirmed deleted; no current copy is confirmed present.";
    nodes.push(deleted);
  }
  return nodes;
}
function radarrState(execution) {
  if (execution?.status !== "matched") return null;
  return {
    active_downloaded_monitored: ["Downloaded", "Monitored", "monitored"],
    active_downloaded_unmonitored: ["Downloaded", "Unmonitored", "unmonitored"],
    active_missing_monitored: ["Not downloaded", "Monitored", "monitored"],
    active_missing_unmonitored: ["Not downloaded", "Unmonitored", "unmonitored"],
  }[execution.state] || null;
}
function language(code) {
  const aliases = { eng: "en", fra: "fr", fre: "fr", deu: "de", ger: "de", spa: "es", ita: "it", nld: "nl", dut: "nl", jpn: "ja", zho: "zh", chi: "zh", rus: "ru", por: "pt", ara: "ar", kor: "ko", hin: "hi" };
  if (!code || code === "und") return "Unknown track";
  const normalized = aliases[code] || code;
  try { return new Intl.DisplayNames(["en"], { type: "language" }).of(normalized) || code; }
  catch (_) { return code; }
}
function renderRatings(item, execution = null) {
  for (const source of ['imdb', 'tmdb']) {
    const value = source === 'tmdb' ? item?.tmdb_rating : execution?.status === 'matched' ? execution.imdb_rating : null;
    const valid = typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 10;
    const label = source === 'imdb' ? 'IMDb' : 'TMDB';
    const text = valid ? source === 'imdb' ? `${value.toFixed(1)}/10` : `${Number((value * 10).toFixed(1))}%` : '—';
    const node = $(source + '-rating');
    node.querySelector('.rating-value').textContent = text;
    node.title = valid ? `${label} audience rating${source === 'imdb' ? ' · supplied by Radarr' : ''}` : `${label} rating unavailable`;
    node.setAttribute('aria-label', valid ? `${label} ${text}` : `${label} rating unavailable`);
  }
}
function status(data) {
  const history = data.history || { status: "loading" };
  $("watch-status").textContent = watchText(history);
  const summary = data.library_status || { label: "Checking...", code: "unknown" };
  $("availability").textContent = summary.label;
  $("availability").dataset.kind = libraryKind(summary);
  $("deleted").hidden = !showDeleted(summary);
  $("library-note").textContent = summary.note || "";
  $("library-note").hidden = !summary.note;
  const execution = data.radarr;
  const acquisition = radarrState(execution);
  const confirmedMonitoring = Boolean(acquisition);
  $("monitoring").hidden = summary.code !== "available" || !confirmedMonitoring;
  $("monitoring").textContent = acquisition?.[1] || "";
  $("monitoring").dataset.kind = acquisition?.[2] || "";
  $("monitoring").title = "Radarr monitoring; not a statement about current quality or active downloads.";
  const versions = data.playback?.versions || [];
  const languages = [...new Set(versions.flatMap((version) => version.audio_languages.map(language)))];
  if (versions.some((version) => !version.audio_complete) && languages.length) languages.push("Unknown track");
  $("audio-languages").replaceChildren(...(languages.length ? [...new Set(languages)].map(audioBadge) : [badge("Not confirmed")]));
  $("audio-note").textContent = versions.length > 1 ? `Across ${versions.length} copies` : "";
}
function dateLabel(value) {
  return /^\d{4}-\d{2}-\d{2}$/.test(value || "") ? value.split("-").reverse().join("-") : "Not recorded";
}
function artwork(img, url, fallback) {
  img.onerror = () => { img.hidden = true; if (fallback) fallback.hidden = false; };
  img.hidden = !url;
  if (fallback) fallback.hidden = Boolean(url);
  if (url) img.src = url; else img.removeAttribute("src");
}
function backToResults() {
  $("acquisition-dialog").close(); state.acquisitionContext = null;
  if (state.returnPage) { const page = state.returnPage; state.returnPage = null; showLibraryPage(page); return; }
  state.detail = false;
  state.detailEpoch++;
  $("detail").hidden = true;
  $("results").hidden = false;
  $("search-form").hidden = false;
  tg?.BackButton?.hide();
  message("");
  renderPoster();
  updateDockSize();
  window.scrollTo({ top: state.browseScroll || 0 });
}
const resultReads = { searchId: null, entries: new Map(), images: new Map(), queue: [], active: 0 };
function ensureResultCache() {
  if (resultReads.searchId !== state.searchId) {
    resultReads.searchId = state.searchId; resultReads.entries.clear(); resultReads.images.clear(); resultReads.queue = [];
  }
}
function invalidateResultFacts() { resultReads.entries.clear(); }
function resultFacts(index) {
  ensureResultCache();
  const searchId = state.searchId;
  const old = resultReads.entries.get(index);
  if (old && (old.promise || old.data && Date.now() - old.time < 30000)) return old.promise || Promise.resolve(old.data);
  const entry = { time: 0, data: null, promise: null };
  entry.promise = api(`/api/search/${searchId}/${index}/status`).then(data => {
    entry.data = data; entry.time = Date.now(); entry.promise = null;
    if (state.searchId === searchId && resultReads.entries.get(index) === entry) updateListStatus(index, data);
    return data;
  }).catch(error => {
    const current = resultReads.entries.get(index) === entry;
    if (current) resultReads.entries.delete(index);
    if (state.searchId === searchId && current) updateListStatus(index, null, true);
    throw error;
  });
  resultReads.entries.set(index, entry);
  return entry.promise;
}
function preloadResults(indices) {
  ensureResultCache();
  for (const index of indices) {
    if (index < 0 || index >= state.items.length) continue;
    if (!resultReads.images.has(index) && state.items[index].poster_url) {
      const image = new Image(); image.src = state.items[index].poster_url; resultReads.images.set(index, image);
    }
    if (!resultReads.queue.some(job => job.index === index && job.searchId === state.searchId)) resultReads.queue.push({ index, searchId: state.searchId });
  }
  pumpResultPreloads();
}
function pumpResultPreloads() {
  while (resultReads.active < 2 && resultReads.queue.length) {
    const job = resultReads.queue.shift();
    if (job.searchId !== state.searchId) continue;
    resultReads.active++;
    resultFacts(job.index).catch(() => {}).finally(() => { resultReads.active--; pumpResultPreloads(); });
  }
}
function updateListStatus(index, data, failed = false) {
  const node = document.querySelector(`.list-item[data-index="${index}"] .list-status`);
  if (!node) return;
  const summary = data?.library_status;
  node.textContent = summary?.label || (failed ? 'Unavailable' : 'Checking…');
  node.dataset.kind = summary ? libraryKind(summary) : '';
  node.title = summary?.note || node.textContent;
}
async function renderPoster() {
  message("");
  const epoch = ++state.epoch;
  const item = state.items[state.index];
  $("position").textContent = `Result ${state.index + 1} of ${state.items.length}`;
  $("list-position").textContent = `${state.items.length} results`;
  $("movie-title").textContent = item.title;
  $("movie-year").textContent = item.year || "";
  $("description").open = false;
  renderRatings(item);
  $("overview").textContent = item.overview || "No description available.";
  $("poster").alt = `${item.title} poster`;
  artwork($("poster"), item.poster_url, $("no-poster"));
  state.resultFacts = null;
  $('result-acquisition').hidden = !state.acquisitionEnabled;
  $('result-add').disabled = true; $('result-add').textContent = 'Add movie'; $('select').classList.add('primary');
  $('result-acquisition-notice').textContent = state.acquisitionEnabled ? 'Checking movie status…' : '';
  status({ status: "loading" });
  try {
    const data = await resultFacts(state.index);
    if (epoch === state.epoch) {
      status(data); renderRatings(item, data.radarr); state.resultFacts = data;
      if (state.acquisitionEnabled) {
        const eligibility = acquisitionEligibility(data);
        $('result-add').textContent = data.history?.states?.includes('archived') ? 'Reactivate movie' : 'Add movie';
        $('result-acquisition-notice').textContent = eligibility.reason;
        if (eligibility.allowed) loadPendingAcquisition(epoch, 'results');
      }
    }
    if (epoch === state.epoch) {
      const count = state.items.length;
      preloadResults([(state.index + 1) % count, (state.index + count - 1) % count]);
    }
  } catch (error) {
    if (epoch === state.epoch) { status({ history: { status: "unavailable" }, library_status: { code: "unavailable", label: "Status unavailable" } }); message(error.message); if (state.acquisitionEnabled) $('result-acquisition-notice').textContent = 'Movie status could not be checked.'; }
  }
}
function setView(view) {
  state.view = view;
  $("carousel").hidden = view !== "poster";
  $("list").hidden = view !== "list";
  $("list-position").hidden = view !== "list";
  $("poster-mode").setAttribute("aria-pressed", String(view === "poster"));
  $("list-mode").setAttribute("aria-pressed", String(view === "list"));
  if (view === "list") preloadResults(state.items.map((_, index) => index));
  updateDockSize();
}
function renderList() {
  $("list").replaceChildren();
  state.items.forEach((item, index) => {
    const button = document.createElement("button");
    button.className = "list-item"; button.dataset.index = index;
    const library = badge("Checking…"); library.classList.add("list-status");
    const image = document.createElement("img");
    image.alt = "";
    image.loading = "lazy";
    const placeholder = document.createElement("span");
    placeholder.className = "list-art";
    artwork(image, item.poster_url, placeholder);
    const text = document.createElement("span"); text.className = "list-movie-info";
    text.textContent = item.title;
    const year = document.createElement("small");
    year.textContent = item.year || "Year unknown";
    text.append(year);
    button.append(image, placeholder, text, library);
    button.onclick = () => { state.index = index; setView("poster"); renderPoster(); };
    $("list").append(button);
    ensureResultCache(); const cached = resultReads.entries.get(index);
    if (cached?.data) updateListStatus(index, cached.data);
  });
}
function facts(target, rows) {
  $(target).replaceChildren();
  rows.forEach(([label, value]) => {
    const term = document.createElement("dt");
    term.textContent = label;
    const definition = document.createElement("dd");
    if (Array.isArray(value)) definition.replaceChildren(...value);
    else definition.textContent = value;
    $(target).append(term, definition);
  });
}
async function showDetails({ openAdd = false } = {}) {
  if (!state.detail) state.browseScroll = window.scrollY;
  const epoch = ++state.detailEpoch;
  $("acquisition-dialog").close(); state.acquisitionContext = null;
  $("acquisition-actions").hidden = true; state.acquisitionFacts = null;
  state.detail = true;
  state.epoch++;
  $("results").hidden = true;
  $("search-form").hidden = true;
  $("detail").hidden = false;
  updateDockSize();
  $("detail-title").textContent = `${state.items[state.index].title}${state.items[state.index].year ? ` (${state.items[state.index].year})` : ""}`;
  $("detail-warning").textContent = "";
  $("detail-warning").hidden = true;
  $("detail-summary").textContent = "";
  $("detail-summary").hidden = true;
  facts("ryot-facts", []);
  $("ryot-note").textContent = "";
  facts("radarr-facts", []);
  $("radarr-profile-result").hidden = true; $("radarr-profile-result").textContent = "";
  $("radarr-profile-dialog").close(); state.profileEditContext = null; state.profilePollContext = null;
  facts("playback-facts", []);
  $("refresh").disabled = true;
  tg?.BackButton?.show();
  message("Reading current movie facts...");
  window.scrollTo({ top: 0 });
  try {
    const data = await api(`/api/search/${state.searchId}/${state.index}/details`);
    if (!state.detail || epoch !== state.detailEpoch) return;
    const history = data.history;
    const historyUncertain = ["unavailable", "needs_review"].includes(history.status);
    const summary = data.library_status || { label: "Status unavailable", code: "unavailable" };
    const historyRows = [["Status", libraryBadges(summary)], ["Watched", watchText(history)]];
    const lastWatched = historyUncertain ? "Not confirmed" : dateLabel(history.last_watched_date);
    historyRows.push(["Last watched", lastWatched === "Not recorded" ? "–" : lastWatched]);
    facts("ryot-facts", historyRows);
    $("ryot-note").textContent = summary.note || "";
    const arr = data.radarr;
    const rows = [];
    if (arr.status === "matched") {
      const acquisition = radarrState(arr);
      if (acquisition) {
        const pair = document.createElement("span");
        pair.className = "status-pair";
        pair.append(badge(acquisition[0], acquisition[0] === "Downloaded" ? "downloaded" : "not_downloaded"), badge(acquisition[1], acquisition[2]));
        pair.title = "File availability and monitoring are independent facts; monitoring does not mean an upgrade is in progress.";
        rows.push(["Status", [pair]]);
      } else rows.push(["Status", "State not confirmed"]);
      const profile = arr.profile;
      const profileName = document.createElement("span");
      profileName.id = "radarr-profile";
      profileName.textContent = profile?.name || "Not confirmed";
      profileName.title = `Quality cutoff: ${profile?.cutoff_quality || "Not confirmed"}. Full profile rules remain in Radarr.`;
      const profileValues = [profileName];
      if (state.acquisitionEnabled) {
        const change = document.createElement('button'); change.id = 'radarr-change-profile'; change.textContent = 'Change profile';
        change.disabled = historyUncertain || history.status === 'unknown' || arr.profile_status !== 'matched' || history.states.includes('archived');
        change.title = change.disabled ? 'Verified catalogue history and an active native profile are required.' : 'Choose a different native download profile.';
        change.onclick = openRadarrProfile;
        const line = document.createElement('span'); line.className = 'radarr-profile-line'; line.append(profileName, change);
        profileValues.splice(0, 1, line);
      }
      if (profile?.upgrade_allowed !== true) {
        const profileSummary = document.createElement("small");
        profileSummary.className = "profile-summary";
        profileSummary.textContent = profile?.upgrade_allowed === false ? "Upgrades disabled" :
          arr.profile_status === "unavailable" ? "Profile lookup unavailable" : "Upgrade policy not confirmed";
        profileValues.push(profileSummary);
      }
      rows.push(["Profile", profileValues]);
      const goals = arr.upgrade_goals;
      rows.push(["Upgrade goals", Array.isArray(goals) && goals.length ? goals.map((goal) => {
        const name = `${goal.label}${goal.bonus ? " bonus" : ""}`;
        const node = badge(`${name} ${goal.status === "met" ? "✓" : goal.status === "unknown" ? "?" : "-"}`, goal.status === "met" ? "achieved" : "");
        const meaning = { met: "matched", unknown: "unknown", not_matched: "not matched" }[goal.status] || "unknown";
        node.title = `${name}: ${meaning}. ${goal.evidence || "Radarr classification"}. Not an active-search or final-quality guarantee.`;
        node.setAttribute("aria-label", `${name}: ${meaning}`);
        return node;
      }) : "Not confirmed"]);
      const noFile = arr.file_present === false;
      const score = document.createElement("span");
      score.id = "radarr-score";
      score.textContent = noFile ? "–" : Number.isInteger(arr.file?.score) ? arr.file.score.toLocaleString("nl-NL") : "Not confirmed";
      if (!noFile && Number.isInteger(arr.file?.score) && profile?.upgrade_allowed === true && Number.isInteger(profile.score_target)) {
        score.textContent += `/${profile.score_target.toLocaleString("nl-NL")}`;
      }
      score.title = "Radarr's current custom-format score / profile upgrade target, not a maximum, percentage, movie rating or original download-time score.";
      rows.push(["Format score", [score]]);
      rows.push(["Current quality", noFile ? "–" : arr.quality || "Not confirmed"]);
      rows.push(["File size", noFile ? "–" : Number.isFinite(arr.size_bytes) ? `${(arr.size_bytes / (1024 ** 3)).toFixed(1)} GiB` : "Not confirmed"]);
      const id = arr.identities.find((value) => value.startsWith("radarr:"));
      rows.push(["Movie ID", id ? id.split(":")[1] : "Not confirmed"]);
    } else {
      rows.push(["Status", { not_present: "Not in Radarr", unavailable: "Status unavailable", needs_review: "Identity needs review" }[arr.status] || "State not confirmed"]);
      for (const label of ["Profile", "Upgrade goals", "Format score", "Current quality", "File size", "Movie ID"]) {
        const value = arr.status === "not_present" ? "–" : "Not confirmed";
        if (label === 'Profile' && state.acquisitionEnabled) {
          const name = document.createElement('span'); name.textContent = value;
          const change = document.createElement('button'); change.id = 'radarr-change-profile'; change.textContent = 'Change profile'; change.disabled = true;
          change.title = 'An active, verified Radarr movie is required.';
          const line = document.createElement('span'); line.className = 'radarr-profile-line'; line.append(name, change);
          rows.push([label, [line]]);
        } else rows.push([label, value]);
      }
    }
    facts("radarr-facts", rows);
    const playback = data.playback;
    const playbackRows = [["Availability", [badge(data.availability.label, data.availability.code)]]];
    if (["unavailable", "needs_review"].includes(playback.status)) {
      playbackRows.push(["Library check", playback.status === "unavailable" ? "Unavailable" : "Identity needs review"]);
    }
    if (!playback.versions.length) playbackRows.push(["Audio", playback.status === "not_present" ? "–" : "Not confirmed"]);
    playback.versions.forEach((version, index) => {
      const names = version.audio_languages.map(language);
      if (!version.audio_complete) names.push("Unknown track");
      playbackRows.push([`Copy ${index + 1} audio`, (names.length ? [...new Set(names)] : ["Not confirmed"]).map(audioBadge)]);
    });
    facts("playback-facts", playbackRows);
    const overview = [];
    if (history.status === "watched") overview.push("Watched");
    if (arr.status === "matched") overview.push("Active in Radarr");
    else if (arr.status === "not_present") overview.push("Not in Radarr");
    state.acquisitionFacts = data;
    $('acquisition-result').textContent = '';
    if (state.acquisitionEnabled && openAdd && acquisitionEligibility(data).allowed) {
      const pending = await loadPendingAcquisition(epoch);
      if (!state.detail || epoch !== state.detailEpoch) return;
      if (!pending) openAcquisition('add_search', 'details');
    }
    $("detail-summary").textContent = overview.join(" · ");
    $("detail-summary").hidden = !overview.length;
    const warnings = [];
    if (history.states.includes("archived")) warnings.push(arr.status === "matched" ?
      "Archived in Ryot but active in Radarr." : "Archived. Reactivation requires confirmation.");
    if (history.status === "needs_review" || arr.status === "needs_review" || data.decision_path === "identity_blocked") warnings.push("The movie identity needs review.");
    if (history.status === "unavailable") warnings.push("Watch history could not be checked.");
    if (arr.status === "unavailable") warnings.push("Radarr status could not be checked.");
    $("detail-warning").textContent = warnings.join(" ");
    $("detail-warning").hidden = !warnings.length;
    message("");
  } catch (error) { if (state.detail && epoch === state.detailEpoch) message(error.message); }
  finally { if (epoch === state.detailEpoch) $("refresh").disabled = false; }
}
function profileViewCurrent(context) {
  return state.detail && !state.maintenance && state.searchId === context.searchId && state.index === context.index;
}
function profileEditorCurrent(context) {
  return state.profileEditContext === context && state.detailEpoch === context.epoch && profileViewCurrent(context);
}
function profileOperationText(operation) {
  return operation.status === 'completed' ? `Profile changed to ${operation.profile}.` : operation.status === 'blocked' ?
    'The profile change needs maintainer review.' : operation.status === 'cancelled' ? 'Profile change cancelled.' :
    'Your profile change is saved. Waiting for verification…';
}
async function pollRadarrProfile(operation, context, remaining = 20) {
  if (remaining === 20) state.profilePollContext = context;
  if (!profileViewCurrent(context) || state.profilePollContext !== context) return;
  const notice = $('radarr-profile-result'); notice.hidden = false; notice.textContent = profileOperationText(operation);
  const button = $('radarr-change-profile'); if (button) button.disabled = true;
  if (operation.status === 'completed') {
    invalidateResultFacts();
    if (state.profileEditContext === context) { $('radarr-profile-dialog').close(); state.profileEditContext = null; }
    const refreshEpoch = state.detailEpoch + 1;
    await showDetails();
    if (profileViewCurrent(context) && state.detailEpoch === refreshEpoch) { notice.hidden = false; notice.textContent = profileOperationText(operation); }
    return;
  }
  if (['blocked', 'cancelled'].includes(operation.status) || remaining <= 0) {
    if (button && operation.status === 'cancelled') button.disabled = false;
    return;
  }
  setTimeout(async () => {
    if (!profileViewCurrent(context) || state.profilePollContext !== context) return;
    try {
      const fresh = await api(`/api/radarr-profile/${encodeURIComponent(operation.operation_id)}`);
      if (profileViewCurrent(context)) pollRadarrProfile(fresh, context, remaining - 1);
    } catch (error) { if (profileViewCurrent(context)) { notice.hidden = false; notice.textContent = 'Saved profile change could not be checked. Reopen Change profile to recover it.'; } }
  }, 2000);
}
async function openRadarrProfile() {
  const context = { searchId: state.searchId, index: state.index, epoch: state.detailEpoch,
    choices: null, selected: null, busy: false, submitted: false };
  state.profileEditContext = context; state.profilePollContext = null;
  $('radarr-profile-movie').textContent = state.items[state.index].title;
  $('radarr-profile-current').textContent = 'Reading current profile…';
  $('radarr-profile-choices').replaceChildren(); $('radarr-profile-notice').textContent = '';
  $('radarr-profile-save').disabled = true; $('radarr-profile-save').textContent = 'Confirm profile change';
  $('radarr-profile-cancel').textContent = 'Cancel';
  $('radarr-profile-dialog').showModal(); $('radarr-profile-title').focus();
  try {
    const choices = await api(`/api/search/${context.searchId}/${context.index}/radarr-profile`);
    if (!profileEditorCurrent(context)) return;
    if (choices.busy) {
      $('radarr-profile-current').textContent = '';
      $('radarr-profile-notice').textContent = choices.message;
      if (choices.pending?.length) pollRadarrProfile(choices.pending[0], context);
      return;
    }
    context.choices = choices; context.selected = choices.current_profile_id;
    $('radarr-profile-current').textContent = `Current profile: ${choices.current_profile}`;
    for (const profile of choices.profiles) {
      const button = document.createElement('button'); button.textContent = profile.name; button.dataset.profile = String(profile.id);
      button.setAttribute('aria-pressed', String(profile.id === context.selected));
      button.onclick = () => {
        if (!profileEditorCurrent(context) || context.busy || context.submitted) return;
        context.selected = profile.id;
        for (const item of $('radarr-profile-choices').children) item.setAttribute('aria-pressed', String(item.dataset.profile === String(profile.id)));
        $('radarr-profile-save').disabled = profile.id === choices.current_profile_id;
      };
      $('radarr-profile-choices').append(button);
    }
  } catch (error) { if (profileEditorCurrent(context)) { $('radarr-profile-current').textContent = ''; $('radarr-profile-notice').textContent = error.message; } }
}
$('radarr-profile-cancel').onclick = () => { $('radarr-profile-dialog').close(); state.profileEditContext = null; };
$('radarr-profile-dialog').addEventListener('cancel', () => { state.profileEditContext = null; });
$('radarr-profile-save').onclick = async () => {
  const context = state.profileEditContext;
  if (!context?.choices || context.busy || !profileEditorCurrent(context) || context.selected === context.choices.current_profile_id) return;
  context.busy = true; context.submitted = true; $('radarr-profile-save').disabled = true;
  $('radarr-profile-cancel').textContent = 'Close'; $('radarr-profile-notice').textContent = 'Saving profile change…';
  for (const button of $('radarr-profile-choices').children) button.disabled = true;
  try {
    const operation = await api('/api/radarr-profile/confirm', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ decision_id: context.choices.decision_id, version: context.choices.version, profile_id: context.selected, confirm: true }) });
    if (state.profileEditContext === context) { $('radarr-profile-dialog').close(); state.profileEditContext = null; }
    if (profileViewCurrent(context)) pollRadarrProfile(operation, context);
  } catch (error) {
    if (profileEditorCurrent(context)) { $('radarr-profile-notice').textContent = error.message; $('radarr-profile-save').textContent = 'Check saved change'; $('radarr-profile-save').disabled = false; }
  } finally { context.busy = false; }
};
function acquisitionStatus(operation) {
  return { completed: operation.title + " · Saved successfully.", pending: "Your request is saved. Waiting to start...",
    running: "Adding the movie...", uncertain: "Your request is saved. The service response is being checked.",
    blocked: "Your request needs attention. The maintainer can review it in System check.", cancelled: "Request cancelled." }[operation.status] || "Checking your request...";
}
function acquisitionEligibility(data) {
  if (data?.radarr?.status === 'matched') return {allowed: false, reason: 'Already active in Radarr.'};
  if (data?.radarr?.status !== 'not_present' || !['unknown', 'watched', 'no_completed_watch'].includes(data?.history?.status)
      || !data.decision_path || data.decision_path === 'identity_blocked')
    return {allowed: false, reason: 'Fresh service and identity checks are required before adding this movie.'};
  return {allowed: true, reason: data.history.states.includes('archived') ? 'Archived. Reactivation requires confirmation.' :
    data.history.status === 'watched' ? 'Already watched. Existing viewing history will be preserved.' : ''};
}
function acquisitionCurrent(view, epoch) {
  return !state.maintenance && (view === 'results' ? !state.detail && state.epoch === epoch : state.detail && state.detailEpoch === epoch);
}
function acquisitionNotice(view) { if (view !== 'results') $('acquisition-actions').hidden = false; return $(view === 'results' ? 'result-acquisition-notice' : 'acquisition-result'); }
function disableAcquisition(view, disabled = true) {
  if (view === 'results') $('result-add').disabled = disabled;
  if (view === 'results') $('select').classList.toggle('primary', disabled);
}
async function pollAcquisition(identifier, epoch, remaining = 15, view = 'details') {
  if (!acquisitionCurrent(view, epoch) || !state.acquisitionEnabled) return;
  try {
    const operation = await api(`/api/acquisition/${encodeURIComponent(identifier)}`);
    if (!acquisitionCurrent(view, epoch)) return;
    acquisitionNotice(view).textContent = acquisitionStatus(operation);
    if (operation.status === "completed") invalidateResultFacts();
    if (['pending', 'running', 'uncertain'].includes(operation.status) && remaining > 0) {
      setTimeout(() => pollAcquisition(identifier, epoch, remaining - 1, view), 2000);
    }
  } catch (error) { if (acquisitionCurrent(view, epoch)) acquisitionNotice(view).textContent = error.message; }
}
async function loadPendingAcquisition(epoch, view = 'details') {
  const tmdb = state.items[state.index]?.identities?.find((id) => /^tmdb:[0-9]+$/.test(id));
  if (!tmdb) return;
  try {
    const data = await api(`/api/acquisition/pending?tmdb=${encodeURIComponent(tmdb.slice(5))}`);
    if (!acquisitionCurrent(view, epoch)) return true;
    if (data.operations.length) {
      disableAcquisition(view); pollAcquisition(data.operations[0].operation_id, epoch, 15, view); return true;
    } else if (view === 'results') {
      const eligibility = acquisitionEligibility(state.resultFacts);
      disableAcquisition(view, !eligibility.allowed); acquisitionNotice(view).textContent = eligibility.reason;
    }
    return false;
  } catch (error) { if (acquisitionCurrent(view, epoch)) { acquisitionNotice(view).textContent = error.message; disableAcquisition(view); } return true; }
}
function acquisitionSelect(node, options, selected) {
  node.replaceChildren(new Option("Select...", ""));
  for (const option of options) node.add(new Option(option.name, String(option.id)));
  node.value = selected == null ? "" : String(selected); node.disabled = false;
}
function updateCatalogueWatchFields() {
  const context = state.acquisitionContext;
  $('catalogue-watch-details').disabled = !$('catalogue-watched').checked || Boolean(context?.busy || context?.submitted);
}
function acquisitionProfiles(options, selected) {
  const group = $('acquisition-profile-buttons'); group.replaceChildren();
  $('acquisition-profile').value = options.some(option => String(option.id) === String(selected)) ? String(selected) : '';
  const update = () => {
    for (const button of group.querySelectorAll('button')) button.setAttribute('aria-pressed', String(button.dataset.profile === $('acquisition-profile').value));
  };
  for (const option of options) {
    const button = document.createElement('button'); button.type = 'button'; button.textContent = option.name;
    button.dataset.profile = String(option.id); button.disabled = Boolean(state.acquisitionContext?.busy || state.acquisitionContext?.submitted);
    button.onclick = () => {
      const context = state.acquisitionContext;
      if (!context || context.busy || context.submitted) return;
      $('acquisition-profile').value = String(option.id); update(); updateAcquisitionConfirm(context);
    };
    group.append(button);
  }
  update();
}
function updateAcquisitionConfirm(context) {
  if (!context || state.acquisitionContext !== context) return;
  const downloadReady = context.optionsLoaded && $('acquisition-profile').value && $('acquisition-root').value;
  $('acquisition-confirm').disabled = context.busy || !context.submitted && context.mode === 'add_search' && !downloadReady;
}
function setCatalogueRating(value) {
  $('catalogue-watch-rating').value = value;
  for (const button of $('catalogue-rating-buttons').querySelectorAll('button')) button.setAttribute('aria-pressed', String(button.dataset.rating === value));
}
function switchAcquisitionMode(mode, { focus = false } = {}) {
  const context = state.acquisitionContext;
  if (!context || context.busy || context.submitted) return;
  const facts = context.facts;
  context.mode = mode; context.preview = null;
  context.reactivate = mode === 'add_search' && facts?.history?.states?.includes('archived') === true;
  $('acquisition-description').textContent = mode === 'catalogue_only' ? 'Save without downloading. Optionally register a past viewing and your rating.' : 'Add to Radarr and search using the selected profile and library folder.';
  for (const [id, selected] of [['acquisition-mode-download', mode === 'add_search'], ['acquisition-mode-catalogue', mode === 'catalogue_only']]) {
    $(id).setAttribute('aria-selected', String(selected)); $(id).tabIndex = selected ? 0 : -1;
    if (selected && focus) $(id).focus();
  }
  const historyNote = [facts?.history?.status === 'watched' ? 'Already watched. Existing viewing history and ratings will be preserved.' : '',
    facts?.history?.states?.includes('archived') ? (mode === 'add_search' ? 'Archived. This action explicitly reactivates the movie.' : 'Archived status will be preserved.') : ''].filter(Boolean).join(' ');
  $('acquisition-history').textContent = historyNote; $('acquisition-history').hidden = !historyNote;
  $('acquisition-choices').hidden = mode !== 'add_search';
  $('catalogue-watch-options').hidden = mode !== 'catalogue_only';
  $('acquisition-confirm').textContent = mode === 'catalogue_only' ? 'Confirm registration' : context.reactivate ? 'Confirm reactivation and search' : 'Confirm add and search';
  $('acquisition-notice').textContent = '';
  document.querySelector('#acquisition-dialog .acquisition-body').scrollTop = 0;
  if (mode === 'add_search' && !context.optionsLoaded) {
    $('acquisition-notice').textContent = 'Loading download options…';
    if (!context.optionsPromise) {
      context.optionsPromise = api('/api/acquisition/options').then(options => {
        if (state.acquisitionContext !== context) return;
        acquisitionProfiles(options.profiles, options.default_profile ?? (options.profiles.length === 1 ? options.profiles[0].id : null));
        acquisitionSelect($('acquisition-root'), options.roots, options.default_root ?? (options.roots.length === 1 ? options.roots[0].id : null));
        context.optionsLoaded = true;
        if (context.mode === 'add_search' && !context.busy && !context.submitted) $('acquisition-notice').textContent = '';
        updateAcquisitionConfirm(context);
      }).catch(error => {
        if (state.acquisitionContext === context && context.mode === 'add_search' && !context.busy && !context.submitted) $('acquisition-notice').textContent = error.message;
      }).finally(() => { context.optionsPromise = null; });
    }
  }
  updateAcquisitionConfirm(context);
}
function openAcquisition(mode = 'add_search', view = 'details') {
  const facts = view === 'results' ? state.resultFacts : state.acquisitionFacts;
  if (view === 'results' && !acquisitionEligibility(facts).allowed) return;
  const context = { mode: null, facts, view, epoch: view === 'results' ? state.epoch : state.detailEpoch, searchId: state.searchId, index: state.index,
    preview: null, busy: false, submitted: false, optionsLoaded: false, optionsPromise: null, reactivate: false };
  state.acquisitionContext = context;
  acquisitionModesBusy(false);
  $('acquisition-title').textContent = 'Add movie';
  $('acquisition-movie').textContent = state.items[state.index].title;
  $('catalogue-watched').checked = false; updateCatalogueWatchFields();
  $('catalogue-watch-date').value = ''; setCatalogueRating('');
  for (const id of ['catalogue-watched', 'catalogue-watch-date', 'catalogue-watch-rating']) $(id).disabled = false;
  for (const button of $('catalogue-rating-buttons').querySelectorAll('button')) button.disabled = false;
  $('acquisition-profile').value = ''; $('acquisition-profile-buttons').replaceChildren();
  $('acquisition-root').replaceChildren(); $('acquisition-root').disabled = true;
  switchAcquisitionMode(mode || 'add_search');
  if (!$('acquisition-dialog').open) $('acquisition-dialog').showModal();
  $('acquisition-title').focus();
}
function acquisitionModesBusy(busy) {
  $('acquisition-mode-download').disabled = busy; $('acquisition-mode-catalogue').disabled = busy;
}
$('result-add').onclick = () => openAcquisition('add_search', 'results');
$('acquisition-mode-download').onclick = () => switchAcquisitionMode('add_search');
$('acquisition-mode-catalogue').onclick = () => switchAcquisitionMode('catalogue_only');
$('acquisition-mode-options').onkeydown = (event) => {
  if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
  event.preventDefault();
  const mode = event.key === 'Home' ? 'add_search' : event.key === 'End' ? 'catalogue_only' : state.acquisitionContext?.mode === 'add_search' ? 'catalogue_only' : 'add_search';
  switchAcquisitionMode(mode, { focus: true });
};
$('acquisition-root').onchange = () => updateAcquisitionConfirm(state.acquisitionContext);
$('acquisition-cancel').onclick = () => { state.acquisitionContext = null; $('acquisition-dialog').close(); };
$('acquisition-dialog').addEventListener('cancel', () => { state.acquisitionContext = null; });
$('catalogue-watched').onchange = updateCatalogueWatchFields;
for (const button of $('catalogue-rating-buttons').querySelectorAll('button')) button.onclick = () => {
  if (!$('catalogue-watched').checked || state.acquisitionContext?.busy || state.acquisitionContext?.submitted) return;
  setCatalogueRating($('catalogue-watch-rating').value === button.dataset.rating ? '' : button.dataset.rating);
};
$('acquisition-confirm').onclick = async () => {
  const context = state.acquisitionContext;
  if (!context?.mode || context.busy || !acquisitionCurrent(context.view, context.epoch)) return;
  context.busy = true; updateCatalogueWatchFields();
  for (const button of $('acquisition-profile-buttons').querySelectorAll('button')) button.disabled = true;
  acquisitionModesBusy(true); $('acquisition-confirm').disabled = true; $('acquisition-notice').textContent = 'Checking…';
  for (const id of ['acquisition-profile', 'acquisition-root', 'catalogue-watched', 'catalogue-watch-date']) $(id).disabled = true;
  for (const button of $('catalogue-rating-buttons').querySelectorAll('button')) button.disabled = true;
  try {
    if (!context.preview) {
      const choice = { mode: context.mode };
      if (context.mode === 'add_search') {
        const profile = Number($('acquisition-profile').value), root = Number($('acquisition-root').value);
        if (!Number.isInteger(profile) || profile <= 0 || !Number.isInteger(root) || root <= 0) throw new Error('Select a profile and library folder.');
        Object.assign(choice, { profile_id: profile, root_id: root, reactivate: context.reactivate });
      } else if ($('catalogue-watched').checked) {
        Object.assign(choice, { watched: true, watched_on: $('catalogue-watch-date').value || null,
          rating: $('catalogue-watch-rating').value ? Number($('catalogue-watch-rating').value) : null });
      }
      context.preview = await api(`/api/search/${context.searchId}/${context.index}/acquisition-preview`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(choice) });
      if (state.acquisitionContext !== context || !acquisitionCurrent(context.view, context.epoch)) return;
    }
    context.submitted = true;
    for (const id of ['acquisition-profile', 'acquisition-root', 'catalogue-watched', 'catalogue-watch-date', 'catalogue-watch-rating']) $(id).disabled = true;
    for (const button of $('catalogue-rating-buttons').querySelectorAll('button')) button.disabled = true;
    disableAcquisition(context.view); acquisitionNotice(context.view).textContent = 'Saving your request…';
    invalidateResultFacts();
    const operation = await api('/api/acquisition/confirm', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ decision_id: context.preview.decision_id, version: context.preview.version, confirm: true }) });
    if (state.acquisitionContext === context) { $('acquisition-dialog').close(); state.acquisitionContext = null; }
    if (!acquisitionCurrent(context.view, context.epoch)) return;
    disableAcquisition(context.view);
    acquisitionNotice(context.view).textContent = acquisitionStatus(operation); pollAcquisition(operation.operation_id, context.epoch, 15, context.view);
    if (context.view === 'results') acquisitionNotice(context.view).scrollIntoView({ block: 'center' });
  } catch (error) {
    if (state.acquisitionContext === context) {
      $('acquisition-notice').textContent = error.message;
      if (context.submitted) $('acquisition-confirm').textContent = 'Check saved request';
      else context.preview = null;
    }
  } finally {
    if (state.acquisitionContext === context) {
      context.busy = false;
      if (!context.submitted) {
        for (const id of ['acquisition-profile', 'acquisition-root']) $(id).disabled = !context.optionsLoaded;
        for (const id of ['catalogue-watched', 'catalogue-watch-date']) $(id).disabled = false;
        for (const button of $('catalogue-rating-buttons').querySelectorAll('button')) button.disabled = false;
      }
      updateCatalogueWatchFields();
      for (const button of $('acquisition-profile-buttons').querySelectorAll('button')) button.disabled = context.submitted;
      updateAcquisitionConfirm(context); acquisitionModesBusy(context.submitted);
    }
  }
};

$("search-form").onsubmit = async (event) => {
  event.preventDefault();
  if (!authenticated) return;
  const query = $("query").value.trim();
  if (!query) return;
  state.epoch++;
  $("results").hidden = true;
  updateDockSize();
  $("search-submit").disabled = true;
  message("Searching movies...");
  try {
    const data = await api(`/api/search?q=${encodeURIComponent(query)}`);
    state.items = data.candidates; state.searchId = data.search_id; state.index = 0;
    try { sessionStorage.setItem("curatarr.query", query); } catch (_) { /* Storage is optional. */ }
    if (!state.items.length) { message("No movies found. Try another title."); return; }
    $("results").hidden = false;
    setView("poster"); renderList(); renderPoster(); message("");
  } catch (error) { message(error.message); }
  finally { $("search-submit").disabled = false; }
};
$("previous").onclick = () => { state.index = (state.index - 1 + state.items.length) % state.items.length; renderPoster(); };
$("next").onclick = () => { state.index = (state.index + 1) % state.items.length; renderPoster(); };
$("poster-mode").onclick = () => setView("poster");
$("list-mode").onclick = () => setView("list");
$("select").onclick = showDetails;
$("back").onclick = backToResults;
$("refresh").onclick = showDetails;
$("status-info").onclick = () => $("definitions").showModal();
$("close-definitions").onclick = () => $("definitions").close();
function maintenanceTime(value, compact = false) {
  if (!value) return "Not scanned";
  const date = new Date(value.includes("T") ? value : value.replace(" ", "T") + "Z");
  return Number.isNaN(date.getTime()) ? "Not confirmed" : date.toLocaleString("en-GB", compact ?
    { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" } : undefined);
}
async function maintenancePoster(movie, epoch) {
  const identities = movie.identities.filter((value) => /^tmdb:[0-9]{1,10}$/.test(value));
  if (identities.length !== 1) return;
  try {
    const data = await api(`/api/maintenance/poster/${identities[0].split(":")[1]}`);
    if (state.maintenance && epoch === state.maintenancePosterEpoch && state.maintenanceData?.movie?.key === movie.key) {
      artwork($("maintenance-poster"), data.poster_url, $("maintenance-no-poster"));
    }
  } catch (_) { /* Optional artwork never blocks current movie facts. */ }
}
function renderMaintenance(data) {
  state.maintenanceData = data;
  $("maintenance-scan-time").hidden = false;
  state.librarySyncCount = data.finding_count; updateSyncCount();
  setMode(data.read_only ? "Read only" : "Maintenance");
  const posterEpoch = ++state.maintenancePosterEpoch;
  artwork($("maintenance-poster"), null, $("maintenance-no-poster"));
  $("maintenance-summary").textContent = `${data.finding_count} ${data.finding_count === 1 ? "finding" : "findings"}${data.finding_count === data.movie_count ? "" : ` · ${data.movie_count} ${data.movie_count === 1 ? "movie" : "movies"}`}`;
  $("maintenance-summary").title = `${data.movie_count} ${data.movie_count === 1 ? "movie requires" : "movies require"} attention. One movie can have multiple findings.`;
  $("maintenance-scan-time").textContent = `Last full scan: ${maintenanceTime(data.last_scan_at, true)}${data.scan_status === "completed" ? "" : ` · ${data.scan_status.replaceAll("_", " ")}`}`;
  $("maintenance-scan-time").title = maintenanceTime(data.last_scan_at);
  const category = $("maintenance-category");
  category.replaceChildren();
  for (const item of [{ key: "all", label: "All pending issues", count: data.movie_count - data.skipped_count },
    ...data.categories, { key: "skipped", label: "Skipped", count: data.skipped_count }]) {
    const option = document.createElement("option");
    option.value = item.key; option.textContent = `${item.label} (${item.count})`;
    category.append(option);
  }
  addViewingFilter();
  category.value = data.progress.parked ? "skipped" : data.progress.category;
  $("maintenance-notice").textContent = data.notice || "";
  $("maintenance-technical").replaceChildren(...data.technical_issues.map((issue) => {
    const item = document.createElement("li"); item.textContent = `${issue.provider}: ${issue.message}`; return item;
  }));
  const movie = data.validated ? data.movie : null;
  $("maintenance-movie").hidden = !movie;
  $("maintenance-findings").replaceChildren();
  facts("maintenance-identities", []);
  if (movie) {
    $("maintenance-title").textContent = `${movie.title}${movie.year ? ` (${movie.year})` : ""}`;
    $("maintenance-checked").textContent = `Checked: ${maintenanceTime(data.checked_at, true)}`;
    $("maintenance-checked").title = maintenanceTime(data.checked_at);
    $("maintenance-poster").alt = `${movie.title} poster`;
    maintenancePoster(movie, posterEpoch);
    facts("maintenance-identities", [["Identifiers", movie.identities.join(" · ") || "Not confirmed"]]);
    for (const finding of movie.findings) {
      const section = document.createElement("section"); section.className = "maintenance-reason";
      const heading = document.createElement("h3"); heading.textContent = finding.conclusion || finding.label;
      const explanation = document.createElement("p"); explanation.textContent = finding.detail || finding.explanation;
      const correction = document.createElement("p"); correction.className = "fact-note";
      correction.textContent = `Correction: ${finding.correction || finding.suggested_actions[0]}`;
      section.append(heading, explanation);
      if (finding.identity_comparison?.length) {
        const comparison = document.createElement("dl"); comparison.className = "identity-comparison";
        for (const record of finding.identity_comparison) {
          const provider = document.createElement("dt"); provider.textContent = record.provider;
          const title = document.createElement("dd"); title.textContent = `${record.title}${record.year ? ` (${record.year})` : ""}`;
          const ids = document.createElement("dd"); ids.textContent = record.identities.join(" · ");
          comparison.append(provider, title, ids);
        }
        section.append(comparison);
      }
      section.append(correction);
      if (finding.correctable) {
        const button = document.createElement("button"); button.className = "primary maintenance-correct";
        button.textContent = "Correct"; button.title = "Record this movie as Previously acquired in Ryot";
        button.onclick = () => {
          state.correctionTarget = { media_key: movie.key, finding_id: finding.id };
          $("correction-movie").textContent = `${movie.title}${movie.year ? ` (${movie.year})` : ""}`;
          $("correction-dialog").showModal();
        };
        section.append(button);
      } else {
        const note = document.createElement("p"); note.className = "fact-note";
        note.textContent = "This correction is not available here yet. You can skip it for later review.";
        section.append(note);
      }
      $("maintenance-findings").append(section);
    }
  }
  $("maintenance-position").textContent = movie ? `Movie ${data.position} of ${data.total}` : "No current movie";
  maintenanceBusy(false);
  updateDockSize();
}
function maintenanceBusy(busy) {
  state.maintenanceBusy = busy;
  $("maintenance").setAttribute("aria-busy", String(busy));
  for (const id of ["maintenance-category", "maintenance-refresh"]) $(id).disabled = busy;
  document.querySelectorAll(".maintenance-correct").forEach((button) => { button.disabled = busy; });
  for (const id of ["maintenance-previous", "maintenance-next", "maintenance-skip"]) {
    $(id).disabled = busy || !state.maintenanceData?.validated || !state.maintenanceData?.movie;
  }
  if (state.maintenanceData?.continue_review) $("maintenance-next").disabled = busy;
}
$("correction-cancel").onclick = () => $("correction-dialog").close();
$("correction-confirm").onclick = async () => {
  if (state.maintenanceBusy || !state.correctionTarget) return;
  const target = state.correctionTarget;
  $("correction-dialog").close();
  maintenanceBusy(true);
  state.maintenancePosterEpoch++;
  $("maintenance-movie").hidden = true;
  const epoch = state.maintenanceEpoch;
  message("Rechecking and updating Ryot...");
  try {
    const data = await api("/api/maintenance/correct", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...target, confirm: true }) });
    if (state.maintenance && epoch === state.maintenanceEpoch) { renderMaintenance(data); message(""); }
  } catch (error) {
    if (state.maintenance && epoch === state.maintenanceEpoch) {
      state.maintenanceData = null;
      $("maintenance-notice").textContent = `${error.message} Refresh before retrying; an interrupted request may have reached Ryot.`;
      message("");
    }
  } finally { if (epoch === state.maintenanceEpoch) maintenanceBusy(false); }
};
async function maintenanceNavigate(action, category = null) {
  if (state.maintenanceBusy) return;
  const epoch = state.maintenanceEpoch;
  maintenanceBusy(true);
  state.maintenancePosterEpoch++;
  $("maintenance-movie").hidden = true;
  message(action === "refresh" ? "Refreshing movie findings..." : "Checking current movie facts...");
  try {
    const data = await api("/api/maintenance/review", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action, category, expected_cursor: state.maintenanceData?.progress.cursor || null }) });
    if (state.maintenance && epoch === state.maintenanceEpoch) { renderMaintenance(data); message(""); }
  } catch (error) {
    if (state.maintenance && epoch === state.maintenanceEpoch) {
      state.maintenanceData = null; $("maintenance-notice").textContent = error.message; message("");
    }
  } finally { if (epoch === state.maintenanceEpoch) maintenanceBusy(false); }
}
async function showMaintenance(branch = state.maintenanceBranch === "viewing" ? "sync" : state.maintenanceBranch) {
  if (typeof invalidateSources === 'function') invalidateSources();
  for (const id of ["library-navigation", "suggestions-panel", "requests-panel"]) $(id).hidden = true;
  state.maintenanceEpoch++;
  state.maintenance = true;
  state.maintenanceData = null;
  maintenanceBusy(true);
  state.epoch++; state.detailEpoch++;
  $("maintenance-toggle").setAttribute("aria-pressed", String(branch !== "viewing"));
  for (const id of ["search-form", "results", "detail"]) $(id).hidden = true;
  $("maintenance").hidden = false;
  $("maintenance-movie").hidden = true;
  tg?.BackButton?.show(); updateDockSize(); window.scrollTo({ top: 0 });
  maintenanceBusy(false);
  const load = selectMaintenanceBranch(branch);
  refreshAdminCounts(branch);
  await load;
}
function setAdminCount(kind, count) {
  const badge = $(kind + "-count");
  badge.hidden = false;
  badge.textContent = Number.isInteger(count) && count >= 0 ? String(count) : "?";
  badge.title = kind === "sync" ? "Unresolved sync findings, including skipped items" : "Household-declined viewings awaiting Admin review";
  if (badge.textContent === "?") badge.title = "Count unavailable; reopen Admin to retry";
}
function renderSystemHealth(data) {
  state.health = data;
  const required = state.viewingSyncCount > 0 ? true : data.maintenance_required;
  $("system-health-value").textContent = required === true ? "Yes" :
    data.status === "checking" ? "Checking…" : required === false ? "No" : "Could not check";
  $("system-health-value").dataset.status = required === true ? "attention" : data.complete ? "healthy" : "unknown";
  $("system-health-time").textContent = `Last checked: ${data.last_checked_at ? maintenanceTime(data.last_checked_at) : "Not yet"}`;
  $("system-health-notice").textContent = data.status === "checking" ? "Checking the movie ecosystem…" :
    !data.complete ? "A complete check is unavailable. Earlier findings are retained." :
    required ? "Repository maintenance is needed." : "No issues found in the supported movie checks.";
  $("system-health-check").disabled = data.status === "checking";
  updateSyncCount();
}
async function loadSystemHealth(start = false, epoch = state.maintenanceEpoch) {
  if (start) $("system-health-check").disabled = true;
  try {
    const data = await api(start ? "/api/maintenance/check" : "/api/maintenance/health", start ? { method: "POST" } : {});
    if (epoch !== state.maintenanceEpoch) return;
    renderSystemHealth(data);
    if (data.status === "checking" && state.maintenance && state.maintenanceBranch === "sync") {
      clearTimeout(state.healthTimer);
      state.healthTimer = setTimeout(() => loadSystemHealth(false, epoch), 2000);
    }
  } catch (error) {
    if (epoch !== state.maintenanceEpoch) return;
    renderSystemHealth({ maintenance_required: null, complete: false, status: "incomplete" });
    $("system-health-notice").textContent = error.message;
    $("system-health-check").disabled = false;
  }
}
$("system-health-check").onclick = () => loadSystemHealth(true);
function updateSyncCount() {
  if (!state.showSyncDiagnostics) {
    const knownIssue = state.health?.maintenance_required === true || state.viewingSyncCount > 0;
    const healthy = state.health?.maintenance_required === false && state.viewingSyncCount === 0;
    const badge = $("sync-count"); badge.hidden = false;
    badge.textContent = knownIssue ? "!" : healthy ? "✓" : "?";
    badge.title = knownIssue ? "Maintenance needed" : healthy ? "No maintenance needed at the last complete check" : "Check incomplete or not yet run";
    return;
  }
  setAdminCount("sync", Number.isInteger(state.librarySyncCount) && Number.isInteger(state.viewingSyncCount) ?
    state.librarySyncCount + state.viewingSyncCount : null);
}
function addViewingFilter() {
  const category = $("maintenance-category");
  category.querySelector("optgroup[data-viewing]")?.remove();
  const group = document.createElement("optgroup"); group.label = "Viewing requests"; group.dataset.viewing = "true";
  const option = document.createElement("option"); option.value = "viewing_requests";
  option.textContent = `Viewing record issues (${Number.isInteger(state.viewingSyncCount) ? state.viewingSyncCount : "?"})`;
  group.append(option); category.append(group);
}
function viewingIssueCount(data) {
  const selected = $("maintenance-category").value;
  state.viewingSyncCount = data.complete === false ? (data.movies.length || null) : data.movies.length;
  $("sync-viewing-count").textContent = data.complete === false ? `${data.movies.length || "?"}${data.movies.length ? "+" : ""}` : String(data.movies.length);
  updateSyncCount();
  if (!state.showSyncDiagnostics && state.health) renderSystemHealth(state.health);
  addViewingFilter(); $("maintenance-category").value = selected;
  if (selected === "viewing_requests") {
    $("maintenance-scan-time").hidden = true;
    $("maintenance-summary").textContent = `${data.movies.length} viewing record ${data.movies.length === 1 ? "issue" : "issues"}${data.complete === false ? " · Check incomplete" : ""}`;
    $("maintenance-summary").title = "Viewing requests checked against current Ryot records";
  }
}
async function refreshAdminCounts(branch) {
  const epoch = state.maintenanceEpoch;
  const requests = [];
  if (!state.showSyncDiagnostics) requests.push(loadSystemHealth(false, epoch));
  if (state.showSyncDiagnostics && branch !== "sync") requests.push(api("/api/maintenance").then((data) => {
    if (epoch !== state.maintenanceEpoch) return;
    state.librarySyncCount = data.finding_count; updateSyncCount();
  }).catch(() => { if (epoch === state.maintenanceEpoch) { state.librarySyncCount = null; updateSyncCount(); } }));
  requests.push(api("/api/maintenance/viewing/issues").then((data) => {
    if (epoch === state.maintenanceEpoch) viewingIssueCount(data);
  }).catch(() => { if (epoch === state.maintenanceEpoch) { state.viewingSyncCount = null; updateSyncCount(); } }));
  if (branch !== "declined") requests.push(api("/api/maintenance/viewing/declined").then((data) => {
    if (epoch === state.maintenanceEpoch) setAdminCount("declined", data.complete === false ? null : data.movies.length);
  }).catch(() => { if (epoch === state.maintenanceEpoch) setAdminCount("declined", null); }));
  await Promise.allSettled(requests);
}
async function selectMaintenanceBranch(branch) {
  // Re-selecting a loading tab must retain its request epoch and busy guard.
  if (branch === state.maintenanceBranch && state.maintenanceBusy) return;
  state.maintenanceBranch = branch;
  state.maintenanceEpoch++;
  state.maintenancePosterEpoch++;
  state.viewingBusy = false;
  maintenanceBusy(false);
  $("viewing-panel").hidden = branch !== "viewing";
  $("sync-panel").hidden = branch !== "sync";
  $("settings-panel").hidden = branch !== "settings";
  $("declined-panel").hidden = branch !== "declined";
  $("declined-branch").setAttribute("aria-selected", String(branch === "declined"));
  $("declined-branch").tabIndex = branch === "declined" ? 0 : -1;
  document.querySelector(".maintenance-branches").hidden = branch === "viewing";
  $("watch-check-in").setAttribute("aria-pressed", String(branch === "viewing"));
  $("sync-branch").setAttribute("aria-selected", String(branch === "sync"));
  $("settings-branch").setAttribute("aria-selected", String(branch === "settings"));
  $("sync-branch").tabIndex = branch === "sync" ? 0 : -1;
  $("settings-branch").tabIndex = branch === "settings" ? 0 : -1;
  $("maintenance-heading").textContent = branch === "viewing" ? "Viewing inbox" : branch === "settings" ? "Settings" : branch === "declined" ? "Declined viewings" : "Library maintenance";
  setMode(branch === "viewing" ? "Viewing inbox" : branch === "settings" ? "Settings" : "Maintenance");
  message(""); updateDockSize();
  if (branch === "viewing") await viewingNavigate("open");
  else if (branch === "sync") {
    $("sync-library").hidden = false; $("sync-viewing-panel").hidden = true;
    $("sync-diagnostics").hidden = !state.showSyncDiagnostics;
    $("system-health").hidden = state.showSyncDiagnostics;
    $("sync-viewing-open").hidden = true;
    if (state.showSyncDiagnostics) await maintenanceNavigate("open");
    else await loadSystemHealth();
  }
  else if (branch === "declined") await loadDeclined();
  else await loadSettings();
}
function viewingBusy(busy) {
  state.viewingBusy = busy;
  $("viewing-panel").setAttribute("aria-busy", String(busy));
  $("viewing-refresh").disabled = busy;
  for (const action of ["previous", "next"]) $("viewing-" + action).disabled = busy || !state.viewingData?.total;
  $("viewing-reopen").disabled = busy;
  $("viewing-actions").querySelectorAll("button").forEach((button) => { button.disabled = busy; });
}
async function viewingNavigate(action) {
  if (state.viewingBusy) return;
  const epoch = state.maintenanceEpoch;
  viewingBusy(true);
  $("viewing-movie").hidden = true;
  $("viewing-empty").textContent = "";
  $("viewing-notice").textContent = "";
  message("Checking pending viewings...");
  try {
    const data = await api("/api/viewing/review", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action, expected_cursor: state.viewingData?.progress.cursor || null }) });
    if (!state.maintenance || epoch !== state.maintenanceEpoch) return;
    state.viewingData = data;
    updateWatchCount(data.pending_movie_count);
    $("viewing-summary").textContent = `${data.pending_movie_count} ${data.pending_movie_count === 1 ? "movie needs" : "movies need"} your input${data.movie_count > data.pending_movie_count ? " · Includes records needing review" : ""}`;
    $("viewing-notice").textContent = data.notice || "";
    $("viewing-empty").textContent = data.empty_message || "";
    $("viewing-position").textContent = data.total ? `Movie ${data.position} of ${data.total}` : "All caught up";
    const movie = data.validated ? data.movie : null;
    $("viewing-movie").hidden = !movie;
    if (movie) {
      $("viewing-reopen").hidden = !movie.reopen_decision_id;
      $("viewing-title").textContent = `${movie.title}${movie.year ? ` (${movie.year})` : ""}`;
      $("viewing-checked").textContent = `Checked: ${maintenanceTime(data.checked_at, true)}`;
      facts("viewing-facts", [["Watched", watchText({ watch_count: movie.watch_count, status: movie.watched ? "watched" : "no_completed_watch" })],
        ["Last watched", movie.last_watched_date ? dateLabel(movie.last_watched_date) : "-"]]);
      $("viewing-questions").replaceChildren(...movie.questions.map((question) => {
        const item = document.createElement("li"); item.textContent = question; return item;
      }));
      renderCheckIns(movie);
      const directQuestions = (movie.check_ins || []).some((q) => q.actions.length || q.disputed);
      $("feedback-heading").hidden = directQuestions;
      $("viewing-questions").hidden = directQuestions;
      artwork($("viewing-poster"), null, $("viewing-no-poster"));
      $("viewing-poster").alt = `${movie.title} poster`;
      const ids = movie.identities.filter((value) => /^tmdb:[0-9]{1,10}$/.test(value));
      if (ids.length === 1) {
        api(`/api/viewing/poster/${ids[0].split(":")[1]}`).then((poster) => {
          if (state.maintenance && epoch === state.maintenanceEpoch && state.viewingData?.movie?.key === movie.key)
            artwork($("viewing-poster"), poster.poster_url, $("viewing-no-poster"));
        }).catch(() => {});
      }
    }
    message(""); updateDockSize();
  } catch (error) {
    if (state.maintenance && epoch === state.maintenanceEpoch) {
      state.viewingData = null; $("viewing-notice").textContent = error.message; message("");
    }
  } finally { if (epoch === state.maintenanceEpoch) viewingBusy(false); }
}
function updateWatchCount(count) {
  if (!Number.isInteger(count) || count < 0) return;
  $("watch-count").hidden = count === 0;
  $("watch-count").textContent = count > 99 ? "99+" : String(count);
  const label = `Viewing inbox · ${count} ${count === 1 ? "movie needs" : "movies need"} your input`;
  $("watch-check-in").setAttribute("aria-label", label);
  $("watch-check-in").title = label;
}
async function refreshWatchCount() {
  if (!authenticated) return;
  try {
    const data = await api("/api/viewing/summary");
    $("watch-check-in").hidden = !data.available;
    updateWatchCount(data.pending_movie_count);
    if (state.maintenance && state.maintenanceBranch === "viewing" && !state.viewingBusy && state.viewingData &&
        data.queue_version !== state.viewingData.queue_version) await viewingNavigate("refresh");
  } catch (_) { $("watch-check-in").title = "Viewing inbox · Count could not be refreshed"; }
}
function renderCheckIns(movie) {
  $("viewing-actions").replaceChildren();
  for (const question of movie.check_ins || []) {
    const section = document.createElement("section"); section.className = "check-in-event";
    const heading = document.createElement("h4");
    heading.textContent = question.pending_viewing ? "Viewing confirmation saved" : question.disputed || question.notice ? "Review needed" : question.confirmed ? "Choose a rating" : "Confirm this viewing";
    section.append(heading);
    if (question.date) {
      const date = document.createElement("p"); date.className = "fact-note";
      date.textContent = `Viewing date: ${dateLabel(question.date)}`; section.append(date);
    }
    if (question.notice || question.disputed) {
      const notice = document.createElement("p"); notice.className = "fact-note";
      notice.textContent = question.notice || "Flagged for review. Ryot history has been preserved."; section.append(notice);
    }
    const rows = new Map();
    for (const action of question.actions || []) {
      if (!rows.has(action.row)) {
        const row = document.createElement("div"); row.className = "check-in-row"; rows.set(action.row, row); section.append(row);
      }
      const button = document.createElement("button"); button.textContent = /^pick/.test(action.action) ? action.action.slice(4) : action.label;
      if (/^(yes|confirm|save)/.test(action.action)) button.className = "primary";
      if (/^pick/.test(action.action)) button.setAttribute("aria-pressed", String(action.label.startsWith("[")));
      button.onclick = () => answerCheckIn(movie, question, action.action);
      rows.get(action.row).append(button);
    }
    $("viewing-actions").append(section);
  }
}
async function answerCheckIn(movie, question, action) {
  if (state.viewingBusy) return;
  const epoch = state.maintenanceEpoch;
  viewingBusy(true);
  let notice = "";
  try {
    const result = await api("/api/viewing/answer", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ decision_id: question.decision_id, media_key: movie.key, action, expected_version: question.version }) });
    updateWatchCount(result.pending_movie_count);
  } catch (error) { notice = error.message; }
  finally {
    if (epoch === state.maintenanceEpoch) {
      viewingBusy(false);
      await viewingNavigate("refresh");
      if (notice) $("viewing-notice").textContent = notice;
    }
  }
}
async function loadDeclined() {
  const sync = state.maintenanceBranch === "sync";
  const prefix = sync ? "sync-viewing" : "declined";
  const epoch = state.maintenanceEpoch;
  $(prefix + "-refresh").disabled = true;
  $(prefix + "-movies").replaceChildren();
  $(prefix + "-notice").textContent = sync ? "Checking viewing record issues..." : "Checking declined viewings...";
  try {
    const data = await api(sync ? "/api/maintenance/viewing/issues" : "/api/maintenance/viewing/declined");
    if (epoch !== state.maintenanceEpoch) return;
    if (sync) viewingIssueCount(data);
    else setAdminCount("declined", data.complete === false ? null : data.movies.length);
    $(prefix + "-notice").textContent = data.complete === false ? "Some requests could not be checked. Refresh to retry." : data.movies.length ? "" : sync ? "No viewing record issues." : "No declined viewings.";
    for (const movie of data.movies) {
      const section = document.createElement("section"); section.className = "check-in-event";
      const heading = document.createElement("div"); heading.className = "maintenance-movie-heading";
      const art = document.createElement("div"); art.className = "maintenance-artwork";
      const poster = document.createElement("img"); poster.alt = `${movie.title} poster`;
      const placeholder = document.createElement("div"); placeholder.textContent = "Poster unavailable";
      art.append(poster, placeholder); artwork(poster, null, placeholder);
      const info = document.createElement("div"); info.className = "maintenance-movie-info";
      const title = document.createElement("h2"); title.textContent = `${movie.title}${movie.year ? ` (${movie.year})` : ""}`;
      title.className = "viewing-correction-title"; info.append(title); heading.append(art, info); section.append(heading);
      if (movie.question?.date) {
        const date = document.createElement("p"); date.className = "fact-note";
        date.textContent = `Viewing date: ${dateLabel(movie.question.date)}`; info.append(date);
      }
      if (movie.notice) { const notice = document.createElement("p"); notice.textContent = movie.notice; section.append(notice); }
      for (const action of movie.question?.actions || []) {
        if (!state.showSyncDiagnostics && !["confirm_declined", "review_again"].includes(action.action)) continue;
        const control = document.createElement("div"); control.className = "viewing-correction";
        const button = document.createElement("button"); button.textContent = action.label;
        button.disabled = action.enabled === false;
        if (action.action === "clear_history") button.className = "danger-button";
        control.append(button);
        if (action.reason) {
          const reason = document.createElement("p"); reason.className = "fact-note"; reason.textContent = action.reason;
          reason.id = `reason-${movie.question.decision_id}-${action.action}`;
          button.setAttribute("aria-describedby", reason.id); button.title = action.reason; control.append(reason);
        }
        button.onclick = async () => {
          button.disabled = true;
          try {
            if (action.action === "clear_history") {
              const preview = await api("/api/maintenance/viewing/reset-preview", { method: "POST",
                headers: { "Content-Type": "application/json" }, body: JSON.stringify(correctionBody(movie, action.action)) });
              if (epoch !== state.maintenanceEpoch) return;
              viewingReset = { movie, preview, epoch };
              $("viewing-reset-movie").textContent = `${preview.title}${movie.year ? ` (${movie.year})` : ""}`;
              $("viewing-reset-description").textContent = preview.record_count ?
                `${preview.resume ? "Continue removing" : "Remove"} all ${preview.record_count} viewing records for ${state.displayName || "the household"}, including repeat watches, and clear the native watched/progress flags. ${preview.remaining_count} records remain.` :
                `No viewing records remain for ${state.displayName || "the household"}. Clear only the native watched/progress flags.`;
              $("viewing-reset-dialog").showModal();
            } else await submitViewingCorrection(movie, action.action, epoch);
          } catch (error) { if (epoch === state.maintenanceEpoch) $(prefix + "-notice").textContent = error.message; }
          finally { button.disabled = action.enabled === false; }
        };
        section.append(control);
      }
      $(prefix + "-movies").append(section);
      const ids = (movie.identities || []).filter((id) => /^tmdb:[0-9]{1,10}$/.test(id));
      if (ids.length === 1) api(`/api/maintenance/poster/${ids[0].split(":")[1]}`).then((data) => {
        if (epoch === state.maintenanceEpoch && section.isConnected) artwork(poster, data.poster_url, placeholder);
      }).catch(() => {});
    }
  } catch (error) {
    if (epoch === state.maintenanceEpoch) { if (sync) { state.viewingSyncCount = null; updateSyncCount(); } else setAdminCount("declined", null); $(prefix + "-notice").textContent = error.message; }
  }
  finally { if (epoch === state.maintenanceEpoch) $(prefix + "-refresh").disabled = false; }
}
let viewingReset = null;
function correctionBody(movie, action) {
  return { media_key: movie.media_key, decision_id: movie.question.decision_id,
           expected_version: movie.question.version, action };
}
async function submitViewingCorrection(movie, action, epoch, extra = {}) {
  const result = await api("/api/maintenance/viewing/confirm-declined", { method: "POST",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ...correctionBody(movie, action), ...extra }) });
  if (epoch === state.maintenanceEpoch) { await loadDeclined(); $(state.maintenanceBranch === "sync" ? "sync-viewing-notice" : "declined-notice").textContent = result.notice;
    await refreshAdminCounts(state.maintenanceBranch); }
  await refreshWatchCount();
}
$("viewing-reset-cancel").onclick = () => $("viewing-reset-dialog").close();
$("viewing-reset-dialog").onclose = () => { viewingReset = null; };
$("viewing-reset-confirm").onclick = async () => {
  const reset = viewingReset;
  if (!reset || reset.epoch !== state.maintenanceEpoch) return;
  $("viewing-reset-confirm").disabled = true;
  try {
    await submitViewingCorrection(reset.movie, "clear_history", reset.epoch, { confirm: true, preview_token: reset.preview.token });
  } catch (error) { if (reset.epoch === state.maintenanceEpoch) $(state.maintenanceBranch === "sync" ? "sync-viewing-notice" : "declined-notice").textContent = error.message; }
  finally { $("viewing-reset-confirm").disabled = false; $("viewing-reset-dialog").close(); }
};
$("watch-check-in").onclick = () => state.maintenance && state.maintenanceBranch === "viewing" ? leaveMaintenance() : showMaintenance("viewing");
$("sync-branch").onclick = () => selectMaintenanceBranch("sync");
$("settings-branch").onclick = () => selectMaintenanceBranch("settings");
$("declined-branch").onclick = () => selectMaintenanceBranch("declined");
$("declined-refresh").onclick = () => loadDeclined();
$("sync-viewing-refresh").onclick = () => loadDeclined();
$("sync-viewing-open").onclick = async () => {
  state.maintenanceEpoch++; state.maintenancePosterEpoch++;
  $("viewing-reset-dialog").close(); $("sync-viewing-panel").hidden = !$("sync-viewing-panel").hidden;
  if (!$("sync-viewing-panel").hidden) { await loadDeclined(); await refreshAdminCounts("sync"); }
};
document.querySelector(".maintenance-branches").onkeydown = (event) => {
  if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
  event.preventDefault();
  const branches = ["sync", "declined", "settings"];
  const branch = event.key === "Home" ? "sync" : event.key === "End" ? "settings" :
    branches[(branches.indexOf(state.maintenanceBranch) + (event.key === "ArrowRight" ? 1 : 2)) % 3];
  $(branch + "-branch").focus();
  selectMaintenanceBranch(branch);
};
for (const action of ["previous", "next", "refresh"]) {
  $("viewing-" + action).onclick = () => viewingNavigate(action);
}
function leaveMaintenance() {
  if ($("viewing-reset-dialog").open) $("viewing-reset-dialog").close();
  setMode("Read only");
  state.maintenance = false;
  state.maintenanceEpoch++;
  state.maintenancePosterEpoch++;
  maintenanceBusy(false);
  $("maintenance").hidden = true;
  $("maintenance-toggle").setAttribute("aria-pressed", "false");
  $("watch-check-in").setAttribute("aria-pressed", "false");
  $("library-navigation").hidden = !state.sourcesEnabled;
  if (!state.detail && state.libraryPage && state.libraryPage !== "search") { showLibraryPage(state.libraryPage); return; }
  refreshWatchCount();
  $("search-form").hidden = state.detail;
  $("results").hidden = state.detail || !state.items.length;
  $("detail").hidden = !state.detail;
  if (state.detail) showDetails(); else { tg?.BackButton?.hide(); if (state.items.length) renderPoster(); }
  message(""); updateDockSize(); window.scrollTo({ top: 0 });
}
function settingsBusy(busy) {
  state.settingsBusy = busy;
  for (const id of ["installation-name", "name-save", "owned-cleanup", "cleanup-answered", "cleanup-unanswered"]) $(id).disabled = busy || !state.settings;
}
function renderSettings(data) {
  state.settings = data;
  setDisplayName(data.display_name);
  $("installation-name").value = data.display_name;
  $("owned-cleanup").checked = data.owned_cleanup_enabled;
  $("cleanup-answered").checked = data.cleanup_answered_prompts;
  $("cleanup-unanswered").checked = data.cleanup_unanswered_prompts;
  const run = data.owned_cleanup_last_run;
  const summary = !run ? "Never run" : ({ completed: `${run.removed} movie ${run.removed === 1 ? "tag" : "tags"} removed`,
    running: "Checking movies", pending: `Awaiting verification · ${run.removed} of ${run.total} removed`,
    stopped: "Stopped", failed: "Check unavailable; no removal submitted" }[run.status] || "Check unavailable");
  $("owned-last-run").textContent = `Last run: ${run ? maintenanceTime(run.at, true) + " · " : ""}${summary}`;
  const promptRun = data.telegram_cleanup_last_run;
  $("prompt-last-run").textContent = !promptRun ? "Last run: Never run" :
    `Last run: ${maintenanceTime(promptRun.at, true)} · ${promptRun.removed} removed${promptRun.failed ? ` · ${promptRun.failed} unavailable` : ""}${promptRun.skipped ? ` · ${promptRun.skipped} skipped` : ""}`;
}
async function loadSettings() {
  const epoch = state.maintenanceEpoch;
  state.settings = null;
  settingsBusy(true); $("settings-notice").textContent = "";
  try {
    const data = await api("/api/settings");
    if (state.maintenance && epoch === state.maintenanceEpoch) renderSettings(data);
    if (state.sourcesEnabled && epoch === state.maintenanceEpoch) await loadSourcesSettings(epoch);
    if (state.directViewingEnabled && epoch === state.maintenanceEpoch) await loadDirectViewingSettings(epoch);
  } catch (error) {
    if (state.maintenance && epoch === state.maintenanceEpoch) $("settings-notice").textContent = error.message;
  } finally { if (epoch === state.maintenanceEpoch) settingsBusy(false); }
}
async function saveSettings(change) {
  if (state.settingsBusy || !state.settings) return;
  const epoch = state.maintenanceEpoch;
  settingsBusy(true); $("settings-notice").textContent = "Saving…";
  try {
    const data = await api("/api/settings", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ expected_revision: state.settings.revision, ...change }) });
    setDisplayName(data.display_name);
    if (state.maintenance && epoch === state.maintenanceEpoch) {
      renderSettings(data); $("settings-notice").textContent = "Saved.";
    }
  } catch (error) {
    if (state.maintenance && epoch === state.maintenanceEpoch) {
      $("settings-notice").textContent = error.message;
      $("owned-cleanup").checked = state.settings.owned_cleanup_enabled;
      $("cleanup-answered").checked = state.settings.cleanup_answered_prompts;
      $("cleanup-unanswered").checked = state.settings.cleanup_unanswered_prompts;
      // A fresh revision is needed after a concurrent admin edit. Do not silently retry a policy change.
      state.settings = null;
      $("settings-notice").textContent += " Reopen Settings to refresh.";
    }
  } finally { if (epoch === state.maintenanceEpoch) settingsBusy(false); }
}
$("name-form").onsubmit = (event) => { event.preventDefault(); saveSettings({ display_name: $("installation-name").value }); };
$("owned-cleanup").onchange = () => saveSettings({ owned_cleanup_enabled: $("owned-cleanup").checked });
$("cleanup-answered").onchange = () => saveSettings({ cleanup_answered_prompts: $("cleanup-answered").checked });
$("cleanup-unanswered").onchange = () => saveSettings({ cleanup_unanswered_prompts: $("cleanup-unanswered").checked });
$("prompt-info").onclick = () => {
  const expanded = $("prompt-info").getAttribute("aria-expanded") !== "true";
  $("prompt-info").setAttribute("aria-expanded", String(expanded)); $("prompt-explanation").hidden = !expanded;
};
$("viewing-reopen").onclick = async () => {
  const movie = state.viewingData?.movie;
  if (!movie?.reopen_decision_id || state.viewingBusy) return;
  const epoch = state.maintenanceEpoch;
  $("viewing-reopen").disabled = true;
  try {
    const data = await api("/api/viewing/reopen", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ decision_id: movie.reopen_decision_id, media_key: movie.key }) });
    if (epoch === state.maintenanceEpoch) { await viewingNavigate("refresh"); $("viewing-notice").textContent = data.notice; }
  } catch (error) { if (epoch === state.maintenanceEpoch) $("viewing-notice").textContent = error.message; }
  finally { $("viewing-reopen").disabled = false; }
};
$("owned-info").onclick = () => {
  const expanded = $("owned-info").getAttribute("aria-expanded") !== "true";
  $("owned-info").setAttribute("aria-expanded", String(expanded)); $("owned-explanation").hidden = !expanded;
};
$("maintenance-toggle").onclick = () => state.maintenance && state.maintenanceBranch !== "viewing" ? leaveMaintenance() : showMaintenance();
$("maintenance-previous").onclick = () => maintenanceNavigate("previous");
$("maintenance-next").onclick = () => maintenanceNavigate(state.maintenanceData?.continue_review ? "open" : "next");
$("maintenance-skip").onclick = () => maintenanceNavigate("skip");
$("maintenance-refresh").onclick = () => maintenanceNavigate("refresh");
$("maintenance-category").onchange = async () => {
  const viewing = $("maintenance-category").value === "viewing_requests";
  state.maintenanceEpoch++; state.maintenancePosterEpoch++;
  $("viewing-reset-dialog").close();
  $("sync-library").hidden = viewing; $("sync-viewing-panel").hidden = !viewing;
  if (viewing) { await loadDeclined(); await refreshAdminCounts("sync"); }
  else await maintenanceNavigate("filter", $("maintenance-category").value);
};
tg?.BackButton?.onClick(() => state.maintenance ? leaveMaintenance() : backToResults());
tg?.ready();
tg?.expand();
$("search-submit").disabled = true;
async function start() {
  try {
    const me = await api("/api/me"); authenticated = true;
    state.acquisitionEnabled = me.acquisition_available === true;
    state.sourcesEnabled = me.sources_available === true;
    state.directViewingEnabled = me.direct_viewing_available === true;
    $('direct-viewing-settings').hidden = !state.directViewingEnabled;
    if (typeof initSources === 'function') initSources();
    setDisplayName(me.display_name || "");
    state.showSyncDiagnostics = me.show_sync_diagnostics === true;
    $("maintenance-toggle-control").hidden = !(me.is_admin && me.maintenance_available);
    // Restore the draft before allowing input; a late counter reply must not erase a new search.
    try { $("query").value = sessionStorage.getItem("curatarr.query") || ""; } catch (_) { /* Storage is optional. */ }
    message("");
    $("search-submit").disabled = false;
    await refreshWatchCount();
  } catch (error) { message(error.message); }
}
start();
setInterval(() => { if (!document.hidden) refreshWatchCount(); }, 15000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshWatchCount(); });

let directViewingPolicy = null;
function renderDirectViewing(data) {
  directViewingPolicy = data.policy;
  $('direct-viewing-enabled').checked = data.policy.enabled;
  $('viewing-minimum').value = data.policy.minimum;
  $('viewing-completed').value = data.policy.completed;
  $('viewing-accounts').replaceChildren();
  for (const user of data.setup.users) {
    const label = document.createElement('label'), input = document.createElement('input');
    input.type = 'checkbox'; input.value = user.id; input.checked = data.policy.accounts.includes(user.id);
    label.append(input, document.createTextNode(user.name + (user.administrator ? ' (admin)' : '')));
    $('viewing-accounts').append(label);
  }
}
async function loadDirectViewingSettings(epoch) {
  directViewingPolicy = null;
  $('direct-viewing-form').querySelectorAll('input, button').forEach(el => el.disabled = true);
  $('direct-viewing-notice').textContent = 'Loading playback accounts…';
  try {
    const data = await api('/api/settings/viewing');
    if (epoch !== state.maintenanceEpoch) return;
    renderDirectViewing(data); $('direct-viewing-notice').textContent = '';
    $('direct-viewing-form').querySelectorAll('input, button').forEach(el => el.disabled = false);
  } catch (error) { if (epoch === state.maintenanceEpoch) $('direct-viewing-notice').textContent = error.message; }
}
$('direct-viewing-form').onsubmit = async event => {
  event.preventDefault(); if (!directViewingPolicy) return;
  const epoch = state.maintenanceEpoch;
  const body = {revision: directViewingPolicy.revision, enabled: $('direct-viewing-enabled').checked,
    minimum: Number($('viewing-minimum').value), completed: Number($('viewing-completed').value),
    accounts: [...$('viewing-accounts').querySelectorAll('input:checked')].map(el => el.value)};
  directViewingPolicy = null;
  $('direct-viewing-form').querySelectorAll('input, button').forEach(el => el.disabled = true);
  $('direct-viewing-notice').textContent = 'Saving…';
  try {
    const data = await api('/api/settings/viewing', {method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body)});
    if (epoch !== state.maintenanceEpoch) return;
    renderDirectViewing(data); $('direct-viewing-notice').textContent = 'Saved.';
    $('direct-viewing-form').querySelectorAll('input, button').forEach(el => el.disabled = false);
  } catch (error) {
    if (epoch === state.maintenanceEpoch) $('direct-viewing-notice').textContent = error.message + ' Reopen Settings to refresh.';
  }
};

$('viewing-info').onclick = () => {
  const expanded = $('viewing-info').getAttribute('aria-expanded') !== 'true';
  $('viewing-info').setAttribute('aria-expanded', String(expanded));
  $('viewing-explanation').hidden = !expanded;
};
