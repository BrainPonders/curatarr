"""Shared catalogue-only and add/search authorization, outside transport-specific UI."""
from copy import deepcopy
import hashlib
import json
import re

from curatarr.adapters import AdapterError
from curatarr.services.operations import Inspection, OperationRunner, OperationStep, Outcome
from curatarr.storage import StorageError
from curatarr.storage.journal import CoordinationJournal, encode


class OriginChanged(StorageError, ValueError):
    """A saved source/household intent was revoked before the next effect."""


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


class MovieAcquisition:
    def __init__(self, storage, catalogue, radarr, watch=None):
        self.storage, self.catalogue, self.radarr = storage, catalogue, radarr
        self.journal = CoordinationJournal(storage)
        self.watch = watch

    def preview(self, candidate, *, actor, mode, profile_id=None, root_id=None, reactivate=False, origin=None,
                watched=False, watched_on=None, rating=None, user_name=None):
        tmdb,imdb=candidate['tmdb'],candidate.get('imdb')
        if not isinstance(tmdb,str) or not re.fullmatch('[1-9][0-9]{0,9}',tmdb):
            raise ValueError('An exact TMDB movie identity is required.')
        if mode not in {'catalogue_only','add_search'} or type(reactivate) is not bool:
            raise ValueError('Choose Catalogue only or Add and search.')
        if mode != 'catalogue_only' and (watched or watched_on is not None or rating is not None):
            raise ValueError('Past viewing details belong to catalogue registration only.')
        if type(watched) is not bool:
            raise ValueError('Watched must be on or off.')
        before=self.catalogue.snapshot(tmdb)
        movie=self.radarr.movie(tmdb,imdb)
        if movie is not None:
            raise ValueError('This movie is already active in Radarr; use its management options.')
        archived=bool(before['movie'] and self.catalogue.reader.collections.archived in before['movie']['collections'])
        if archived and not reactivate and mode=='add_search':
            raise ValueError('Explicitly confirm reactivation of this archived movie.')
        profile=root=None
        if mode=='add_search':
            if type(profile_id) is not int or profile_id<=0 or type(root_id) is not int or root_id<=0:
                raise ValueError('Choose a valid native profile and root folder.')
            if self.radarr.excluded(tmdb):
                raise ValueError('This movie has a native exclusion; its ownership needs review before reactivation.')
            profile,root=self.radarr.selection(profile_id,root_id)
        elif profile_id is not None or root_id is not None or reactivate:
            raise ValueError('Catalogue only has no download profile, root selection or reactivation.')
        context={'candidate':deepcopy(candidate),'mode':mode,'profile':profile,'root':root,'reactivate':reactivate,
                 'catalogue_before':before,'radarr_before':movie,'actor':actor}
        if watched or watched_on is not None or rating is not None:
            if actor.startswith('source:'):
                raise ValueError('Automatic source policies cannot register past viewings.')
            if self.watch is None:
                raise ValueError('Past viewing registration is unavailable.')
            context['manual_watch'] = self.watch.prepare(before, candidate, watched=watched, watched_on=watched_on, rating=rating, user_name=user_name)
        if origin:
            context['origin'] = deepcopy(origin)
            self._origin_guard(context)
        fingerprint=digest(context)
        decision=self.storage.create_pending_decision(media_key='tmdb:'+tmdb,decision_type='acquisition_preview',audience=actor,
                                                       state_fingerprint=fingerprint,context=context,lifetime_seconds=600)
        return {'decision_id':decision.decision_id,'version':fingerprint,'title':candidate['title'],'mode':mode,
                'profile':profile['name'] if profile else None,'reactivate':reactivate}

    def confirm(self, decision_id, *, actor, version):
        decision=self.storage.get_pending_decision(decision_id)
        if decision is None or decision.decision_type!='acquisition_preview' or decision.audience!=actor or decision.state_fingerprint!=version:
            raise StorageError('The acquisition preview is unavailable or changed.')
        from datetime import datetime,timezone
        if not decision.expires_at or datetime.fromisoformat(decision.expires_at).replace(tzinfo=timezone.utc)<=datetime.now(timezone.utc):
            raise StorageError('The acquisition preview expired. Refresh before continuing.')
        origin = decision.context.get('origin')
        key = ('wanted:'+origin['source_id']+':'+digest(origin['item_key']) if origin and origin['kind']=='wanted' else
               'discovery:'+decision.media_key+':'+str(origin['revision']) if origin else 'acquisition:'+str(decision_id))
        existing=self.storage.connection.execute('SELECT id FROM coordination_operation WHERE request_key=?',(key,)).fetchone()
        if existing:
            operation = self.journal.operation(existing[0])
            if operation['authorization']['actor'] != actor or operation['expected'] != decision.context:
                raise StorageError('Another household member is already handling this request.')
            return operation
        if decision.status!='open':
            raise StorageError('The acquisition preview is closed.')
        context=deepcopy(decision.context)
        self._guard_context(context,initial=True)
        steps=['guard','library','metadata_ready']
        if context['mode']=='add_search':
            if context['reactivate']:steps.append('reactivate')
            steps+=['radarr_movie','radarr_membership','search']
        if context.get('manual_watch'):
            steps += self.watch.steps
        steps+=['marker_collection','marker_membership','preservation']
        operation=self.journal.create_operation(key,decision.media_key,'movie_acquisition',
            {'actor':actor,'basis':'trusted_wanted_source' if origin and origin['kind']=='wanted' and actor.startswith('source:') else 'confirmed_preview'}, context,steps,origin=origin)
        self.storage.close_open_decision(decision_id,'resolved')
        return operation

    def run(self, identifier):
        operation=self.journal.operation(identifier)
        if operation['kind']!='movie_acquisition':raise ValueError('Wrong operation type.')
        context=operation['expected']
        names=['guard','library','metadata_ready']
        if context['mode']=='add_search':
            if context['reactivate']:names.append('reactivate')
            names+=['radarr_movie','radarr_membership','search']
        if context.get('manual_watch'):
            if self.watch is None:
                raise ValueError('Past viewing registration is unavailable.')
            names += self.watch.steps
        names+=['marker_collection','marker_membership','preservation']
        plans=[OperationStep(name,lambda op,name=name:self.inspect(name,op),lambda op,name=name:self.apply(name,op)) for name in names]
        return OperationRunner(self.journal,{'movie_acquisition':plans}).run(identifier)

    def _origin_guard(self, context, *, operation=None):
        origin = context.get('origin')
        if not origin:
            return
        db = self.storage.connection
        if origin['kind'] == 'wanted':
            source = self.journal.get_source(origin['source_id'])
            import json
            row = db.execute('SELECT * FROM wanted_request WHERE source_id=? AND item_key=?',
                             (origin['source_id'], origin['item_key'])).fetchone()
            selected = db.execute("SELECT payload_json FROM state_snapshot WHERE media_key='sources' AND source='wanted_selection'").fetchone()
            state = 'applying' if operation else 'new'
            if (source['revision'] != origin['revision'] or context['actor'].startswith('source:') and not json.loads(source['config_json']).get('enabled')
                    or not selected or json.loads(selected[0])['id'] != source['id']
                    or row is None or row['state'] != state or operation and row['operation_id'] != operation['id']):
                raise OriginChanged('Wanted policy or request changed. Maintainer review may be needed.')
            if context['actor'].startswith('source:'):
                policy = json.loads(source['config_json'])
                if (context['mode'] != policy['mode'] or context['reactivate'] or context['mode']=='add_search' and
                        (context['profile']['id'] != policy['profile_id'] or context['profile']['name'] != policy['profile_name']
                         or context['root']['id'] != policy['root_id'] or context['root']['path'] != policy['root_path'])):
                    raise OriginChanged('The saved wanted policy does not authorize this action.')
        elif origin['kind'] == 'discovery':
            row = db.execute('SELECT * FROM discovery_candidate WHERE movie_key=?', ('tmdb:'+context['candidate']['tmdb'],)).fetchone()
            if row is None or row['decision'] != 'selected' or row['revision'] != origin['revision']:
                raise OriginChanged('This suggestion changed. Refresh before continuing.')
        else:
            raise ValueError('Unsupported acquisition origin.')

    def _guard_context(self, context, *, initial=False, operation=None):
        self._origin_guard(context, operation=operation)
        candidate=context['candidate'];tmdb=candidate['tmdb']
        current=self.catalogue.snapshot(tmdb);movie=self.radarr.movie(tmdb,candidate.get('imdb'))
        if initial:
            if current!=context['catalogue_before'] or movie!=context['radarr_before']:
                raise StorageError('Movie state changed after preview. Refresh before continuing.')
        else:
            steps={s['name']:s for s in operation['steps']}
            untouched=all(s['status']=='planned' for s in operation['steps'] if s['name']!='guard')
            if untouched:
                if current!=context['catalogue_before'] or movie!=context['radarr_before']:
                    raise AdapterError('Another operation changed this movie before acquisition started.')
            elif current['movie'] is not None:
                if context.get('manual_watch'):
                    self.watch.facts(operation, current)
                else:
                    self.catalogue.preserved(context['catalogue_before'],current,allow_reactivate=context['reactivate'])
            elif context['catalogue_before']['movie'] is not None:
                raise AdapterError('Existing catalogue history disappeared.')
            if movie is not None and steps.get('radarr_movie',{}).get('status','planned')=='planned':
                raise AdapterError('An unrelated native movie add changed this request.')
        if context.get('manual_watch') and initial:
            # Recheck account/reset/history without changing the approved viewing intent.
            self.watch.facts({'id': '0'*32, 'expected': context}, current)
        if current['account_id']!=context['catalogue_before']['account_id']:
            raise AdapterError('The catalogue account changed.')
        if context['mode']=='add_search':
            profile,root=self.radarr.selection(context['profile']['id'],context['root']['id'])
            if profile!=context['profile'] or root['path']!=context['root']['path'] or self.radarr.excluded(tmdb):
                raise AdapterError('Profile, root or exclusion state changed after confirmation.')
            from pathlib import PurePosixPath
            if movie is not None and (movie.get('qualityProfileId')!=profile['id'] or movie.get('monitored') is not True
                                       or PurePosixPath(root['path']) not in PurePosixPath(movie.get('path','')).parents):
                raise AdapterError('The active movie no longer matches the confirmed acquisition policy.')
        return current,movie

    def inspect(self, name, operation):
        context=operation['expected'];candidate=context['candidate'];tmdb=candidate['tmdb']
        self._origin_guard(context, operation=operation)
        if name=='guard':
            self._guard_context(context,operation=operation)
            return Inspection(Outcome.VERIFIED,{'guard':'fresh_native_state'})
        if name=='marker_collection':
            account=self.catalogue.account()
            return Inspection(Outcome.VERIFIED if self.catalogue.marker in account['collections'] else Outcome.NEEDS_WRITE,
                              {'account_id':account['id']})
        if name=='search':
            step=next(s for s in operation['steps'] if s['name']==name)
            evidence=json.loads(step['evidence_json']).get('result',{})
            if evidence.get('command_id'):
                movie=self.radarr.movie(tmdb,candidate.get('imdb'))
                if movie is None:raise AdapterError('Movie disappeared before search verification.')
                return Inspection(Outcome.VERIFIED,self.radarr.command(evidence['command_id'],movie['id']))
            return Inspection(Outcome.NEEDS_WRITE,{'command_id_recorded':False})
        current=self.catalogue.snapshot(tmdb);movie=current['movie']
        if context.get('manual_watch') and name in self.watch.steps:
            return self.watch.inspect(name, operation, current)
        if name=='library':
            done=movie is not None and self.catalogue.reader.collections.library in movie['collections']
        elif name=='metadata_ready':
            done=movie is not None and 'isPartial' in movie['metadata'] and movie['metadata']['isPartial'] is not True
        elif name=='reactivate':
            done=movie is not None and self.catalogue.reader.collections.archived not in movie['collections']
        elif name=='radarr_membership':
            done=movie is not None and self.catalogue.reader.collections.radarr in movie['collections']
        elif name=='marker_membership':
            done=movie is not None and self.catalogue.marker in movie['collections']
        elif name=='radarr_movie':
            native=self.radarr.movie(tmdb,candidate.get('imdb'))
            done=native is not None
            from pathlib import PurePosixPath
            if done and (native.get('qualityProfileId')!=context['profile']['id'] or native.get('monitored') is not True
                         or PurePosixPath(context['root']['path']) not in PurePosixPath(native.get('path','')).parents):
                return Inspection(Outcome.CONFLICT,{'reason':'different_acquisition_policy'})
        elif name=='preservation':
            if context.get('manual_watch'):
                self.watch.preserved(operation, current)
            else:
                self.catalogue.preserved(context['catalogue_before'],current,allow_reactivate=context['reactivate'])
            return Inspection(Outcome.VERIFIED,{'history_and_reviews':'preserved'})
        else:raise ValueError('Unknown acquisition step.')
        return Inspection(Outcome.VERIFIED if done else Outcome.NEEDS_WRITE,{'tmdb':tmdb,'step':name,'verified':done})

    def apply(self, name, operation):
        context=operation['expected'];candidate=context['candidate'];tmdb=candidate['tmdb']
        current,native=self._guard_context(context,operation=operation)
        if context.get('manual_watch') and name in self.watch.steps:
            return self.watch.apply(name, operation, current)
        if name=='library':
            if current['movie'] is None:
                return self.catalogue.register_library(tmdb,candidate['title'],current['account_id'],operation['id'])
            return self.catalogue.membership(current,self.catalogue.reader.collections.library,True)
        if name=='metadata_ready':return self.catalogue.refresh_metadata(current)
        if name=='marker_collection':return self.catalogue.ensure_marker()
        if name in {'reactivate','radarr_membership','marker_membership'}:
            name_on_provider={'reactivate':self.catalogue.reader.collections.archived,'radarr_membership':self.catalogue.reader.collections.radarr,'marker_membership':self.catalogue.marker}[name]
            return self.catalogue.membership(current,name_on_provider,name!='reactivate')
        if name=='radarr_movie':return self.radarr.add(tmdb,candidate.get('imdb'),context['profile'],context['root'])
        if name=='search':
            if native is None:raise AdapterError('A verified native movie is required before search.')
            return self.radarr.search(native)
        raise ValueError('This step has no native mutation.')
