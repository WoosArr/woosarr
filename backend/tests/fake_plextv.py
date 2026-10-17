"""A small fake of the plex.tv endpoints PlexApiGateway uses.

Mimics python-plexapi's object shapes (MyPlexUser, MyPlexServerShare, Section)
and plex.tv's replace semantics for shared_servers, so gateway logic and the
shared GatewayContract can run without a real account.
"""

import itertools
import re
import xml.etree.ElementTree as ET
from types import SimpleNamespace

import requests
from plexapi.exceptions import NotFound, Unauthorized

MACHINE = "machine-abc123"


class FakePlexTV:
    def __init__(self, libraries=None, users=None):
        # library key -> (plex.tv section id, title, type)
        self.libraries = libraries or {"1": (101, "Movies", "movie"), "2": (102, "TV", "show"),
                                       "3": (103, "Music", "artist"), "4": (104, "Movies", "movie")}
        # user id -> {"username", "email", "home", "allowSync", ...}
        self.users = users or {"11": {"username": "alice", "email": "a@x"}, "12": {"username": "bob", "email": "b@x"},
                               "13": {"username": "kid", "email": None, "home": True}}
        self.shares = {}  # user id -> {"id": share id, "sections": set(plex.tv ids)}
        self._ids = itertools.count(500)
        self.calls = []
        self.fail_next = []  # exceptions to raise on upcoming calls
        self.token_valid = True

    # --- what the gateway calls -------------------------------------------------
    def account(self):
        if not self.token_valid:
            raise Unauthorized("(401) unauthorized")
        session = SimpleNamespace(get="get", put="put", post="post", delete="delete")
        return SimpleNamespace(_session=session, query=self.query, users=self.users_list)

    def _maybe_fail(self):
        if self.fail_next:
            raise self.fail_next.pop(0)

    def users_list(self):
        self.users_calls = getattr(self, "users_calls", 0) + 1
        self._maybe_fail()
        out = []
        for uid, u in self.users.items():
            share = self.shares.get(uid)
            servers = []
            if share:
                servers.append(SimpleNamespace(id=share["id"], machineIdentifier=MACHINE, allLibraries=False,
                                               sections=lambda uid=uid: self.sections(uid)))
            out.append(SimpleNamespace(id=int(uid), username=u["username"], title=u["username"], email=u.get("email"),
                                       home=u.get("home", False), servers=servers,
                                       allowSync=u.get("allowSync", True), allowCameraUpload=False, allowChannels=True,
                                       filterMovies="", filterTelevision="", filterMusic="", filterPhotos="",
                                       filterAll=""))
        return out

    def sections(self, uid):
        if uid in getattr(self, "broken_users", set()):
            raise requests.ConnectionError(f"share read failed for {uid}")
        self._maybe_fail()
        shared = self.shares.get(uid, {}).get("sections", set())
        return [SimpleNamespace(id=sid, key=int(key), title=t, type=ty, shared=sid in shared)
                for key, (sid, t, ty) in self.libraries.items()]

    def query(self, url, method="get", json=None, headers=None, **_):
        self._maybe_fail()
        self.calls.append((method, url, json))
        if method == "get" and re.fullmatch(r"https://plex.tv/api/servers/[\w-]+", url):
            server = ET.Element("Server", machineIdentifier=MACHINE)
            for key, (sid, title, ty) in self.libraries.items():
                ET.SubElement(server, "Section", id=str(sid), key=key, title=title, type=ty)
            root = ET.Element("MediaContainer")
            root.append(server)
            return root
        m = re.fullmatch(r"https://plex.tv/api/servers/[\w-]+/shared_servers(?:/(\d+))?", url)
        if not m:
            raise NotFound(url)
        share_id = int(m.group(1)) if m.group(1) else None
        owner = next((uid for uid, s in self.shares.items() if s["id"] == share_id), None)
        if method == "post":
            uid = str(json["shared_server"]["invited_id"])
            self.shares[uid] = {"id": next(self._ids), "sections": set(json["shared_server"]["library_section_ids"])}
        elif method == "put":
            # plex.tv replaces the whole list
            self.shares[owner]["sections"] = set(json["shared_server"]["library_section_ids"])
        elif method == "delete":
            self.shares.pop(owner, None)
        return None

    def connection_error(self):
        return requests.ConnectionError("connection refused")
