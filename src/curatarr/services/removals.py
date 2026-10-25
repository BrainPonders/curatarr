"""Durable Jellyfin removal confirmation, with exact-state checks and resumable writes."""
from copy import deepcopy
import hashlib
import json
import secrets
import time
from pathlib import PurePosixPath

from curatarr.adapters import AdapterError
from curatarr.domain import MediaIdentity
from curatarr.services.telegram import TelegramButton, TelegramPrompt
from curatarr.services.viewing_workflow import bot_key
from curatarr.storage import StorageError
from curatarr.storage.viewing import ViewingStore


def encoded(value):
    return json.dumps(value, sort_keys=True)


class RemovalStore:
    def __init__(self, storage):
        self.storage = storage

    def decisions(self, closed=False):
        rows = self.storage.connection.execute(
            "SELECT id FROM pending_decision WHERE decision_type='external_removal' AND audience='shared_household' "
            + ("" if closed else "AND status='open' ") + "ORDER BY id").fetchall()
        return [self.storage.get_pending_decision(r[0]) for r in rows]

    def enqueue(self, hint, source, now=None):
        now = time.time() if now is None else now
        key = hashlib.sha256((source + '\0' + hint['item_id']).encode()).hexdigest()
        connection = self.storage.connection
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT payload_json FROM state_snapshot WHERE media_key=? AND source='removal_event'", (key,)).fetchone()
            existing = json.loads(row[0]) if row else {}
            if existing.get('decision_id') and not existing.get('rearmed'):
                return existing['decision_id']
            if connection.execute("SELECT count(*) FROM pending_decision WHERE decision_type='external_removal' AND status='open'").fetchone()[0] >= 1000:
                raise StorageError("Removal queue is full; maintainer review is required.")
            context = {'workflow': 'external_removal_v1', 'hint': hint, 'source': source, 'event_key': key,
                       'received_at': now, 'not_before': now + 300, 'delivery': {'state': 'pending'},
                       'phase': 'verify', 'steps': {}, 'operation': None}
            cursor = connection.execute("INSERT INTO pending_decision(media_key,decision_type,audience,state_fingerprint,context_json,expires_at) "
                                        "VALUES(?,'external_removal','shared_household',?,?,NULL)",
                                        ('jellyfin:' + hint['item_id'], key, encoded(context)))
            identifier = cursor.lastrowid
            connection.execute("INSERT INTO state_snapshot(media_key,source,fingerprint,payload_json) VALUES(?,'removal_event','',?) "
                               "ON CONFLICT(media_key,source) DO UPDATE SET payload_json=excluded.payload_json,updated_at=CURRENT_TIMESTAMP",
                               (key, encoded({'decision_id': identifier, 'rearmed': False})))
            connection.execute("INSERT INTO audit_log(actor,action,media_key,details_json) VALUES('jellyfin:webhook','movie_removal_received',?,?)",
                               ('jellyfin:' + hint['item_id'], encoded({'decision_id': identifier, 'tmdb': hint['tmdb']})))
        return identifier

    def update(self, decision, context, action, actor='system', resolved=False):
        with self.storage.connection:
            cursor = self.storage.connection.execute("UPDATE pending_decision SET context_json=?,status=? WHERE id=? AND context_json=? AND status='open' "
                                                     "AND decision_type='external_removal'",
                                                     (encoded(context), 'resolved' if resolved else 'open', decision.decision_id, encoded(decision.context)))
            if cursor.rowcount != 1:
                raise StorageError("This removal request changed. Refresh before continuing.")
            self.storage.connection.execute("INSERT INTO audit_log(actor,action,media_key,details_json) VALUES(?,?,?,?)",
                                            (actor, action, decision.media_key, encoded({'decision_id': decision.decision_id,
                                             'phase': context['phase'], 'choice': context.get('choice')})))
        return self.storage.get_pending_decision(decision.decision_id)

    def claim(self):
        now, token = time.time(), secrets.token_hex(16)
        connection = self.storage.connection
        with connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute("SELECT payload_json FROM state_snapshot WHERE media_key='worker' AND source='removal_lease'").fetchone()
            if row and json.loads(row[0]).get('until', 0) > now:
                return None
            connection.execute("INSERT INTO state_snapshot(media_key,source,fingerprint,payload_json) VALUES('worker','removal_lease','',?) "
                               "ON CONFLICT(media_key,source) DO UPDATE SET payload_json=excluded.payload_json",
                               (encoded({'token': token, 'until': now + 1800}),))
        return token

    def release(self, token):
        with self.storage.connection:
            self.storage.connection.execute("DELETE FROM state_snapshot WHERE media_key='worker' AND source='removal_lease' "
                                            "AND json_extract(payload_json,'$.token')=?", (token,))


class MovieRemovals:
    def __init__(self, storage, jellyfin, ryot, radarr, telegram, source):
        self.storage, self.jellyfin, self.ryot, self.radarr = storage, jellyfin, ryot, radarr
        self.telegram, self.source = telegram, source
        self.store = RemovalStore(storage)
        self.lease = None

    def binding(self):
        value = ViewingStore(self.storage).binding()
        return value if value and value.get('enabled') and value.get('bot_key') == bot_key(self.telegram.token) else None

    def verify(self, context):
        if context['source'] != self.source:
            raise AdapterError('Jellyfin destination changed; maintainer review is required.')
        hint = context['hint']
        if self.jellyfin.verify(hint) != 'absent':
            return None
        movie = self.radarr.movie(hint)
        self.radarr.ensure_idle(movie)
        if 'radarr_before' in context and movie is not None:
            before = context['radarr_before']
            if before is None:
                raise AdapterError('A new Radarr entry appeared. Maintainer review is required.')
            # Downloaded replacement/import, identity changes or profile edits
            # invalidate an earlier confirmation. Never delete a newly added entry.
            fields = ('id', 'tmdbId', 'imdbId', 'path', 'movieFileId', 'hasFile', 'qualityProfileId', 'monitored')
            if any(movie.get(f) != before.get(f) for f in fields
                   if not (f == 'monitored' and context.get('choice') == 'wanted' and context.get('steps', {}).get('monitored'))):
                raise AdapterError('Radarr state changed since this request. Maintainer review is required.')
        return movie, self.ryot.snapshot(context['ryot_before']['id'], hint) if context.get('ryot_before') else self.ryot.find(hint)

    def process(self, decision):
        context = deepcopy(decision.context)
        if context['phase'] == 'question' and context['delivery']['state'] in {'sent', 'sending', 'uncertain'} and not context.get('operation'):
            return
        if context.get('retry_after', 0) > time.time() or context['not_before'] > time.time():
            return
        verified = self.verify(context)
        if verified is None:
            if context.get('operation'):
                raise AdapterError('A movie copy reappeared during retention. Maintainer review is required.')
            context['phase'] = 'copy_present'
            self.store.update(decision, context, 'removal_copy_present', resolved=True)
            return
        movie, current = verified
        if context.get('ryot_before'):
            self.ryot.preserved(context['ryot_before'], current)
        if context['phase'] == 'verify':
            if self.ryot.names['archived'] in current['collections']:
                context['phase'] = 'already_archived'
                self.store.update(decision, context, 'removal_already_archived', resolved=True)
                return
            context.update(phase='question', ryot_before=current, radarr_before=movie, needs_review=False)
            decision = self.store.update(decision, context, 'removal_verified')
        if context.get('operation'):
            return self.apply(decision, movie, current)
        if context['delivery']['state'] != 'pending':
            return
        binding = self.binding()
        if not binding:
            return
        # Persist the claim before sending. A lost Telegram response is never blindly resent.
        context['delivery'] = {'state': 'sending', 'chat_id': binding['chat_id']}
        decision = self.store.update(decision, context, 'removal_prompt_attempted')
        try:
            result = self.telegram.send_prompt(binding['chat_id'], self.prompt(decision))
            if not isinstance(result, dict) or type(result.get('message_id')) is not int:
                raise AdapterError('Removal message delivery was not confirmed.')
        except AdapterError:
            context['delivery']['state'] = 'uncertain'
            self.store.update(decision, context, 'removal_prompt_uncertain')
            raise
        context['delivery'].update(state='sent', message_id=result['message_id'])
        self.store.update(decision, context, 'removal_prompt_sent')

    def prompt(self, decision):
        context, identifier = decision.context, decision.decision_id
        hint = context['hint']
        title = hint['title'] + (f" ({hint['year']})" if hint.get('year') else '')
        if decision.status != 'open':
            outcome = {'archived': 'Archived. Viewing history and ratings were preserved.',
                       'wanted': 'Kept wanted in Radarr. Restoration will follow its existing profile.',
                       'copy_present': 'A movie copy is still available. No archive changes were made.',
                       'already_archived': 'This movie was already archived.'}.get(context['phase'], 'Request closed.')
            return TelegramPrompt('MOVIE REMOVAL\n\n' + title + '\n\n' + outcome)
        if context.get('operation'):
            return TelegramPrompt('MOVIE REMOVAL\n\n' + title + '\n\nYour choice is saved. Curatarr is verifying the remaining steps.')
        return TelegramPrompt('MOVIE REMOVAL — CONFIRMATION REQUIRED\n\n' + title +
                              '\n\nThe file is no longer available. Did you deliberately remove this movie, or should it remain wanted?'
                              '\n\nArchive preserves viewing history and ratings and removes the Radarr entry to prevent downloading it again.',
                              (TelegramButton('Archive', f'remove:{identifier}:archive'),
                               TelegramButton('Keep wanted', f'remove:{identifier}:wanted')))

    def respond(self, data, user_id, chat_id, message_id):
        pieces = data.split(':')
        if len(pieces) != 3 or pieces[0] != 'remove' or not pieces[1].isdigit() or pieces[2] not in {'archive', 'wanted', 'confirm_archive', 'confirm_wanted', 'back'}:
            raise ValueError('Invalid removal response.')
        decision = self.storage.get_pending_decision(int(pieces[1]))
        if decision is None or decision.decision_type != 'external_removal':
            raise StorageError('Removal request is unavailable.')
        context = deepcopy(decision.context)
        delivery = context['delivery']
        if delivery.get('chat_id') != chat_id or delivery.get('message_id') != message_id:
            raise StorageError('Respond from the original removal request or use /removals.')
        if decision.status != 'open' or context.get('operation'):
            return self.prompt(decision)
        action = pieces[2]
        if action == 'back':
            return self.prompt(decision)
        if action in {'archive', 'wanted'}:
            return TelegramPrompt(('Archive this movie and remove its Radarr entry?' if action == 'archive' else
                                   'Keep this movie wanted and monitored in Radarr?') + '\n\n' + context['hint']['title'] +
                                   '\n\nExisting viewing history and ratings will be preserved.',
                                   (TelegramButton('Confirm', f'remove:{decision.decision_id}:confirm_{action}'),
                                    TelegramButton('Back', f'remove:{decision.decision_id}:back')))
        # Claim the household's first confirmed choice atomically before provider writes.
        choice = action.removeprefix('confirm_')
        if choice == 'wanted' and context.get('radarr_before') is None:
            raise AdapterError('This movie has no Radarr entry. Restoration needs maintainer review.')
        context.update(choice=choice, operation={'actor': f'telegram:user:{user_id}', 'confirmed_at': time.time()}, phase='applying', retry_after=0)
        verified = self.verify(context)
        if verified is None:
            context.update(phase='copy_present', operation=None)
            return self.prompt(self.store.update(decision, context, 'removal_copy_present', resolved=True))
        self.ryot.preserved(context['ryot_before'], verified[1])
        decision = self.store.update(decision, context, 'removal_choice_confirmed', actor=context['operation']['actor'])
        lease = self.store.claim()
        if lease:
            self.lease = lease
            try:
                self.process(decision)
            finally:
                self.store.release(lease)
                self.lease = None
        return self.prompt(self.storage.get_pending_decision(decision.decision_id))

    def reopen(self, chat_id):
        binding = self.binding()
        if not binding or binding['chat_id'] != chat_id:
            raise StorageError('Use /removals in the configured follow-up chat.')
        decisions = [d for d in self.store.decisions() if d.context['phase'] == 'question' and not d.context.get('operation')]
        if not decisions:
            self.telegram.send_prompt(chat_id, TelegramPrompt('No verified movie removal requests are waiting.'))
            return
        for decision in decisions[:5]:
            context = deepcopy(decision.context)
            verified = self.verify(context)
            if verified is None:
                context['phase'] = 'copy_present'
                self.store.update(decision, context, 'removal_copy_present', resolved=True)
                continue
            self.ryot.preserved(context['ryot_before'], verified[1])
            context['delivery'] = {'state': 'sending', 'chat_id': chat_id}
            decision = self.store.update(decision, context, 'removal_prompt_reopen_attempted')
            result = self.telegram.send_prompt(chat_id, self.prompt(decision))
            if not isinstance(result, dict) or type(result.get('message_id')) is not int:
                raise AdapterError('Removal message delivery was not confirmed.')
            context['delivery'].update(state='sent', message_id=result['message_id'])
            self.store.update(decision, context, 'removal_prompt_reopened')

    def step(self, decision, name, satisfied, mutation):
        context = deepcopy(decision.context)
        if satisfied:
            if context['steps'].get(name) != 'verified':
                context['steps'][name] = 'verified'
                decision = self.store.update(decision, context, 'removal_step_verified')
            return decision, True
        if context['steps'].get(name):
            # Both an accepted async job and an uncertain HTTP mutation are checked
            # by read-back only. No repeated destructive write after a timeout.
            return decision, False
        context['steps'][name] = 'attempted'
        decision = self.store.update(decision, context, 'removal_step_attempted')
        row = self.storage.connection.execute("SELECT payload_json FROM state_snapshot WHERE media_key='worker' AND source='removal_lease'").fetchone()
        owned = json.loads(row[0]) if row else {}
        if owned.get('token') != self.lease or owned.get('until', 0) <= time.time():
            raise StorageError('Removal worker lease expired; maintainer review is required.')
        mutation()
        return decision, False

    def apply(self, decision, movie, current):
        context = decision.context
        actor, hint = context['operation']['actor'], context['hint']
        if context['choice'] == 'wanted':
            if movie is None:
                raise AdapterError('Radarr entry disappeared; restoration needs maintainer review.')
            # Our own successful monitoring change is expected during read-back.
            decision, done = self.step(decision, 'monitored', movie['monitored'], lambda: self.radarr.write('PUT', movie))
            if not done:
                return
            context = deepcopy(decision.context); context['phase'] = 'wanted'
        else:
            # The event plus verified former managed file or native acquisition
            # history must establish acquisition before recording Deleted.
            acquired = self.ryot.names['previously_acquired'] in current['collections']
            old_movie = context.get('radarr_before')
            file = (old_movie or {}).get('movieFile') or {}
            positive_file = bool(file.get('size', 0) > 0 and file.get('relativePath') and
                                 str(PurePosixPath(old_movie['path']) / file['relativePath']) == hint['path'])
            if not acquired and not positive_file:
                raise AdapterError('Prior file acquisition needs maintainer verification before archiving.')
            for key in ('previously_acquired', 'archived', 'radarr'):
                add = key != 'radarr'
                satisfied = (self.ryot.names[key] in current['collections']) == add
                decision, done = self.step(decision, 'ryot_' + key, satisfied, lambda key=key, add=add: self.ryot.change(current, key, add))
                if not done:
                    return
            decision, done = self.step(decision, 'radarr_removed', movie is None,
                                       lambda: self.radarr.write('DELETE', movie))
            if not done:
                return
            # Recheck physical absence after Arr removal; upgrades or index changes
            # must never be recorded as deliberate title-wide deletion.
            if self.jellyfin.verify(hint) != 'absent':
                raise AdapterError('A copy appeared during removal. Maintainer review is required.')
            decision, done = self.step(decision, 'ryot_deleted', self.ryot.names['deleted'] in current['collections'],
                                       lambda: self.ryot.change(current, 'deleted', True))
            if not done:
                return
            context = deepcopy(decision.context); context['phase'] = 'archived'
        # The saved intent remains open until every provider outcome and history
        # preservation has been checked after the last mutation.
        fresh = self.ryot.snapshot(context['ryot_before']['id'], hint)
        self.ryot.preserved(context['ryot_before'], fresh)
        if context['phase'] == 'archived':
            self.storage.record_confirmed_file_deletion(MediaIdentity('tmdb', hint['tmdb']), actor=actor, source='curatarr')
        finished = self.store.update(decision, context, 'removal_completed', actor=actor, resolved=True)
        delivery = context['delivery']
        if delivery.get('state') == 'sent':
            self.telegram.send_prompt(delivery['chat_id'], self.prompt(finished), message_id=delivery['message_id'], replace_missing=False)

    def tick(self):
        lease = self.store.claim()
        if not lease:
            return
        self.lease = lease
        try:
            processed = 0
            for decision in self.store.decisions():
                c = decision.context
                if c.get('retry_after', 0) > time.time() or c['not_before'] > time.time():
                    continue
                if c['phase'] == 'question' and c['delivery']['state'] != 'pending' and not c.get('operation'):
                    continue
                if processed >= 5:
                    break
                processed += 1
                try:
                    self.process(decision)
                except (AdapterError, ValueError, KeyError, TypeError):
                    latest = self.storage.get_pending_decision(decision.decision_id)
                    if latest.status == 'open':
                        context = deepcopy(latest.context)
                        context.update(retry_after=time.time() + 300, needs_review=True)
                        self.store.update(latest, context, 'removal_verification_deferred')
            # Re-arm a terminal item's event only after the exact item reappears.
            for decision in self.store.decisions(closed=True):
                if decision.status == 'open' or decision.context['source'] != self.source:
                    continue
                if self.jellyfin.present(decision.context['hint']):
                    with self.storage.connection:
                        self.storage.connection.execute("UPDATE state_snapshot SET payload_json=? WHERE media_key=? AND source='removal_event' "
                                                        "AND json_extract(payload_json,'$.decision_id')=?",
                                                        (encoded({'decision_id': decision.decision_id, 'rearmed': True}), decision.context['event_key'], decision.decision_id))
        finally:
            self.store.release(lease)
            self.lease = None
