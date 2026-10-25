"""Narrow native profile edit: omit monitoring, paths, tags and all search commands."""
from copy import deepcopy
import hashlib
import json
import re
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener

from curatarr.adapters import AdapterError
from curatarr.integrations.radarr.acquisition import RadarrAcquisitionWriter
from curatarr.integrations.radarr.removals import NoRedirect


class RadarrProfileWriter(RadarrAcquisitionWriter):
    @property
    def source(self):
        return hashlib.sha256(self.client.url.rstrip('/').encode()).hexdigest()

    def profiles(self):
        rows = self.client._get('/api/v3/qualityprofile', {})
        if (not isinstance(rows, list) or not rows or any(not isinstance(p, dict)
                or type(p.get('id')) is not int or p['id'] <= 0
                or not isinstance(p.get('name'), str) or not p['name'].strip() for p in rows)
                or len({p['id'] for p in rows}) != len(rows)):
            raise AdapterError('Native profiles are unavailable or ambiguous.')
        return deepcopy(rows)

    def snapshot(self, candidate):
        tmdb = candidate.get('tmdb')
        if not isinstance(tmdb, str) or not re.fullmatch('[1-9][0-9]{0,9}', tmdb):
            raise ValueError('An exact movie identity is required.')
        raw = self.movie(tmdb, candidate.get('imdb'))
        if raw is None:
            raise ValueError('This movie is no longer active in Radarr.')
        if (type(raw.get('qualityProfileId')) is not int or raw['qualityProfileId'] <= 0
                or type(raw.get('monitored')) is not bool or type(raw.get('hasFile')) is not bool
                or not isinstance(raw.get('path'), str) or not raw['path'].startswith('/')
                or not isinstance(raw.get('minimumAvailability'), str)
                or not isinstance(raw.get('tags'), list) or any(type(t) is not int for t in raw['tags'])):
            raise AdapterError('Radarr movie management facts are incomplete.')
        file = raw.get('movieFile') if isinstance(raw.get('movieFile'), dict) else {}
        file_id = raw.get('movieFileId', file.get('id', 0))
        if (type(file_id) is not int or file_id < 0 or raw['hasFile'] and
                (file_id <= 0 or file.get('id') != file_id)
                or raw['hasFile'] is False and file_id != 0):
            raise AdapterError('The movie file association is incomplete.')
        # Derived profile/file scores can legitimately change with the new profile.
        return {'source': self.source, 'id': raw['id'], 'tmdbId': raw['tmdbId'],
                'imdbId': raw.get('imdbId'), 'qualityProfileId': raw['qualityProfileId'],
                'monitored': raw['monitored'], 'path': raw['path'], 'rootFolderPath': raw.get('rootFolderPath'),
                'minimumAvailability': raw['minimumAvailability'], 'tags': sorted(raw['tags']),
                'hasFile': raw['hasFile'], 'movieFileId': file_id,
                'file': {k: deepcopy(file.get(k)) for k in ('id', 'relativePath', 'size', 'quality', 'dateAdded')} if raw['hasFile'] else None}

    def change_profile(self, candidate, before, profile):
        if self.snapshot(candidate) != before:
            raise AdapterError('The movie changed before saving. Refresh its profile.')
        fresh = next((p for p in self.profiles() if p['id'] == profile['id']), None)
        if fresh != profile:
            raise AdapterError('The selected native profile changed.')
        payload = {'movieIds': [before['id']], 'qualityProfileId': profile['id']}
        result = self.put('/api/v3/movie/editor', payload)
        if (not isinstance(result, list) or len(result) != 1 or not isinstance(result[0], dict)
                or result[0].get('id') != before['id'] or result[0].get('qualityProfileId') != profile['id']):
            raise AdapterError('Profile save outcome is uncertain; verify the native movie.')
        return {'movie_id': before['id'], 'profile_id': profile['id']}

    def put(self, path, payload):
        if self.transport:
            return self.transport(path, payload)
        request = Request(self.client.url.rstrip('/') + path, data=json.dumps(payload).encode(), method='PUT',
            headers={'X-Api-Key': self.client.api_key, 'Content-Type': 'application/json'})
        try:
            with build_opener(NoRedirect()).open(request, timeout=30) as response:
                if response.status not in {200, 202}:
                    raise AdapterError('Profile save was not confirmed.')
                return json.load(response)
        except (HTTPError, URLError, OSError, ValueError):
            raise AdapterError('Profile save outcome is uncertain; read back before retrying.') from None
