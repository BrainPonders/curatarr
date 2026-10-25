"""Native movie add/search boundary, with fresh profile/root and exact-ID checks."""
import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener

from curatarr.adapters import AdapterError
from curatarr.integrations.radarr.removals import NoRedirect


class RadarrAcquisitionWriter:
    def __init__(self, client, transport=None):
        self.client, self.transport = client, transport

    def options(self):
        profiles = self.client._get('/api/v3/qualityprofile', {})
        roots = self.client._get('/api/v3/rootfolder', {})
        if (not isinstance(profiles, list) or not isinstance(roots, list)
                or any(type(p.get('id')) is not int or p['id'] <= 0 or not isinstance(p.get('name'), str) or not p['name'] for p in profiles)
                or any(type(r.get('id')) is not int or r['id'] <= 0 or not isinstance(r.get('path'), str) or not r['path'].startswith('/') for r in roots)
                or len({p['id'] for p in profiles}) != len(profiles) or len({r['id'] for r in roots}) != len(roots)):
            raise AdapterError('Native acquisition choices are incomplete.')
        return {'profiles': profiles, 'roots': roots}

    def movie(self, tmdb, imdb=None):
        matches = [m for m in self.client.movies() if str(m.get('tmdbId')) == tmdb or imdb and m.get('imdbId') == imdb]
        if len(matches) > 1:
            raise AdapterError('Radarr has ambiguous movie identities.')
        if not matches:
            return None
        movie = matches[0]
        if (str(movie.get('tmdbId')) != tmdb or imdb and movie.get('imdbId') not in {None, '', imdb}
                or type(movie.get('id')) is not int or movie['id'] <= 0):
            raise AdapterError('Radarr movie identifiers conflict.')
        return movie

    def selection(self, profile_id, root_id):
        options = self.options()
        profiles = [p for p in options['profiles'] if p['id'] == profile_id and type(profile_id) is int]
        roots = [r for r in options['roots'] if r['id'] == root_id and type(root_id) is int]
        if len(profiles) != 1 or len(roots) != 1 or roots[0].get('accessible') is False:
            raise AdapterError('Choose an available native profile and root folder.')
        return profiles[0], roots[0]

    def excluded(self, tmdb):
        rows = self.client._get('/api/v3/exclusions', {})
        if not isinstance(rows, list) or any(not isinstance(r, dict) or type(r.get('tmdbId')) is not int for r in rows):
            raise AdapterError('Native acquisition exclusions are unavailable.')
        return any(str(r['tmdbId']) == tmdb for r in rows)

    def add(self, tmdb, imdb, profile, root):
        # Re-check the exact identities/options at the mutation boundary. Do not
        # send the caller's arbitrary movie body or root path to Radarr.
        if self.movie(tmdb, imdb) is not None or self.excluded(tmdb):
            raise AdapterError('Movie already active or excluded; refresh before adding.')
        fresh_profile,fresh_root=self.selection(profile['id'],root['id'])
        if fresh_profile!=profile or fresh_root['path']!=root['path']:
            raise AdapterError('The confirmed acquisition choices changed.')
        rows = self.client._get('/api/v3/movie/lookup', {'term': 'tmdb:' + tmdb})
        matches = [m for m in rows if str(m.get('tmdbId')) == tmdb] if isinstance(rows, list) else []
        if len(matches) != 1 or imdb and matches[0].get('imdbId') not in {None, '', imdb}:
            raise AdapterError('Radarr provider lookup did not resolve the exact movie.')
        metadata = matches[0]
        if not isinstance(metadata.get('title'), str) or not metadata.get('titleSlug'):
            raise AdapterError('Native movie metadata is incomplete.')
        result = self.post('/api/v3/movie', {'tmdbId': int(tmdb), 'title': metadata['title'],
                           'titleSlug': metadata['titleSlug'], 'year': metadata.get('year'), 'images': metadata.get('images', []),
                           'qualityProfileId': profile['id'], 'rootFolderPath': root['path'], 'monitored': True,
                           'minimumAvailability': 'released', 'addOptions': {'searchForMovie': False}})
        if not isinstance(result, dict) or str(result.get('tmdbId')) != tmdb or type(result.get('id')) is not int:
            raise AdapterError('Native movie add outcome is uncertain.')
        return {'movie_id': result['id']}

    def search(self, movie):
        result = self.post('/api/v3/command', {'name': 'MoviesSearch', 'movieIds': [movie['id']]})
        if not isinstance(result, dict) or type(result.get('id')) is not int:
            raise AdapterError('Search command outcome is uncertain.')
        return {'command_id': result['id']}

    def command(self, identifier, movie_id):
        if type(identifier) is not int or identifier <= 0:
            raise AdapterError('Invalid search command identity.')
        command = self.client._get('/api/v3/command/' + str(identifier), {})
        body = command.get('body') if isinstance(command, dict) else None
        if (not isinstance(command, dict) or command.get('id') != identifier or command.get('name') != 'MoviesSearch'
                or not isinstance(body, dict) or body.get('movieIds') != [movie_id]
                or command.get('status') not in {'queued', 'started', 'completed'}):
            raise AdapterError('The search command is unavailable, failed, or belongs to another movie.')
        return {'command_id': identifier, 'status': command['status']}

    def post(self, path, payload):
        if self.transport:
            return self.transport(path, payload)
        request = Request(self.client.url.rstrip('/') + path, data=json.dumps(payload).encode(), method='POST',
                          headers={'X-Api-Key': self.client.api_key, 'Content-Type': 'application/json'})
        try:
            with build_opener(NoRedirect()).open(request, timeout=30) as response:
                if response.status not in {200, 201, 202}:
                    raise AdapterError('Native acquisition write was not confirmed.')
                return json.load(response)
        except (HTTPError, URLError, OSError, ValueError):
            raise AdapterError('Native acquisition outcome is uncertain; read back before retrying.') from None
