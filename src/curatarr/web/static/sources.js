/* Persistent household intake; no provider action is submitted from a Yes click. */
const sourceState = { epoch: 0, busy: false, after: '', data: null, settingsBusy: false };
function invalidateSources() { sourceState.epoch++; sourceState.busy = false; }
function initSources() { $("library-navigation").hidden = !state.sourcesEnabled; }
function sourceMessage(id, text) { $(id).textContent = text; }
function sourceButton(label, action, primary = false) {
  const button = document.createElement('button'); button.textContent = label;
  if (primary) button.className = 'primary'; button.onclick = action; return button;
}
function sourceBusy(busy) {
  sourceState.busy = busy;
  for (const id of ['suggestions-panel', 'requests-panel']) $(id).querySelectorAll('button,select').forEach(node => { node.disabled = busy; });
}
async function showLibraryPage(page) {
  sourceState.epoch++; const epoch = sourceState.epoch;
  state.libraryPage = page; state.maintenance = false; state.detail = false; state.returnPage = null;
  state.epoch++; state.detailEpoch++; state.maintenanceEpoch++;
  $("acquisition-dialog").close(); state.acquisitionContext = null;
  for (const id of ['maintenance', 'detail', 'results', 'search-form', 'suggestions-panel', 'requests-panel']) $(id).hidden = true;
  $("maintenance-toggle").setAttribute('aria-pressed', 'false'); $("watch-check-in").setAttribute('aria-pressed', 'false');
  $("library-navigation").hidden = !state.sourcesEnabled;
  for (const name of ['search', 'requests', 'suggestions']) $("library-" + name).setAttribute('aria-pressed', String(name === page));
  message(''); setMode(page === 'search' ? 'Read only' : page === 'suggestions' ? 'Suggestions' : 'Wanted requests');
  tg?.BackButton?.hide(); window.scrollTo({ top: 0 });
  if (page === 'search') {
    $("search-form").hidden = false; $("results").hidden = !state.items.length;
    if (state.items.length) { renderList(); renderPoster(); } updateDockSize(); return;
  }
  $(page + '-panel').hidden = false;
  if (page === 'suggestions') { sourceState.after = ''; await loadSuggestions(epoch); }
  else await loadWantedRequests(epoch);
  updateDockSize();
}
async function loadSuggestions(epoch = sourceState.epoch) {
  sourceBusy(true); sourceMessage('suggestions-notice', 'Loading suggestions…');
  $('suggestions-cards').replaceChildren(); $('suggestions-continue').hidden = true;
  try {
    const query = new URLSearchParams({ after: sourceState.after, view: $('suggestions-view').value });
    const data = await api('/api/suggestions?' + query);
    if (epoch !== sourceState.epoch || state.libraryPage !== 'suggestions' || state.maintenance) return;
    sourceState.next = data.continue;
    for (const item of data.items) renderSuggestion(item);
    sourceMessage('suggestions-notice', data.items.length ? `${data.items.length} movies · Notifications off` :
      $('suggestions-view').value === 'declined' ? 'No passed suggestions.' : 'No suggestions to review. Collection is controlled in Settings.');
    $('suggestions-continue').hidden = !data.continue;
  } catch (error) { if (epoch === sourceState.epoch) sourceMessage('suggestions-notice', error.message); }
  finally { if (epoch === sourceState.epoch) sourceBusy(false); }
}
function renderSuggestion(item) {
  const value = item.candidate, card = document.createElement('article'); card.className = 'proposal-card';
  const info = document.createElement('div'); info.className = 'proposal-info';
  if (value.poster_path) {
    const image = document.createElement('img'); image.src = 'https://image.tmdb.org/t/p/w185' + value.poster_path;
    image.alt = value.title + ' poster'; image.loading = 'lazy'; image.onerror = () => { image.hidden = true; }; card.append(image);
  }
  const title = document.createElement('h2'); title.textContent = value.title + (value.release_date ? ' (' + value.release_date.slice(0,4) + ')' : ''); info.append(title);
  const reason = document.createElement('p'); reason.className = 'fact-note'; reason.textContent = `TMDB ${value.rating}/10 · ${value.votes} votes`; info.append(reason);
  const link = document.createElement('a'); link.href = item.source_url; link.target = '_blank'; link.rel = 'noopener noreferrer'; link.textContent = item.source; info.append(link);
  const controls = document.createElement('div'); controls.className = 'proposal-buttons';
  if (item.decision === 'declined') controls.append(sourceButton('Reconsider', () => answerSuggestion(item, 'reopen')));
  else if (item.decision === 'selected') controls.append(sourceButton('Review choices', () => answerSuggestion(item, 'open'), true));
  else { controls.append(sourceButton('Yes', () => answerSuggestion(item, 'yes'), true), sourceButton('No', () => answerSuggestion(item, 'no'))); }
  info.append(controls); card.append(info); $('suggestions-cards').append(card);
}
async function answerSuggestion(item, action) {
  if (sourceState.busy) return;
  const epoch = sourceState.epoch; sourceBusy(true);
  try {
    const result = await api('/api/suggestions/' + item.candidate.tmdb, { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({revision:item.revision,action}) });
    if (epoch !== sourceState.epoch) return;
    if (result.search_id) openSourceMovie(result, 'suggestions'); else await loadSuggestions(epoch);
  } catch (error) { if (epoch === sourceState.epoch) sourceMessage('suggestions-notice', error.message + ' Refresh to see the current household answer.'); }
  finally { if (epoch === sourceState.epoch) sourceBusy(false); }
}
function openSourceMovie(result, returnPage) {
  sourceState.epoch++; sourceState.busy = false;
  state.returnPage = returnPage; state.items = result.candidates; state.searchId = result.search_id; state.index = 0;
  $('suggestions-panel').hidden = true; $('requests-panel').hidden = true;
  showDetails({ openAdd: true });
}
async function loadWantedRequests(epoch = sourceState.epoch) {
  sourceBusy(true); $('requests-cards').replaceChildren(); sourceMessage('requests-notice','Loading requests…');
  try {
    const data = await api('/api/source-requests');
    if (epoch !== sourceState.epoch || state.maintenance || state.libraryPage !== 'requests') return;
    for (const item of data.items) {
      const card = document.createElement('article'); card.className = 'proposal-card';
      const info = document.createElement('div'); info.className = 'proposal-info';
      const title = document.createElement('h2'); title.textContent = item.title; info.append(title);
      const note = document.createElement('p'); note.className = 'fact-note';
      note.textContent = item.status === 'waiting' ? 'Awaiting supported movie metadata. Kept for a later check.' : 'A fresh choice or maintainer review is needed.'; info.append(note);
      const controls = document.createElement('div'); controls.className = 'proposal-buttons';
      const review = sourceButton('Review choices', () => wantedAction(item, 'open'), true); review.disabled = !item.candidate; review.dataset.unavailable = String(!item.candidate);
      controls.append(review, sourceButton('Withdraw', () => wantedAction(item, 'withdraw'))); info.append(controls); card.append(info); $('requests-cards').append(card);
    }
    sourceMessage('requests-notice', data.items.length ? 'Requests stay here while waiting; no further response is required to keep them.' : 'No wanted requests need input.');
  } catch (error) { if (epoch === sourceState.epoch) sourceMessage('requests-notice',error.message); }
  finally { if (epoch === sourceState.epoch) { sourceBusy(false); $('requests-cards').querySelectorAll('[data-unavailable="true"]').forEach(node => {node.disabled = true;}); } }
}
async function wantedAction(item, action) {
  if (sourceState.busy) return;
  const epoch = sourceState.epoch; sourceBusy(true);
  try {
    const result = await api('/api/source-requests', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({source_id:item.source_id,item_key:item.item_key,revision:item.revision,action})});
    if (epoch !== sourceState.epoch) return;
    if (result.search_id) openSourceMovie(result, 'requests'); else await loadWantedRequests(epoch);
  } catch (error) { if (epoch === sourceState.epoch) sourceMessage('requests-notice',error.message); }
  finally { if (epoch === sourceState.epoch) { sourceBusy(false); $('requests-cards').querySelectorAll('[data-unavailable="true"]').forEach(node => {node.disabled=true;}); } }
}
for (const page of ['search','requests','suggestions']) $('library-'+page).onclick = () => showLibraryPage(page);
$('suggestions-refresh').onclick = () => { sourceState.after=''; loadSuggestions(); };
$('suggestions-view').onchange = () => { sourceState.after=''; loadSuggestions(); };
$('suggestions-continue').onclick = () => {sourceState.after=sourceState.next || ''; loadSuggestions(); window.scrollTo({top:0});};
$('requests-refresh').onclick = () => loadWantedRequests();

function sourceSettingsBusy(busy) {
  sourceState.settingsBusy = busy;
  $('source-settings').querySelectorAll('input,select,button').forEach(node => {node.disabled=busy || !sourceState.data;});
  if (!busy && sourceState.data) {
    $('wanted-import').disabled = !sourceState.data.wanted;
    $('discovery-check').disabled = !sourceState.data.discovery.settings.enabled;
    for(const id of ['wanted-profile','wanted-root']) $(id).disabled=sourceState.data.choices_available===false;
  }
}
function sourceHealth(data) {
  if (!data) return 'Not configured';
  const health = data.health;
  const label = {not_run:'Not collected', collecting:'Collecting', settings_changed:'Settings changed; waiting for collection', incomplete:'Could not complete; earlier data retained', completed:`${health.item_count} ${health.item_count === 1 ? 'entry' : 'entries'} collected`}[health.status];
  return 'Last run: ' + (health.last_success ? new Date(health.last_success*1000).toLocaleString('en-GB') + ' · ' : '') + label;
}
function renderSourceSettings(data) {
  const previous = sourceState.data; sourceState.data = { ...previous, ...data };
  const discovery=data.discovery.settings;
  $('discovery-enabled').checked=discovery.enabled;
  for (const name of ['months','rating','votes']) $('discovery-'+name).value=discovery[name];
  $('discovery-health').textContent=sourceHealth(data.discovery);
  if (data.genres) {
    $('discovery-genres').replaceChildren();
    for (const genre of data.genres) {
      const label=document.createElement('label'), checkbox=document.createElement('input'); checkbox.type='checkbox'; checkbox.value=genre.id;
      checkbox.checked=discovery.excluded_genres.includes(genre.id); label.append(checkbox,document.createTextNode(genre.name)); $('discovery-genres').append(label);
    }
  }
  const wanted=data.wanted, choices=sourceState.data.choices;
  if (choices) {
    acquisitionSelect($('wanted-profile'),choices.profiles,wanted?.settings.profile_id ?? choices.default_profile);
    acquisitionSelect($('wanted-root'),choices.roots,wanted?.settings.root_id ?? choices.default_root);
  }
  $('wanted-user').value=wanted?.imdb_user || ''; $('wanted-enabled').checked=wanted?.settings.enabled || false;
  $('wanted-mode').value=wanted?.settings.mode || 'add_search'; $('wanted-download-choices').hidden=$('wanted-mode').value==='catalogue_only';
  $('wanted-health').textContent=sourceHealth(wanted)+(wanted ? (wanted.baseline_complete ? ' · Historical baseline protected' : ' · First complete import will be historical only') : '');
}
async function loadSourcesSettings(epoch = state.maintenanceEpoch) {
  $('source-settings').hidden=false; sourceSettingsBusy(true); sourceMessage('source-settings-notice','');
  try {
    const data=await api('/api/settings/sources');
    if (epoch===state.maintenanceEpoch && state.maintenanceBranch==='settings') renderSourceSettings(data);
  } catch(error) { if(epoch===state.maintenanceEpoch) sourceMessage('source-settings-notice',error.message); }
  finally { if(epoch===state.maintenanceEpoch) sourceSettingsBusy(false); }
}
async function saveSource(path, values) {
  if(sourceState.settingsBusy || !sourceState.data) return;
  const epoch=state.maintenanceEpoch; sourceSettingsBusy(true); sourceMessage('source-settings-notice','Saving…');
  try {
    const data=await api('/api/settings/sources/'+path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(values)});
    if(epoch!==state.maintenanceEpoch) return;
    if(path==='imdb-import') {
      sourceState.data.wanted=data.source;
      await loadSourcesSettings(epoch);
      sourceMessage('source-settings-notice',data.historical_baseline ? `${data.count} historical entries protected. No downloads requested.` : `${data.count} entries checked. Only genuinely new entries use the saved policy.`);
    } else {renderSourceSettings(data); sourceMessage('source-settings-notice','Saved.');}
  } catch(error) {if(epoch===state.maintenanceEpoch) sourceMessage('source-settings-notice',error.message);}
  finally {if(epoch===state.maintenanceEpoch) sourceSettingsBusy(false);}
}
$('discovery-form').onsubmit=event=>{
  event.preventDefault(); if(!sourceState.data)return;
  saveSource('discovery',{expected_revision:sourceState.data.discovery.revision,enabled:$('discovery-enabled').checked,
    months:Number($('discovery-months').value),rating:Number($('discovery-rating').value),votes:Number($('discovery-votes').value),
    excluded_genres:sourceState.data.genres_available===false ? sourceState.data.discovery.settings.excluded_genres : [...$('discovery-genres').querySelectorAll('input:checked')].map(node=>Number(node.value)),max_pages:sourceState.data.discovery.settings.max_pages});
};
$('wanted-mode').onchange=()=>{$('wanted-download-choices').hidden=$('wanted-mode').value==='catalogue_only';};
$('wanted-form').onsubmit=event=>{
  event.preventDefault(); if(!sourceState.data)return;
  const mode=$('wanted-mode').value;
  saveSource('wanted',{expected_revision:$('wanted-user').value===sourceState.data.wanted?.imdb_user ? sourceState.data.wanted.revision : 0,
    imdb_user:$('wanted-user').value,enabled:$('wanted-enabled').checked,mode,
    profile_id:mode==='add_search' ? Number($('wanted-profile').value)||sourceState.data.wanted?.settings.profile_id||null : null,
    root_id:mode==='add_search' ? Number($('wanted-root').value)||sourceState.data.wanted?.settings.root_id||null : null});
};
$('wanted-import-form').onsubmit=async event=>{
  event.preventDefault(); if(!sourceState.data?.wanted || sourceState.settingsBusy)return;
  const file=$('wanted-file').files[0]; if(!file || file.size>5000000){sourceMessage('source-settings-notice','Choose a complete CSV export up to 5 MB.');return;}
  const wanted=sourceState.data.wanted, epoch=state.maintenanceEpoch;
  const content=await file.text(); if(epoch!==state.maintenanceEpoch || sourceState.data.wanted?.id!==wanted.id)return;
  saveSource('imdb-import',{source_id:wanted.id,expected_revision:wanted.revision,content,count:Number($('wanted-count').value),complete:$('wanted-complete').checked});
};
$('discovery-check').onclick=async()=>{
  if(sourceState.settingsBusy)return;
  const epoch=state.maintenanceEpoch;sourceSettingsBusy(true);
  try {await api('/api/settings/sources/check',{method:'POST'});if(epoch===state.maintenanceEpoch)await loadSourcesSettings(epoch);}
  catch(error){if(epoch===state.maintenanceEpoch)sourceMessage('source-settings-notice',error.message);}
  finally{if(epoch===state.maintenanceEpoch)sourceSettingsBusy(false);}
};
if (state.sourcesEnabled) initSources();
