"""Exact-ID catalogue registration and neutral provenance through native Ryot APIs."""
import json
import re
import secrets
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, build_opener
from urllib.error import HTTPError, URLError

from curatarr.adapters import AdapterError
from curatarr.integrations.radarr.removals import NoRedirect
from curatarr.integrations.ryot.viewing import HISTORY, REVIEWS


class RyotCatalogueWriter:
    def __init__(self, reader, upload_transport=None, marker='Curatarr'):
        if not marker or marker.casefold() in {getattr(reader.collections,k).casefold() for k in ['library','radarr','archived','deleted','previously_acquired','sonarr']} or marker.casefold() in {'owned','completed','watchlist','custom','monitoring','in progress'}:
            raise ValueError('The provenance collection must be distinct from lifecycle/system collections.')
        self.reader, self.client, self.upload_transport, self.marker = reader, reader.client, upload_transport, marker

    def account(self):
        data = self.client.execute('query CuratarrCatalogueAccount{userDetails{... on UserDetails{id}} userCollectionsList{response{id name isDefault creator{id}}}}')
        user, rows = data['userDetails']['id'], data['userCollectionsList']['response']
        if not isinstance(user,str) or not user or not isinstance(rows,list):
            raise AdapterError('The catalogue account could not be verified.')
        names = {}
        for row in rows:
            if row['creator']['id'] == user:
                if row['name'] in names:
                    raise AdapterError('Catalogue collection names are ambiguous.')
                names[row['name']] = row
        for name in [self.reader.collections.library,self.reader.collections.radarr,self.reader.collections.archived]:
            if name not in names or names[name]['isDefault'] or not names[name]['id']:
                raise AdapterError('Expected account-owned catalogue collections are unavailable.')
        if self.marker in names and names[self.marker]['isDefault']:
            raise AdapterError('The Curatarr marker cannot be a system collection.')
        return {'id':user,'collections':names}

    def snapshot(self, tmdb):
        account = self.account()
        fields = f'history{{{HISTORY}}} reviews{{{REVIEWS}}} collections{{details{{collectionId collectionName rank information}}}}'
        rows = [r for r in self.reader._library_records() if r['lot']=='MOVIE' and r['source']=='TMDB' and r['identifier']==tmdb]
        if len(rows)>1:
            raise AdapterError('Multiple Ryot records match this movie.')
        if not rows:
            return {'account_id':account['id'],'movie':None}
        identifier=rows[0]['id']
        query='query CuratarrCatalogueSnapshot($id:String!){metadataDetails(metadataId:$id){cacheId response{id title identifier lot source isPartial}} userMetadataDetails(metadataId:$id){cacheId response{'+fields+'}}}'
        data=self.client.execute(query,{'id':identifier})
        caches={data[key].get('cacheId') for key in ['metadataDetails','userMetadataDetails']} - {None}
        for cache in caches:
            if self.client.execute('mutation($id:UUID!){expireCacheKey(cacheId:$id)}',{'id':cache}).get('expireCacheKey') is not True:
                raise AdapterError('Fresh catalogue state could not be established.')
        if caches:
            data=self.client.execute(query,{'id':identifier})
        m,u=data['metadataDetails']['response'],data['userMetadataDetails']['response']
        if (m['id']!=identifier or m['lot']!='MOVIE' or m['source']!='TMDB' or m['identifier']!=tmdb
                or not isinstance(u['history'],list) or not isinstance(u['reviews'],list) or not isinstance(u['collections'],list)):
            raise AdapterError('Catalogue movie identity/history is incomplete.')
        collections={v['details']['collectionName']:v['details'] for v in u['collections']}
        if len(collections)!=len(u['collections']):
            raise AdapterError('Catalogue memberships are ambiguous.')
        for name,c in collections.items():
            if name in account['collections'] and c['collectionId']!=account['collections'][name]['id']:
                raise AdapterError('Catalogue collection ownership changed.')
        return {'account_id':account['id'],'movie':{'metadata':m,'history':u['history'],'reviews':u['reviews'],'collections':collections}}

    def preserved(self, before, after, *, allow_reactivate=False):
        if before['account_id']!=after['account_id']:
            raise AdapterError('Catalogue account changed.')
        old,new=before['movie'],after['movie']
        if new is None:
            raise AdapterError('Catalogue movie disappeared.')
        allowed={self.reader.collections.library,self.reader.collections.radarr,self.marker}
        if allow_reactivate:allowed.add(self.reader.collections.archived)
        if old is None:
            if new['history'] or new['reviews'] or set(new['collections'])-allowed:
                raise AdapterError('A new catalogue association has unexpected household state.')
            return
        normalize=lambda x:sorted(x,key=lambda v:json.dumps(v,sort_keys=True))
        if old['metadata']['id']!=new['metadata']['id'] or any(normalize(old[k])!=normalize(new[k]) for k in ['history','reviews']):
            raise AdapterError('Existing viewings or reviews changed during acquisition.')
        if {k:v for k,v in old['collections'].items() if k not in allowed}!={k:v for k,v in new['collections'].items() if k not in allowed}:
            raise AdapterError('Unrelated catalogue memberships changed.')

    def register_library(self, tmdb, title, account_id, operation_id):
        account=self.account()
        if account['id']!=account_id or not re.fullmatch('[a-f0-9]{32}',operation_id) or not re.fullmatch('[1-9][0-9]{0,9}',tmdb):
            raise AdapterError('Catalogue registration identity changed.')
        collection=account['collections'][self.reader.collections.library]
        now=datetime.now(timezone.utc).isoformat()
        payload={'metadata':[{'lot':'movie','source':'tmdb','identifier':tmdb,'source_id':'Curatarr '+operation_id,
                              'collections':[{'collection_id':collection['id'],'collection_name':collection['name'],
                                              'created_on':now,'last_updated_on':now,'creator_user_id':account_id,'information':None}],
                              'seen_history':[],'reviews':[]}]}
        path=self.upload(json.dumps(payload).encode(),operation_id)
        accepted=self.client.execute('mutation CuratarrRegisterCatalogue($input:DeployImportJobInput!){deployImportJob(input:$input)}',
                                     {'input':{'source':'GENERIC_JSON','path':{'exportPath':path}}})
        if accepted.get('deployImportJob') is not True:
            raise AdapterError('Catalogue import outcome is uncertain.')
        return {'import_accepted':True}

    def upload(self, payload, operation_id):
        if self.upload_transport:
            return self.upload_transport(payload,operation_id)
        origin=urlsplit(self.client.url)
        if origin.query or origin.fragment or not origin.path.endswith('/graphql'):
            raise AdapterError('Native upload endpoint is not configured.')
        url=urlunsplit((origin.scheme,origin.netloc,origin.path[:-len('/graphql')]+'/upload','',''))
        boundary='Curatarr'+secrets.token_hex(16)
        body=(f'--{boundary}\r\nContent-Disposition: form-data; name="files[]"; filename="curatarr-{operation_id}.json"\r\n'
              'Content-Type: application/json\r\n\r\n').encode()+payload+f'\r\n--{boundary}--\r\n'.encode()
        request=Request(url,data=body,method='POST',headers={'Authorization':'Bearer '+self.client.api_key,
                                                         'Content-Type':'multipart/form-data; boundary='+boundary})
        try:
            with build_opener(NoRedirect()).open(request,timeout=30) as response:
                if response.status!=200:raise AdapterError('Native import upload was not confirmed.')
                data=json.load(response)
        except (HTTPError,URLError,OSError,ValueError):
            raise AdapterError('Native import upload is unavailable.') from None
        if not isinstance(data,list) or len(data)!=1 or not isinstance(data[0],str) or not data[0]:
            raise AdapterError('Native import upload response is invalid.')
        return data[0]

    def refresh_metadata(self, snapshot):
        if snapshot['movie'] is None:
            raise AdapterError('Metadata must exist before refresh.')
        response=self.client.execute('mutation CuratarrCompleteMetadata($input:EntityWithLotInput!){deployUpdateMediaEntityJob(input:$input)}',
                                     {'input':{'entityId':snapshot['movie']['metadata']['id'],'entityLot':'METADATA'}})
        if response.get('deployUpdateMediaEntityJob') is not True:
            raise AdapterError('Metadata refresh outcome is uncertain.')
        return {'refresh_accepted':True}

    def ensure_marker(self):
        account=self.account()
        if self.marker in account['collections']:return {'collection_id':account['collections'][self.marker]['id']}
        data=self.client.execute('mutation CuratarrCreateMarker($input:CreateOrUpdateCollectionInput!){createOrUpdateCollection(input:$input){id}}',
                                 {'input':{'name':self.marker,'description':'Movies successfully added or updated through Curatarr.'}})
        identifier=data.get('createOrUpdateCollection',{}).get('id')
        if not isinstance(identifier,str) or not identifier:raise AdapterError('Marker creation outcome is uncertain.')
        return {'collection_id':identifier}

    def membership(self, snapshot, name, add):
        account=self.account()
        if snapshot['movie'] is None or snapshot['account_id']!=account['id']:
            raise AdapterError('Catalogue membership target changed.')
        allowed={self.reader.collections.library,self.reader.collections.radarr,self.marker}
        if not add:allowed={self.reader.collections.archived}
        if name not in allowed or name not in account['collections'] or account['collections'][name]['isDefault']:
            raise AdapterError('Unsupported catalogue membership change.')
        field='deployAddEntitiesToCollectionJob' if add else 'deployRemoveEntitiesFromCollectionJob'
        data=self.client.execute('mutation CuratarrAcquisitionMembership($input:ChangeCollectionToEntitiesInput!){'+field+'(input:$input)}',
                                 {'input':{'creatorUserId':account['id'],'collectionName':name,
                                           'entities':[{'entityId':snapshot['movie']['metadata']['id'],'entityLot':'METADATA'}]}})
        if data.get(field) is not True:raise AdapterError('Catalogue membership outcome is uncertain.')
        return {'accepted':True}
