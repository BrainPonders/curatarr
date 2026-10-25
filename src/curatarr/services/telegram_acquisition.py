"""Private-chat acquisition choices backed by the shared preview/operation service."""
from datetime import datetime, timezone
from pathlib import PurePosixPath

from curatarr.storage import StorageError
from curatarr.services.telegram import TelegramButton, TelegramPrompt


class TelegramAcquisition:
    def __init__(self, service):
        self.service, self.storage = service, service.storage

    def open(self, decision, actor):
        raw=decision.context['candidate']
        identities=raw['identities']
        values=[i if isinstance(i,str) else ':'.join(i) for i in identities]
        tmdb=[i[5:] for i in values if i.startswith('tmdb:')]
        imdb=[i[5:] for i in values if i.startswith('imdb:')]
        if len(tmdb)!=1 or len(imdb)>1:
            raise ValueError('Exact movie identity needs review.')
        options=self.service.radarr.options()
        candidate={'tmdb':tmdb[0],'imdb':imdb[0] if imdb else None,'title':raw['title'],'year':raw.get('year')}
        context={'candidate':candidate,'profiles':[{'id':p['id'],'name':p['name']} for p in options['profiles']],
                 'roots':[{'id':r['id'],'name':PurePosixPath(r['path']).name} for r in options['roots']],
                 'reactivate':decision.decision_type=='reactivate_archived'}
        chooser=self.storage.create_pending_decision(media_key='tmdb:'+tmdb[0],decision_type='acquisition_choices',audience=actor,
                                                      state_fingerprint=decision.state_fingerprint,context=context,lifetime_seconds=600)
        buttons=tuple(TelegramButton(p['name'][:64],f'acq:{chooser.decision_id}:profile:{p["id"]}',row=n)
                      for n,p in enumerate(context['profiles']))
        return TelegramPrompt(candidate['title']+'\n\nChoose a download profile, or save the movie in the catalogue only.',
                              buttons+(TelegramButton('Catalogue only',f'acq:{chooser.decision_id}:catalogue',row=len(buttons)),))

    def handle(self, data, actor):
        parts=data.split(':')
        if len(parts)==3 and parts[0]=='acqop' and parts[2]=='refresh':
            operation=self.service.journal.operation(parts[1])
            if operation['authorization']['actor']!=actor:
                raise StorageError('This operation belongs to another user.')
            return self.status(self.service.run(operation['id']))
        if len(parts) not in {3,4} or parts[0]!='acq' or not parts[1].isascii() or not parts[1].isdigit():
            raise ValueError('Invalid acquisition button.')
        decision=self.storage.get_pending_decision(int(parts[1]))
        if decision is None or decision.audience!=actor or decision.decision_type not in {'acquisition_choices','acquisition_preview'}:
            raise StorageError('This acquisition choice is unavailable.')
        if not decision.expires_at or datetime.fromisoformat(decision.expires_at).replace(tzinfo=timezone.utc)<=datetime.now(timezone.utc):
            raise StorageError('This acquisition choice expired.')
        action=parts[2]
        if decision.decision_type=='acquisition_preview' and action=='confirm' and len(parts)==3:
            operation=self.service.confirm(decision.decision_id,actor=actor,version=decision.state_fingerprint)
            return self.status(self.service.run(operation['id']))
        if decision.status!='open' or decision.decision_type!='acquisition_choices':
            raise StorageError('This acquisition choice is already closed.')
        context=decision.context
        if action=='profile' and len(parts)==4 and parts[3].isascii() and parts[3].isdigit():
            profile=int(parts[3])
            if not any(p['id']==profile for p in context['profiles']):raise ValueError('This profile was not offered.')
            roots=self.storage.create_pending_decision(media_key=decision.media_key,decision_type='acquisition_choices',audience=actor,
                                                       state_fingerprint=decision.state_fingerprint,context={**context,'profile_id':profile},lifetime_seconds=600)
            self.storage.close_open_decision(decision.decision_id,'resolved')
            return TelegramPrompt(context['candidate']['title']+'\n\nChoose the library folder.',
                                  tuple(TelegramButton(r['name'][:64],f'acq:{roots.decision_id}:root:{r["id"]}') for r in context['roots']))
        if action=='catalogue' and len(parts)==3:
            preview=self.service.preview(context['candidate'],actor=actor,mode='catalogue_only')
        elif action=='root' and len(parts)==4 and parts[3].isascii() and parts[3].isdigit():
            root=int(parts[3])
            if not context.get('profile_id') or not any(r['id']==root for r in context['roots']):raise ValueError('This folder was not offered.')
            preview=self.service.preview(context['candidate'],actor=actor,mode='add_search',profile_id=context['profile_id'],root_id=root,
                                         reactivate=context['reactivate'])
        else:raise ValueError('Invalid acquisition choice.')
        self.storage.close_open_decision(decision.decision_id,'resolved')
        text=(('Reactivate and search' if preview['reactivate'] else 'Add and search')+'\nProfile: '+preview['profile'] if preview['mode']=='add_search' else 'Catalogue only · no download requested')
        return TelegramPrompt(context['candidate']['title']+'\n\n'+text,
                              (TelegramButton('Confirm',f'acq:{preview["decision_id"]}:confirm'),))

    @staticmethod
    def status(operation):
        states={'completed':'Saved successfully.','pending':'Your request is saved and waiting to start.',
                'running':'Your request is being processed.','uncertain':'The service response is being checked. Your request is saved.',
                'blocked':'Your request needs maintainer attention.','cancelled':'Request cancelled.'}
        buttons=() if operation['status'] in {'completed','cancelled'} else (TelegramButton('Refresh status',f'acqop:{operation["id"]}:refresh'),)
        return TelegramPrompt(operation['expected']['candidate']['title']+'\n\n'+states[operation['status']],buttons)
