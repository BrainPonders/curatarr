"""Explicit profile-only intent, shared movie reservations and verified provenance."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib

from curatarr.adapters import AdapterError
from curatarr.services.operations import Inspection, OperationRunner, OperationStep, Outcome
from curatarr.storage import StorageError
from curatarr.storage.journal import CoordinationJournal, encode
from curatarr.storage.viewing import ViewingStore


class MovieProfileChanges:
    kind = 'movie_profile'
    steps = ['guard', 'profile', 'marker_collection', 'marker_membership', 'preservation']

    def __init__(self, storage, radarr, catalogue):
        self.storage, self.radarr, self.catalogue = storage, radarr, catalogue
        self.journal = CoordinationJournal(storage)

    def pending(self, tmdb):
        return [self.journal.operation(r[0]) for r in self.storage.connection.execute(
            "SELECT id FROM coordination_operation WHERE media_key=? AND kind='movie_profile' "
            "AND status NOT IN ('completed','cancelled') ORDER BY created_at DESC LIMIT 10", ('tmdb:' + tmdb,))]

    def reset_guard(self, candidate, catalogue):
        store = ViewingStore(self.storage)
        if store.reset_pending('tmdb:' + candidate['tmdb']) or catalogue['movie'] and store.reset_pending('ryot:' + catalogue['movie']['metadata']['id']):
            raise ValueError('A history correction is pending for this movie.')

    def choices(self, candidate, *, actor):
        before = self.radarr.snapshot(candidate)
        profiles = self.radarr.profiles()
        current = next((p for p in profiles if p['id'] == before['qualityProfileId']), None)
        if current is None:
            raise ValueError('The current profile is unavailable. Refresh before editing.')
        catalogue = self.catalogue.snapshot(candidate['tmdb'])
        self.reset_guard(candidate, catalogue)
        if catalogue['movie'] is None:
            raise ValueError('Register this movie in the catalogue before changing its profile.')
        if self.catalogue.reader.collections.archived in catalogue['movie']['collections']:
            raise ValueError('This archived movie needs its restoration workflow, not a profile edit.')
        if self.pending(candidate['tmdb']):
            raise ValueError('A profile change is already pending. Check its saved status.')
        context = {'candidate': deepcopy(candidate), 'before': before, 'profiles': profiles,
                   'catalogue_before': catalogue, 'before_profile': deepcopy(current), 'actor': actor}
        fingerprint = hashlib.sha256(encode(context).encode()).hexdigest()
        decision = self.storage.create_pending_decision(media_key='tmdb:' + candidate['tmdb'],
            decision_type='radarr_profile_choices', audience=actor, state_fingerprint=fingerprint,
            context=context, lifetime_seconds=600)
        return {'decision_id': decision.decision_id, 'version': fingerprint, 'title': candidate['title'],
                'current_profile_id': current['id'], 'current_profile': current['name'],
                'profiles': [{'id': p['id'], 'name': p['name']} for p in profiles]}

    def confirm(self, decision_id, *, actor, version, profile_id):
        decision = self.storage.get_pending_decision(decision_id)
        if (decision is None or decision.decision_type != 'radarr_profile_choices'
                or decision.audience != actor or decision.state_fingerprint != version):
            raise ValueError('This profile selection is unavailable or belongs to another user.')
        profile = next((p for p in decision.context['profiles'] if type(profile_id) is int and p['id'] == profile_id), None)
        if profile is None or profile_id == decision.context['before']['qualityProfileId']:
            raise ValueError('Select a different offered profile.')
        key = 'profile:' + str(decision_id)
        old = self.storage.connection.execute('SELECT id FROM coordination_operation WHERE request_key=?', (key,)).fetchone()
        if old:
            operation = self.journal.operation(old[0])
            if operation['expected']['profile'] != profile or operation['authorization']['actor'] != actor:
                raise ValueError('This request was already saved with a different profile.')
            return operation
        if (decision.status != 'open' or not decision.expires_at
                or datetime.fromisoformat(decision.expires_at).replace(tzinfo=timezone.utc) <= datetime.now(timezone.utc)):
            raise ValueError('This profile selection expired. Reopen it before saving.')
        context = {k: deepcopy(v) for k, v in decision.context.items() if k != 'profiles'}
        context['profile'] = deepcopy(profile)
        if self.pending(context['candidate']['tmdb']):
            raise ValueError('Another profile change is already pending.')
        # No provider effects until a saved, actor-bound version is explicitly confirmed.
        self.guard({'expected': context, 'steps': [{'name': name, 'status': 'planned'} for name in self.steps]})
        operation = self.journal.create_operation(key, decision.media_key, self.kind,
            {'actor': actor, 'basis': 'confirmed_profile_change'}, context, self.steps)
        self.storage.close_open_decision(decision_id, 'resolved')
        return operation

    def catalogue_preserved(self, expected, current):
        before = expected['catalogue_before']
        self.catalogue.preserved(before, current)
        old, new = before['movie'], current['movie']
        if old is None or new is None or before['account_id'] != current['account_id']:
            raise AdapterError('The catalogue association changed.')
        without_marker = lambda movie: {k: v for k, v in movie['collections'].items() if k != self.catalogue.marker}
        if without_marker(old) != without_marker(new):
            raise AdapterError('An existing catalogue membership changed.')

    def guard(self, operation):
        e = operation['expected']; current = self.radarr.snapshot(e['candidate'])
        before = e['before']; allowed = deepcopy(before); allowed['qualityProfileId'] = current['qualityProfileId']
        if current != allowed or current['qualityProfileId'] not in {before['qualityProfileId'], e['profile']['id']}:
            raise AdapterError('Movie identity, file, monitoring, path or profile changed.')
        profile_step = next(s for s in operation['steps'] if s['name'] == 'profile')
        if profile_step['status'] == 'planned' and current != before:
            raise AdapterError('Another actor changed the profile before this operation started.')
        if profile_step['status'] == 'verified' and current['qualityProfileId'] != e['profile']['id']:
            raise AdapterError('The verified profile changed again.')
        profiles = self.radarr.profiles()
        if current['qualityProfileId'] == before['qualityProfileId'] and next((p for p in profiles if p['id'] == before['qualityProfileId']), None) != e['before_profile']:
            raise AdapterError('The original profile definition changed.')
        offered = next((p for p in profiles if p['id'] == e['profile']['id']), None)
        if offered != e['profile']:
            raise AdapterError('The selected native profile definition changed.')
        catalogue = self.catalogue.snapshot(e['candidate']['tmdb'])
        self.reset_guard(e['candidate'], catalogue)
        self.catalogue_preserved(e, catalogue)
        return current, catalogue

    def inspect(self, name, operation):
        current, catalogue = self.guard(operation); e = operation['expected']
        if name == 'guard':
            done = True
        elif name == 'profile':
            done = current['qualityProfileId'] == e['profile']['id']
        elif name == 'marker_collection':
            done = self.catalogue.marker in self.catalogue.account()['collections']
        elif name == 'marker_membership':
            done = self.catalogue.marker in catalogue['movie']['collections']
        elif name == 'preservation':
            done = current['qualityProfileId'] == e['profile']['id']
        else:
            raise ValueError('Unsupported profile operation step.')
        return Inspection(Outcome.VERIFIED if done else Outcome.NEEDS_WRITE, {'step': name, 'profile_id': current['qualityProfileId']})

    def apply(self, name, operation):
        current, catalogue = self.guard(operation); e = operation['expected']
        if name == 'profile':
            return self.radarr.change_profile(e['candidate'], current, e['profile'])
        if name == 'marker_collection':
            return self.catalogue.ensure_marker()
        if name == 'marker_membership':
            return self.catalogue.membership(catalogue, self.catalogue.marker, True)
        raise ValueError('This step has no profile mutation.')

    def run(self, identifier):
        if self.journal.operation(identifier)['kind'] != self.kind:
            raise ValueError('Wrong operation kind.')
        plan = [OperationStep(name, lambda op, name=name: self.inspect(name, op),
                              lambda op, name=name: self.apply(name, op)) for name in self.steps]
        return OperationRunner(self.journal, {self.kind: plan}).run(identifier)
