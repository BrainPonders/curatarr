"""Installed Default webhook format: strict movie hints, no history mutations."""
from datetime import datetime, timezone
import hashlib
import json
import re

from curatarr.adapters import AdapterError
from curatarr.integrations.jellyfin.removals import JellyfinRemovalVerifier


EVENTS = {'Play','Pause','Resume','Progress','Stop','MarkPlayed','MarkUnplayed'}


def member(value, name, default=None):
    if not isinstance(value,dict):
        raise ValueError('Invalid webhook object.')
    camel = name[0].lower()+name[1:]
    if name in value and camel in value and value[name]!=value[camel]:
        raise ValueError('Webhook property casing is ambiguous.')
    return value.get(name,value.get(camel,default))


def identifier(value, *, guid=False):
    if not isinstance(value,str) or not value or len(value)>128 or any(ord(c)<32 for c in value):
        raise ValueError('A reliable source identifier is required.')
    if guid:
        if not re.fullmatch(r'(?:[a-fA-F0-9]{32}|[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12})',value):
            raise ValueError('Invalid Jellyfin item/user identifier.')
        return value.replace('-','').lower()
    return value


def timestamp(value):
    if value is None:
        return None
    if not isinstance(value,str) or len(value)>64:
        raise ValueError('Invalid source timestamp.')
    try:
        result=datetime.fromisoformat(value.replace('Z','+00:00'))
    except ValueError:
        raise ValueError('Invalid source timestamp.') from None
    if result.tzinfo is None or result.year<1900:
        raise ValueError('A source timestamp requires a time zone.')
    return result.astimezone(timezone.utc).isoformat()


def viewing_hint(payload):
    if not isinstance(payload,dict):
        raise ValueError('Invalid Jellyfin webhook.')
    event=member(payload,'Event')
    if event not in EVENTS:
        return None  # ItemRemoved and other events never enter position parsing.
    item=member(payload,'Item')
    if not isinstance(item,dict):
        raise ValueError('Missing webhook item.')
    if member(item,'Type')!='Movie':
        return None
    user=member(payload,'User');server=member(payload,'Server')
    item_id=identifier(member(item,'Id'),guid=True);user_id=identifier(member(user,'Id'),guid=True)
    server_id=identifier(member(server,'Id'))
    providers=member(item,'ProviderIds',{})
    if not isinstance(providers,dict):raise ValueError('Missing provider identities.')
    ids={}
    for key,value in providers.items():
        normalized=str(key).lower()
        if normalized in ids and ids[normalized]!=value:raise ValueError('Ambiguous provider identifiers.')
        ids[normalized]=value
    tmdb=ids.get('tmdb');imdb=ids.get('imdb')
    if not isinstance(tmdb,str) or not re.fullmatch(r'[1-9][0-9]{0,9}',tmdb) or int(tmdb)>2147483647:
        raise ValueError('An exact TMDB movie identity is required.')
    if imdb is not None and (not isinstance(imdb,str) or not re.fullmatch(r'tt[0-9]{5,12}',imdb)):
        raise ValueError('Invalid IMDb movie identity.')
    data=member(item,'UserData',{}) or {}
    count=member(data,'PlayCount')
    if count is not None and (type(count) is not int or not 0<=count<=1000000):
        raise ValueError('Invalid source play count.')
    last_played=timestamp(member(data,'LastPlayedDate'))
    title=member(item,'Name')
    if not isinstance(title,str) or not title.strip() or len(title)>1024:
        raise ValueError('Missing movie title.')
    session=member(payload,'Session')
    session_id=position=duration=None
    if event not in {'MarkPlayed','MarkUnplayed'}:
        session_id=identifier(member(session,'Id'))
        state=member(session,'PlayState',{}) or {}
        position=member(state,'PositionTicks')
        duration=member(item,'RunTimeTicks')
        if type(duration) is not int or not 0<duration<=604800*10000000:
            raise ValueError('Missing reliable runtime.')
        if position is None and event=='Stop':
            pass  # A stop can settle earlier armed progress even if position was cleared.
        elif type(position) is not int or not 0<=position<=duration:
            raise ValueError('Missing or invalid playback position.')
    year=member(item,'ProductionYear')
    if year is not None and (type(year) is not int or not 1888<=year<=2200):
        raise ValueError('Invalid production year.')
    return {'event':event,'server_id':server_id,'user_id':user_id,'item_id':item_id,'tmdb':tmdb,'imdb':imdb,
            'title':' '.join(title.split()),'year':year,'session_id':session_id,'position':position,'duration':duration,
            'play_count':count,'last_played':last_played}


def event_key(hint):
    return hashlib.sha256(json.dumps(hint,sort_keys=True,separators=(',',':')).encode()).hexdigest()


class JellyfinViewingVerifier:
    def __init__(self,url,api_key,metadata,transport=None):
        self.client=JellyfinRemovalVerifier(url,api_key,transport)
        self.metadata=metadata

    def setup(self):
        server=self.client.get('/System/Info/Public');users=self.client.get('/Users')
        if not isinstance(server,dict):raise AdapterError('Jellyfin server identity is unavailable.')
        identifier(server.get('Id'))
        if not isinstance(users,list):raise AdapterError('Jellyfin accounts are unavailable.')
        result=[]
        for user in users:
            if not isinstance(user,dict) or not isinstance(user.get('Policy',{}),dict):
                raise AdapterError('Jellyfin account evidence is incomplete.')
            result.append({'id':identifier(user.get('Id'),guid=True),'name':str(user.get('Name',''))[:100],
                           'administrator':user.get('Policy',{}).get('IsAdministrator') is True})
        if len({u['id'] for u in result})!=len(result):raise AdapterError('Jellyfin account identities are ambiguous.')
        return {'server_id':server['Id'],'users':result}

    def verify(self,hint):
        server=self.client.get('/System/Info/Public')
        if not isinstance(server,dict) or server.get('Id')!=hint['server_id']:
            raise AdapterError('The configured Jellyfin server identity changed.')
        raw=self.client.get('/Users/'+hint['user_id']+'/Items/'+hint['item_id'])
        if not isinstance(raw,dict):raise AdapterError('Jellyfin movie evidence is incomplete.')
        ids=raw.get('ProviderIds',{})
        if not isinstance(ids,dict):raise AdapterError('Jellyfin provider evidence is incomplete.')
        if (identifier(raw.get('Id'),guid=True)!=hint['item_id'] or raw.get('Type')!='Movie'
                or ids.get('Tmdb')!=hint['tmdb'] or hint.get('imdb') and ids.get('Imdb')!=hint['imdb']):
            raise AdapterError('Jellyfin movie identity changed.')
        if hint['event']=='MarkPlayed' and (not isinstance(raw.get('UserData'),dict) or raw['UserData'].get('Played') is not True):
            raise AdapterError('The source account no longer marks this movie played.')
        external=self.metadata.movie_external_ids(hint['tmdb'])
        if (type(external.get('id')) is not int or str(external['id'])!=hint['tmdb']
                or hint.get('imdb') and external.get('imdb_id')!=hint['imdb']):
            raise AdapterError('Jellyfin movie identities disagree with exact movie metadata.')
        return True
