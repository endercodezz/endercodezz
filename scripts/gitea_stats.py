"""Private Gitea collector. API contracts inspected at git.ywfl.dev/swagger.v1.json.

No private metadata is returned in the public summary. All failures are redacted.
GITEA_MIRRORS is an optional secret JSON object mapping local owner/repo to
github:owner/repo or gitea:owner/repo. The canonical repository supplies full
language/release/repository statistics; mirror copies supply none of these.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter

BASE = "https://git.ywfl.dev"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # Never forward an Authorization header to another location.


class API:
    def __init__(self, token):
        self.token = token
        self.opener = urllib.request.build_opener(NoRedirect())

    def get(self, path, *, text=False, diff=False):
        req = urllib.request.Request(BASE + "/api/v1" + path, headers={
            "Authorization": "token " + self.token,
            "User-Agent": "profile-readme-generator", "Accept": "text/plain" if text else "application/json"})
        try:
            with self.opener.open(req, timeout=60) as response:
                raw = response.read()
                return raw.decode("utf-8") if text else json.loads(raw)
        except urllib.error.HTTPError as exc:
            if diff and exc.code in (404, 422):
                return None
            status = "authentication/access failed" if exc.code in (401, 403) else "API request failed"
            raise SystemExit("Gitea " + status + "; combined statistics were not written") from None
        except Exception:
            raise SystemExit("Gitea API response unavailable or invalid; combined statistics were not written") from None

    def pages(self, path, field=None):
        page = 1
        while True:
            response = self.get(path + ("&" if "?" in path else "?") + f"limit=50&page={page}")
            items = response[field] if field else response
            if not isinstance(items, list):
                raise SystemExit("Gitea pagination response invalid; nothing was written")
            if not items:
                return
            yield from items
            page += 1


def remember(value, secrets):
    """Remember all private string metadata, including nested attachment URLs."""
    if isinstance(value, str):
        if value:
            secrets.add(value)
    elif isinstance(value, dict):
        for item in value.values():
            remember(item, secrets)
    elif isinstance(value, list):
        for item in value:
            remember(item, secrets)


def origin_identity(url):
    if not url:
        return None
    url = re.sub(r"^git@([^:]+):", r"https://\1/", url)
    parsed = urllib.parse.urlsplit(url)
    provider = {"github.com": "github", "git.ywfl.dev": "gitea"}.get(parsed.hostname)
    path = parsed.path.strip("/").removesuffix(".git").lower()
    return f"{provider}:{path}" if provider and path.count("/") == 1 else None


def diff_languages(diff, files, language_of, added_bytes, secrets, stats):
    """Measure complete, ordinary unified hunks only; never estimate missing diffs.

    The instance's CommitAffectedFiles has no patch member. Match raw diff file
    headers to the JSON file list. Quoted/ambiguous paths and incomplete hunks
    are deliberately unmeasured instead of assigning bytes to a guessed file.
    """
    out = Counter()
    measured = set()
    allowed = {f["filename"] for f in files if f.get("status") not in ("removed", "deleted")}
    for f in files:
        secrets.add(f["filename"])
    for section in re.split(r"(?m)^diff --git ", diff)[1:]:
        lines = section.split("\n")
        path, hunks, complete = None, [], True
        old_left = new_left = 0
        in_hunk = False
        for line in lines[1:]:
            if line.startswith("@@ "):
                if in_hunk and (old_left or new_left):
                    complete = False
                match = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
                if not match:
                    complete = False
                    break
                old_left = int(match[2] or 1)
                new_left = int(match[4] or 1)
                in_hunk = True
                hunks.append(line)
            elif in_hunk:
                if line.startswith("\\ No newline"):
                    hunks.append(line)
                elif line and line[0] in " +-" and (old_left or new_left):
                    old_left -= line[0] in " -"
                    new_left -= line[0] in " +"
                    hunks.append(line)
                    if old_left < 0 or new_left < 0:
                        complete = False
                elif line:
                    complete = False
            elif line.startswith("+++ b/"):
                path = line[6:]
        if path in allowed and language_of(path) and complete and in_hunk and old_left == new_left == 0:
            out[language_of(path)] += added_bytes("\n".join(hunks))
            measured.add(path)
    stats["unmeasured_files"] += sum(bool(language_of(path)) and path not in measured for path in allowed)
    return out


def collect(token, github_sources, github_shas, github_own_shas, language_of, added_bytes,
            secrets, *, api=None, mirrors=None):
    if not token:
        return {"status": "missing_token", "raw": None, "included": None, "times": {},
                "own_shas": set(), "languages": Counter(), "releases": []}
    api = api or API(token)
    user = api.get("/user")
    if not isinstance(user.get("id"), int) or not user.get("login"):
        raise SystemExit("Gitea authentication response invalid; nothing was written")
    # Account metadata is private, except the login may equal the public GH login.
    secrets.update(filter(None, (user.get("email"),)))
    if mirrors is None:
        try:
            mirrors = json.loads(os.environ.get("GITEA_MIRRORS") or "{}")
        except ValueError:
            raise SystemExit("GITEA_MIRRORS must be a secret JSON object; nothing was written") from None
    if not isinstance(mirrors, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in mirrors.items()):
        raise SystemExit("GITEA_MIRRORS must map repository identities; nothing was written")
    mappings = {"gitea:" + k.lower(): v.lower() for k, v in mirrors.items()}
    records = {}
    # Unlike /user/repos (documented as owned repos), search includes accessible
    # private repositories belonging to other users and organisations.
    for repo in api.pages("/repos/search?private=true&is_private=true&sort=id&order=asc", "data"):
        if not repo.get("private"):
            continue
        key = "gitea:" + repo["full_name"].lower()
        if key in records:
            continue
        # Whitelist metadata fields so ordinary values such as 'main', language
        # names and booleans cannot become false-positive privacy needles.
        for field in ("name", "full_name", "description", "html_url", "clone_url", "ssh_url", "url", "original_url"):
            remember(repo.get(field), secrets)
        if repo["owner"]["login"].lower() != user["login"].lower():
            secrets.add(repo["owner"]["login"])
        origin = origin_identity(repo.get("original_url"))
        if origin and origin != key:
            mappings.setdefault(key, origin)
        elif repo.get("mirror") and key not in mappings:
            raise SystemExit("Gitea mirror identity unresolved; configure GITEA_MIRRORS; nothing was written")
        prefix = "/repos/" + "/".join(urllib.parse.quote(part, safe="") for part in repo["full_name"].split("/"))
        if not origin and key not in mappings and (repo.get("permissions") or {}).get("admin"):
            destinations = set()
            for mirror in api.pages(prefix + "/push_mirrors"):
                remember(mirror.get("remote_address"), secrets)
                destination = origin_identity(mirror.get("remote_address"))
                if destination:
                    destinations.add(destination)
            if len(destinations) > 1:
                raise SystemExit("Gitea mirror has multiple canonical candidates; configure GITEA_MIRRORS; nothing was written")
            if destinations:
                mappings[key] = destinations.pop()
        commits = {}
        if not repo.get("empty"):
            branch = repo.get("default_branch")
            if not branch:
                raise SystemExit("Gitea default branch unavailable; nothing was written")
            for c in api.pages(prefix + "/commits?" + urllib.parse.urlencode({
                    "sha": branch, "stat": "false", "verification": "false", "files": "false"})):
                secrets.add(c["sha"])
                commits[c["sha"]] = c
        own = repo["owner"]["id"] == user["id"] and not repo.get("fork")
        languages = Counter(api.get(prefix + "/languages")) if own and not repo.get("empty") else Counter()
        releases = []
        if repo.get("has_releases", True):
            for release in api.pages(prefix + "/releases"):
                for field in ("name", "body", "html_url", "url", "tarball_url", "zipball_url"):
                    remember(release.get(field), secrets)
                for asset in release.get("assets") or []:
                    for field in ("name", "browser_download_url", "uuid"):
                        remember(asset.get(field), secrets)
                if not release.get("draft") and release.get("published_at"):
                    releases.append({"repo": "PRIVATE", "tag": None,
                        "at": dt.datetime.fromisoformat(release["published_at"].replace("Z", "+00:00")),
                        "downloads": sum(a.get("download_count", 0) for a in release.get("assets") or [])})
        records[key] = {"repo": repo, "prefix": prefix, "commits": commits, "own": own,
                        "languages": languages, "releases": releases}
    known = set(records) | {"github:" + k.lower() for k in github_sources}
    if set(mappings) - set(records):
        raise SystemExit("Gitea mirror mapping references an inaccessible repository; nothing was written")
    def canonical(key, trail=()):
        if key in trail or key not in known:
            raise SystemExit("Gitea mirror mapping is cyclic or its canonical repository is unavailable; nothing was written")
        return canonical(mappings[key], trail + (key,)) if key in mappings else key
    included_keys = {key for key in records if canonical(key) == key}
    # Shared history alone cannot tell a fork from a mirror. Demand an explicit
    # repository identity before publishing full sizes/releases twice.
    for key in included_keys:
        if set(records[key]["commits"]) & github_shas:
            raise SystemExit("Gitea/GitHub shared history requires GITEA_MIRRORS; nothing was written")
    for key in included_keys:
        record = records[key]
        if record["own"] and any(other != key and records[other]["own"] and
                set(record["commits"]) & set(records[other]["commits"]) for other in included_keys):
            raise SystemExit("Gitea shared repository history requires GITEA_MIRRORS; nothing was written")
    measured = {}
    diagnostics = Counter()
    def aggregate(keys, excluded_shas=()):
        summary = Counter(repositories=len(keys))
        languages, times, all_own = Counter(), {}, set(excluded_shas)
        releases, foreign = [], {}
        for key in sorted(keys):
            record = records[key]
            repo = record["repo"]
            languages.update(record["languages"])
            releases += record["releases"]
            summary["total_repository_commits"] += len(record["commits"])
            summary["stars"] += repo.get("stars_count", 0)
            summary["forks"] += repo.get("forks_count", 0)
            for sha, c in record["commits"].items():
                if (c.get("author") or {}).get("id") != user["id"]:
                    continue
                when = dt.datetime.fromisoformat(c["commit"]["author"]["date"].replace("Z", "+00:00"))
                times.setdefault(sha, when)
                if record["own"]:
                    all_own.add(sha)
                else:
                    foreign.setdefault(sha, record)
        for sha, record in foreign.items():
            if sha in all_own:
                continue
            if sha not in measured:
                c = api.get(record["prefix"] + "/git/commits/" + urllib.parse.quote(sha, safe=""))
                if (c.get("author") or {}).get("id") != user["id"]:
                    raise SystemExit("Gitea commit attribution changed; nothing was written")
                if len(c.get("parents") or []) > 1:
                    diagnostics["merges_skipped"] += 1
                    measured[sha] = Counter()
                else:
                    patch = api.get(record["prefix"] + "/git/commits/" + urllib.parse.quote(sha, safe="") + ".diff", text=True, diff=True)
                    if patch is None:
                        diagnostics["unavailable_diffs"] += 1
                        measured[sha] = Counter()
                    else:
                        measured[sha] = diff_languages(patch, c.get("files") or [], language_of, added_bytes, secrets, diagnostics)
            languages.update(measured[sha])
        summary["personal_commits"] = len(times)
        summary["external_personal_commits"] = len(foreign)
        summary["releases"] = len(releases)
        summary["downloads"] = sum(r["downloads"] for r in releases)
        activity = dict(sorted(Counter(t.date().isoformat() for t in times.values()).items()))
        return {**summary, "language_bytes": dict(languages), "activity": activity}, times, all_own - set(excluded_shas), languages, releases
    raw, _, _, _, _ = aggregate(set(records))
    included, times, own_shas, languages, releases = aggregate(included_keys, github_own_shas | github_shas)
    return {"status": "authenticated", "raw": raw, "included": included, "times": times,
            "own_shas": own_shas, "languages": languages, "releases": releases,
            "mirrors_excluded": len(records) - len(included_keys), "diagnostics": dict(diagnostics)}
