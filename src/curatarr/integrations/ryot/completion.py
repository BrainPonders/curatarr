"""One native completed viewing with an exact recoverable consumption reference."""
from datetime import datetime
import re

from curatarr.adapters import AdapterError
from curatarr.storage.journal import encode


class RyotCompletionWriter:
    def __init__(self,catalogue):self.catalogue=catalogue

    @staticmethod
    def marker(operation):
        if not re.fullmatch('[a-f0-9]{32}',operation):raise ValueError('Invalid viewing operation.')
        return 'Jellyfin via Curatarr · CV-'+operation

    def history(self,snapshot):
        movie=snapshot['movie']
        if movie is None:return {}
        result={}
        for event in movie['history']:
            if (not isinstance(event,dict) or not isinstance(event.get('id'),str) or event['id'] in result
                    or event.get('userId')!=snapshot['account_id'] or event.get('metadataId')!=movie['metadata']['id']
                    or not isinstance(event.get('state'),str) or not isinstance(event.get('providersConsumedOn'),list)
                    or any(not isinstance(v,str) for v in event['providersConsumedOn'])):
                raise AdapterError('Native viewing ownership/history is incomplete.')
            result[event['id']]=event
        return result

    def result(self,before,after,operation,*,finished_on=None,reuse=None):
        if before['account_id']!=after['account_id'] or after['movie'] is None:
            raise AdapterError('The viewing account or catalogue association changed.')
        old,current=self.history(before),self.history(after)
        if any(current.get(key)!=event for key,event in old.items()):
            raise AdapterError('Existing viewing history changed; do not create or select another event.')
        if reuse:
            if set(old)!=set(current) or reuse not in current or current[reuse]['state']!='COMPLETED':
                raise AdapterError('The existing completed viewing changed.')
            return current[reuse]
        marked=[e for e in current.values() if self.marker(operation) in e['providersConsumedOn']]
        if len(marked)>1:raise AdapterError('Duplicate operation references require maintainer review.')
        extras=set(current)-set(old)
        if not marked:
            if extras:raise AdapterError('An unrelated viewing appeared during this operation.')
            return None
        event=marked[0]
        if event['id'] in old or extras!={event['id']} or event['state']!='COMPLETED':
            raise AdapterError('The completed viewing could not be attributed exactly.')
        actual=event.get('finishedOn')
        if finished_on is None:
            if actual is not None:raise AdapterError('A viewing date was invented by the native service.')
        elif not isinstance(actual,str) or datetime.fromisoformat(actual.replace('Z','+00:00'))!=datetime.fromisoformat(finished_on.replace('Z','+00:00')):
            raise AdapterError('The native viewing date differs from the source evidence.')
        return event

    def preserved(self,before,after,operation,*,finished_on=None,reuse=None,allow_unlinked=False):
        if before['account_id']!=after['account_id']:raise AdapterError('The catalogue account changed.')
        if after['movie'] is None:
            if before['movie'] is not None:raise AdapterError('The catalogue association disappeared.')
            return
        old=before['movie'];new=after['movie']
        if old and old['metadata']['id']!=new['metadata']['id']:
            raise AdapterError('The catalogue identity changed.')
        if (old['reviews'] if old else [])!=new['reviews']:
            raise AdapterError('Existing ratings or reviews changed.')
        previous=old['collections'] if old else {}
        for key,value in previous.items():
            if new['collections'].get(key)!=value:raise AdapterError('An existing collection changed.')
        allowed={self.catalogue.reader.collections.library,self.catalogue.marker,'Completed','Owned'}
        if set(new['collections'])-set(previous)-allowed:
            raise AdapterError('An unrelated collection was added.')
        event=self.result(before,after,operation,finished_on=finished_on,reuse=reuse)
        if event is None and not allow_unlinked:raise AdapterError('Completed viewing is not yet verified.')
        return event

    def create(self,snapshot,operation,finished_on):
        movie=snapshot['movie']
        if movie is None:raise AdapterError('Exact movie registration must complete first.')
        common={'providersConsumedOn':[self.marker(operation)]}
        change={'finishedOnDate':dict(common,timestamp=finished_on)} if finished_on else {'withoutDates':common}
        result=self.catalogue.client.execute('mutation CuratarrCompleteViewing($input:[MetadataProgressUpdateInput!]!){deployBulkMetadataProgressUpdate(input:$input)}',
            {'input':[{'metadataId':movie['metadata']['id'],'change':{'createNewCompleted':change}}]})
        if result.get('deployBulkMetadataProgressUpdate') is not True:
            raise AdapterError('Native viewing creation outcome is uncertain.')
        return {'accepted':True}
