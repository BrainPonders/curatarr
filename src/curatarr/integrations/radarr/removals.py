"""Narrow movie retention writer: no media deletion, acquisition or profile changes."""
import json
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError, URLError

from curatarr.adapters import AdapterError


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class RadarrRemovalWriter:
    def __init__(self, client, transport=None):
        self.client, self.transport = client, transport

    def movie(self, hint):
        matches = [m for m in self.client.movies() if str(m.get("tmdbId")) == hint["tmdb"]
                   or hint.get("imdb") and m.get("imdbId") == hint["imdb"]]
        if len(matches) > 1:
            raise AdapterError("Multiple Radarr movie identities match.")
        if not matches:
            return None
        movie = matches[0]
        if (str(movie.get("tmdbId")) != hint["tmdb"] or hint.get("imdb") and movie.get("imdbId") not in {None, "", hint["imdb"]}
                or type(movie.get("id")) is not int or type(movie.get("monitored")) is not bool):
            raise AdapterError("Radarr movie identity is inconsistent.")
        return movie

    def ensure_idle(self, movie):
        if movie is None:
            return
        count, seen, page = None, set(), 1
        while count is None or len(seen) < count:
            data = self.client._get("/api/v3/queue", {"page": str(page), "pageSize": "500", "includeUnknownMovieItems": "true"})
            if (not isinstance(data, dict) or type(data.get("totalRecords")) is not int
                    or not 0 <= data["totalRecords"] <= 50000 or not isinstance(data.get("records"), list)
                    or count is not None and count != data["totalRecords"]):
                raise AdapterError("Download queue verification is incomplete.")
            count = data["totalRecords"]
            if not data["records"] and len(seen) < count:
                raise AdapterError("Download queue verification is incomplete.")
            for row in data["records"]:
                if type(row.get("id")) is not int or row["id"] in seen:
                    raise AdapterError("Download queue verification is ambiguous.")
                seen.add(row["id"])
                if row.get("movieId") == movie["id"]:
                    raise AdapterError("A download or upgrade is active for this movie.")
            if len(seen) > count:
                raise AdapterError("Download queue verification changed.")
            page += 1

    def write(self, method, movie):
        if self.transport:
            return self.transport(method, movie)
        path = "/api/v3/movie/" + str(movie["id"])
        data = None
        if method == "DELETE":
            path += "?deleteFiles=false&addImportExclusion=false"
        elif method == "PUT":
            data = json.dumps({**movie, "monitored": True}).encode()
        else:
            raise ValueError("Unsupported retention operation.")
        request = Request(self.client.url.rstrip("/") + path, data=data, method=method,
                          headers={"X-Api-Key": self.client.api_key, "Content-Type": "application/json"})
        try:
            with build_opener(NoRedirect()).open(request, timeout=30) as response:
                if response.status not in {200, 202, 204}:
                    raise AdapterError("Radarr retention write was not confirmed.")
        except (HTTPError, URLError, OSError):
            raise AdapterError("Radarr retention outcome is uncertain; recheck before retrying.") from None
