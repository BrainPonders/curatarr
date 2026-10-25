"""Explicit past-viewing registration as recoverable catalogue operation steps."""
from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal
import re

from curatarr.adapters import AdapterError
from curatarr.integrations.ryot.completion import RyotCompletionWriter
from curatarr.integrations.ryot.viewing import SUPPORTED_REVIEW_SCALES, viewing_review_text, _rating
from curatarr.services.operations import Inspection, Outcome
from curatarr.services.settings import InstallationSettings
from curatarr.storage import StorageError
from curatarr.storage.journal import encode
from curatarr.storage.viewing import ViewingStore


class CatalogueCompletion(RyotCompletionWriter):
    @staticmethod
    def marker(operation):
        if not re.fullmatch('[a-f0-9]{32}', operation):
            raise ValueError('Invalid viewing operation.')
        return 'Curatarr catalogue · CV-' + operation


class CatalogueWatch:
    steps = ['catalogue_completion', 'catalogue_review', 'catalogue_review_link', 'catalogue_watch_ledger']

    def __init__(self, storage, catalogue, native, completion=None):
        self.storage, self.catalogue, self.native = storage, catalogue, native
        self.completion = completion or CatalogueCompletion(catalogue)

    def prepare(self, before, candidate, *, watched, watched_on, rating, user_name):
        if type(watched) is not bool or rating is not None and (type(rating) is not int or not 1 <= rating <= 5):
            raise ValueError('Choose an optional rating from 1 to 5.')
        if not watched:
            if watched_on is not None or rating is not None:
                raise ValueError('Confirm watched before supplying a date or rating.')
            return None
        finished = None
        if watched_on is not None:
            if not isinstance(watched_on, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', watched_on):
                raise ValueError('Choose a valid past viewing date, or leave it empty.')
            day = date.fromisoformat(watched_on)
            if day > datetime.now(timezone.utc).date():
                raise ValueError('The viewing date cannot be in the future.')
            finished = day.isoformat() + 'T00:00:00+00:00'
        account = self.native.account()
        if account['id'] != before['account_id'] or account['scale'] not in SUPPORTED_REVIEW_SCALES:
            raise AdapterError('The viewing account or rating scale is unavailable.')
        self._reset_guard(candidate['tmdb'], before)
        history = self.completion.history(before)
        if any(e['state'] != 'COMPLETED' for e in history.values()):
            raise ValueError('Existing partial viewing history needs maintainer review.')
        if before['movie'] and any(n.casefold().replace(' ', '') in {'watchlist', 'monitoring'} for n in before['movie']['collections']):
            raise ValueError('Existing native watch intent needs maintainer review.')
        reuse = max(history.values(), key=lambda e: (e.get('finishedOn') or '', e['id'])) if history else None
        if reuse and finished and (reuse.get('finishedOn') or '')[:10] != watched_on:
            raise ValueError('A completed watch already exists with a different or unknown date. Leave the date empty to preserve it.')
        # Register an existing fact, never infer another repeat watch from this form.
        if reuse:
            finished = reuse.get('finishedOn')
            self._pending_rating_guard(account['source'], reuse['id'])
            if rating is not None and reuse.get('reviewId'):
                reviews = [r for r in before['movie']['reviews'] if r.get('id') == reuse['reviewId']
                           and r.get('postedBy', {}).get('id') == account['id']]
                if len(reviews) != 1 or _rating(reviews[0].get('rating')) != rating * 20:
                    raise ValueError('An existing rating will be preserved. Leave rating empty to register this movie.')
        return {'source': account['source'], 'account_id': account['id'], 'scale': account['scale'],
                'finished_on': finished, 'rating': rating, 'reuse': reuse['id'] if reuse else None,
                'user_name': ' '.join(str(user_name or 'Household member').split())[:100],
                'display_name': InstallationSettings(self.storage).get()['display_name']}

    def _reset_guard(self, tmdb, snapshot):
        store = ViewingStore(self.storage)
        if store.reset_pending('tmdb:' + tmdb) or snapshot['movie'] and store.reset_pending('ryot:' + snapshot['movie']['metadata']['id']):
            raise ValueError('A viewing-history reset is pending.')

    def _pending_rating_guard(self, source, event_id):
        for row in self.storage.connection.execute("SELECT context_json FROM pending_decision WHERE status='open' AND decision_type='viewing_followup'"):
            import json
            context = json.loads(row[0])
            if context.get('source') == source and context.get('event_id') == event_id and context.get('operation'):
                raise ValueError('A rating operation is already pending for this viewing.')

    def review_text(self, operation):
        e = operation['expected']; w = e['manual_watch']
        return viewing_review_text({'title': e['candidate']['title'], 'year': e['candidate'].get('year'),
            'finished_on': w['finished_on'], 'source': w['source'], 'event_id': 'catalogue:' + operation['id'],
            'rated_by': {'name': w['user_name']}}, w['display_name'])

    def facts(self, operation, current):
        """Verify permitted changes, excluding only this operation's exact review/link."""
        e = operation['expected']; w = e['manual_watch']; before = e['catalogue_before']
        account = self.native.account()
        if account['id'] != w['account_id'] or account['source'] != w['source'] or account['scale'] != w['scale']:
            raise AdapterError('The viewing account or scale changed.')
        self._reset_guard(e['candidate']['tmdb'], current)
        cleaned = deepcopy(current)
        review = None; old = before['movie']
        if current['movie']:
            original = {r['id']: r for r in old['reviews']} if old else {}
            fresh = {r['id']: r for r in current['movie']['reviews']}
            if len(fresh) != len(current['movie']['reviews']) or any(fresh.get(k) != v for k, v in original.items()):
                raise AdapterError('Existing reviews changed.')
            extra = [r for k, r in fresh.items() if k not in original]
            if extra:
                if (len(extra) != 1 or w['rating'] is None or extra[0].get('textOriginal') != self.review_text(operation)
                        or extra[0].get('visibility') != 'PRIVATE' or extra[0].get('postedBy', {}).get('id') != w['account_id']
                        or _rating(extra[0].get('rating')) != w['rating'] * 20):
                    raise AdapterError('An unrelated review appeared.')
                review = extra[0]
                cleaned['movie']['reviews'] = deepcopy(old['reviews'] if old else [])
            for event in cleaned['movie']['history']:
                is_target = event['id'] == w['reuse'] if w['reuse'] else self.completion.marker(operation['id']) in event.get('providersConsumedOn', [])
                if is_target and review and event.get('reviewId') == review['id']:
                    prior = next((v for v in old['history'] if v['id'] == event['id']), None) if old else None
                    event['reviewId'] = prior.get('reviewId') if prior else None
        self.completion.preserved(before, cleaned, operation['id'], finished_on=w['finished_on'], reuse=w['reuse'], allow_unlinked=True)
        event = self.completion.result(before, cleaned, operation['id'], finished_on=w['finished_on'], reuse=w['reuse']) if cleaned['movie'] else None
        if event and w['reuse']:
            self._pending_rating_guard(w['source'], event['id'])
        if event and event.get('reviewId'):
            linked = [r for r in current['movie']['reviews'] if r['id'] == event['reviewId'] and r.get('postedBy', {}).get('id') == w['account_id']]
            if w['rating'] is not None and (len(linked) != 1 or _rating(linked[0].get('rating')) != w['rating'] * 20):
                raise AdapterError('An existing rating differs.')
            review = linked[0] if linked else None
        return event, review

    def preserved(self, operation, current):
        event, review = self.facts(operation, current)
        if event is None:
            raise AdapterError('Completed viewing is not verified.')
        w = operation['expected']['manual_watch']
        actual = next(v for v in current['movie']['history'] if v['id'] == event['id'])
        if w['rating'] is not None and (review is None or actual.get('reviewId') != review['id']):
            raise AdapterError('Rating linkage is not verified.')
        return event, review

    def inspect(self, name, operation, current):
        event, review = self.facts(operation, current)
        w = operation['expected']['manual_watch']
        if name == 'catalogue_completion':
            done = event is not None
        elif name == 'catalogue_review':
            done = w['rating'] is None or review is not None
        elif name == 'catalogue_review_link':
            actual = next((v for v in (current['movie'] or {}).get('history', []) if event and v['id'] == event['id']), None)
            done = w['rating'] is None or review is not None and actual is not None and actual.get('reviewId') == review['id']
        elif name == 'catalogue_watch_ledger':
            self.preserved(operation, current)
            row = self.storage.connection.execute('SELECT media_key FROM viewing_event WHERE source=? AND event_id=?', (w['source'], event['id'])).fetchone()
            key = 'ryot:' + current['movie']['metadata']['id']
            if row and row[0] != key:
                raise StorageError('Viewing ledger identity changed.')
            audit = self.storage.connection.execute(
                "SELECT 1 FROM audit_log WHERE action='catalogue_viewing_registered' AND json_extract(details_json,'$.operation_id')=?",
                (operation['id'],)).fetchone()
            done = row is not None and audit is not None
        else:
            raise ValueError('Unknown catalogue viewing step.')
        return Inspection(Outcome.VERIFIED if done else Outcome.NEEDS_WRITE, {'step': name})

    def apply(self, name, operation, current):
        e = operation['expected']; w = e['manual_watch']; event, review = self.facts(operation, current)
        if name == 'catalogue_completion':
            return self.completion.create(current, operation['id'], w['finished_on'])
        if not event:
            raise AdapterError('A verified completed watch is required.')
        if name == 'catalogue_review':
            value = Decimal(w['rating']) if w['scale'] == 'OUT_OF_FIVE' else Decimal(w['rating'] * 20)
            result = self.catalogue.client.execute('mutation CuratarrCatalogueRating($input:CreateOrUpdateReviewInput!){createOrUpdateReview(input:$input){id}}',
                {'input': {'entityId': current['movie']['metadata']['id'], 'entityLot': 'METADATA', 'rating': str(value),
                           'text': self.review_text(operation), 'visibility': 'PRIVATE'}})
            if not isinstance(result.get('createOrUpdateReview', {}).get('id'), str):
                raise AdapterError('Rating creation outcome is uncertain.')
            return {'accepted': True}
        if name == 'catalogue_review_link':
            if not review:
                raise AdapterError('The private rating must be verified first.')
            result = self.catalogue.client.execute('mutation CuratarrCatalogueRatingLink($input:UpdateSeenItemInput!){updateSeenItem(input:$input)}',
                {'input': {'seenId': event['id'], 'reviewId': review['id']}})
            if result.get('updateSeenItem') is not True:
                raise AdapterError('Rating linkage outcome is uncertain.')
            return {'accepted': True}
        if name == 'catalogue_watch_ledger':
            self.preserved(operation, current)
            key = 'ryot:' + current['movie']['metadata']['id']
            with self.storage.connection:
                self.storage.connection.execute('INSERT OR IGNORE INTO viewing_event(source,event_id,media_key,payload_json,decision_id) VALUES(?,?,?,?,NULL)',
                    (w['source'], event['id'], key, encode({'event_id': event['id'], 'media_key': key, 'source': 'curatarr_catalogue'})))
                # A manually confirmed fact must not remain an unanswered native-observer question.
                self._pending_rating_guard(w['source'], event['id'])
                self.storage.connection.execute("UPDATE pending_decision SET status='resolved' WHERE status='open' AND decision_type='viewing_followup' AND json_extract(context_json,'$.source')=? AND json_extract(context_json,'$.event_id')=?", (w['source'], event['id']))
                self.storage.connection.execute('INSERT INTO audit_log(actor,action,media_key,details_json) VALUES(?,?,?,?)',
                    (e['actor'], 'catalogue_viewing_registered', key, encode({'operation_id': operation['id'], 'event_id': event['id'], 'rating': w['rating']})))
            return {'linked': True}
        raise ValueError('Unknown catalogue viewing mutation.')
