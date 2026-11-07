#!/usr/bin/env python3
"""Render the profile README panels from live GitHub API data.

Stdlib only. Needs a token in GH_TOKEN (the workflow passes GITHUB_TOKEN, or an
optional STATS_TOKEN secret). Every number in the output comes from the API;
the only static text lives in scripts/profile.json.

Every panel is written twice, NAME-light.svg and NAME-dark.svg, in GitHub's
Primer colours; the README swaps them with <picture> + prefers-color-scheme.

Private repositories
--------------------
* Contribution totals come from GitHub's own calendar. Private activity is in it
  only when "Private contributions" is enabled on the profile, and then only as
  an anonymous count (restrictedContributionsCount).
* If the token belongs to the profile owner (STATS_TOKEN secret), private repos
  the token can read are folded in as aggregates: language bytes, commit times,
  releases. Anywhere a project would be named they become one PRIVATE bucket.
* Private names, descriptions and URLs are kept in memory only. Before anything
  is written, every output is scanned for them and the run aborts on a match.
  Errors from private queries are reported without details (Actions logs are
  public).
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import re
import sys
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo
import gitea_stats

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
USER = os.environ.get("GH_USER", "endercodezz")
TOKEN = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
GITEA_TOKEN = os.environ.get("GITEA_TOKEN")
TZ = ZoneInfo(os.environ.get("PROFILE_TZ") or "UTC")
NOW = dt.datetime.now(dt.timezone.utc)
# Shown next to every hour of day: the rhythm is computed in this zone.
TZ_LABEL = "UTC" if str(TZ) in ("UTC", "Etc/UTC") else str(TZ)
CONFIG = json.loads((Path(__file__).resolve().parent / "profile.json").read_text("utf-8"))
PRIVATE = "PRIVATE"
W = 840

# ---------------------------------------------------------------- design tokens
# GitHub Primer, light and dark default. Project colours are Primer accents,
# ordered so neighbours stay apart under colour-vision deficiency.
THEMES = {
    "dark": {
        "card": "#161b22", "canvas": "#0d1117", "border": "#30363d", "muted_border": "#21262d",
        "text": "#e6edf3", "muted": "#8b949e", "subtle": "#6e7681",
        "blue": "#58a6ff", "green": "#3fb950", "green_em": "#238636",
        "track": "#21262d",
        "heat": ["#21262d", "#0e4429", "#006d32", "#26a641", "#39d353"],
        "projects": ["#58a6ff", "#d29922", "#bc8cff", "#3fb950", "#f778ba", "#f0883e"],
        "private": "#6e7681", "other": "#6e7681",
    },
    "light": {
        "card": "#f6f8fa", "canvas": "#ffffff", "border": "#d0d7de", "muted_border": "#d8dee4",
        "text": "#1f2328", "muted": "#656d76", "subtle": "#6e7781",
        "blue": "#0969da", "green": "#1a7f37", "green_em": "#1f883d",
        "track": "#e6eaef",
        "heat": ["#ebedf0", "#9be9a8", "#40c463", "#30a14e", "#216e39"],
        "projects": ["#0969da", "#9a6700", "#8250df", "#1a7f37", "#bf3989", "#bc4c00"],
        "private": "#8c959f", "other": "#8c959f",
    },
}
T = THEMES["dark"]          # current theme, switched by render_all()


# ---------------------------------------------------------------- GitHub API
SKIPPABLE = {"NOT_FOUND", "FORBIDDEN"}   # deleted, renamed away, or access revoked


def gql(query: str, private: bool = False, missing_ok: bool = False, **variables):
    """GraphQL call. With missing_ok, NOT_FOUND / FORBIDDEN errors (a repository that
    no longer exists or is no longer readable) return the partial data instead of
    aborting; the caller sees `repository: None` and skips it."""
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=json.dumps({"query": query, "variables": variables}).encode(),
        headers={"Authorization": f"bearer {TOKEN}", "User-Agent": "profile-readme-generator"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            payload = json.load(r)
    except Exception as e:
        raise SystemExit("GitHub API request failed" + ("" if private else f": {e}"))
    errors = payload.get("errors")
    if errors and not (missing_ok and all(e.get("type") in SKIPPABLE for e in errors)):
        raise SystemExit("GraphQL error" + (" in a private-repo query" if private else f": {errors}"))
    return payload.get("data") or {}


def rest(path: str, private: bool = False):
    """REST GET. Returns None for repositories/commits that are gone or unreadable
    (404, 409 empty repo, 410, 422, 451, or a 403 that is not a rate limit)."""
    req = urllib.request.Request(
        f"https://api.github.com{path}",
        headers={"Authorization": f"bearer {TOKEN}", "User-Agent": "profile-readme-generator",
                 "Accept": "application/vnd.github+json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        limited = e.code in (403, 429) and e.headers.get("X-RateLimit-Remaining") == "0"
        if e.code in (403, 404, 409, 410, 422, 451) and not limited:
            return None
        raise SystemExit("GitHub REST request failed" + ("" if private else f": {e.code} {path}"))
    except Exception as e:
        raise SystemExit("GitHub REST request failed" + ("" if private else f": {e}"))


PROFILE_Q = """
query($login: String!) {
  user(login: $login) {
    id
    contributionsCollection {
      totalCommitContributions totalIssueContributions totalPullRequestContributions
      totalPullRequestReviewContributions restrictedContributionsCount
      contributionCalendar { totalContributions weeks { contributionDays { date contributionCount weekday } } }
    }
    repositories(first: 60, ownerAffiliations: OWNER, privacy: PUBLIC, orderBy: {field: PUSHED_AT, direction: DESC}) {
      nodes {
        name isFork stargazerCount forkCount pushedAt createdAt description
        primaryLanguage { name color }
        languages(first: 20, orderBy: {field: SIZE, direction: DESC}) { edges { size node { name color } } }
        releases(first: 50, orderBy: {field: CREATED_AT, direction: DESC}) {
          totalCount
          nodes { tagName publishedAt isDraft releaseAssets(first: 25) { nodes { downloadCount } } }
        }
      }
    }
  }
}"""

VIEWER_Q = "query { viewer { login } }"

PRIVATE_Q = """
query($after: String) {
  viewer {
    repositories(first: 40, after: $after, privacy: PRIVATE,
                 affiliations: [OWNER, COLLABORATOR, ORGANIZATION_MEMBER],
                 ownerAffiliations: [OWNER, COLLABORATOR, ORGANIZATION_MEMBER]) {
      pageInfo { hasNextPage endCursor }
      nodes {
        name description url isFork owner { login }
        languages(first: 20, orderBy: {field: SIZE, direction: DESC}) { edges { size node { name color } } }
        releases(first: 50, orderBy: {field: CREATED_AT, direction: DESC}) {
          nodes { publishedAt isDraft releaseAssets(first: 25) { nodes { downloadCount } } }
        }
      }
    }
  }
}"""

HISTORY_Q = """
query($owner: String!, $name: String!, $author: ID!, $after: String) {
  repository(owner: $owner, name: $name) {
    defaultBranchRef { target { ... on Commit {
      allCommits: history(first: 1) { totalCount }
      history(first: 100, after: $after, author: {id: $author}) {
        totalCount pageInfo { hasNextPage endCursor }
        nodes { oid authoredDate author { user { id } } }
      }
    } } }
  }
}"""

# Repositories owned by someone else (users or organisations) that I committed to.
# Two sources, because each misses some: the contribution graph (last year, max
# 100 repos, no pagination) and repositoriesContributedTo (paginated).
EXTERNAL_Q = """
query($login: String!, $after: String) {
  user(login: $login) {
    repositoriesContributedTo(first: 100, after: $after, includeUserRepositories: false,
                              contributionTypes: [COMMIT, PULL_REQUEST]) {
      pageInfo { hasNextPage endCursor }
      nodes { name owner { login } isPrivate isFork description url }
    }
    contributionsCollection {
      commitContributionsByRepository(maxRepositories: 100) {
        repository { name owner { login } isPrivate isFork description url }
      }
    }
  }
}"""

ALLTIME_Q = """
query($owner: String!, $name: String!, $author: ID!) {
  repository(owner: $owner, name: $name) {
    defaultBranchRef { target { ... on Commit {
      yours: history(first: 1, author: {id: $author}) { totalCount }
      total: history(first: 1) { totalCount }
    } } }
  }
}"""


def my_commits_in(repo: str, author: str, owner: str = USER,
                  private: bool = False, totals: dict | None = None) -> list[tuple[str, dt.datetime]]:
    """(sha, authored date) of every commit I authored on the default branch.
    Paginated; a repository that is gone, unreadable or empty yields []."""
    # The filter is discovery, not permission to attribute every returned diff.
    # Require the primary git author to resolve to this GitHub account; a
    # co-author trailer or a matching display name is insufficient.
    out, after = [], None
    while True:
        d = gql(HISTORY_Q, private=private, missing_ok=True, owner=owner, name=repo, author=author, after=after)
        ref = (d.get("repository") or {}).get("defaultBranchRef")
        if not ref:
            if totals is not None and d.get("repository"):
                totals[f"{owner}/{repo}".lower()] = 0
            return out
        if totals is not None:
            totals[f"{owner}/{repo}".lower()] = ref["target"]["allCommits"]["totalCount"]
        h = ref["target"]["history"]
        out += [(n["oid"], dt.datetime.fromisoformat(n["authoredDate"].replace("Z", "+00:00")))
                for n in h["nodes"]
                if ((n.get("author") or {}).get("user") or {}).get("id") == author]
        if not h["pageInfo"]["hasNextPage"]:
            return out
        after = h["pageInfo"]["endCursor"]


def external_repos() -> list[dict]:
    """Other people's / organisations' repos I contributed to, de-duplicated.

    Only sources tied to actions of my account are used. Commit search is not:
    it matches the author e-mail written into a commit, which anyone can forge
    (e.g. bots that push empty commits "authored" by other users)."""
    found, after = {}, None
    while True:
        # Errors can contain private repository metadata, even before discovery.
        u = gql(EXTERNAL_Q, private=True, missing_ok=True, login=USER, after=after).get("user") or {}
        nodes = list((u.get("repositoriesContributedTo") or {}).get("nodes") or [])
        if after is None:
            nodes += [c["repository"] for c in
                      (u.get("contributionsCollection") or {}).get("commitContributionsByRepository") or []]
        for r in nodes:
            if r and r["owner"]["login"].lower() != USER.lower():
                found.setdefault(f"{r['owner']['login']}/{r['name']}".lower(), r)
        page = (u.get("repositoriesContributedTo") or {}).get("pageInfo") or {}
        if not page.get("hasNextPage"):
            return list(found.values())
        after = page["endCursor"]


# ---------------------------------------------------------------- my code in other repos
# File extension -> Linguist language. Prose, data and config files are not
# programming languages and Linguist leaves them out of language stats too.
EXT_LANG = {
    ".c": "C", ".h": "C", ".cpp": "C++", ".cc": "C++", ".cxx": "C++", ".hpp": "C++", ".hh": "C++",
    ".hxx": "C++", ".inl": "C++", ".cs": "C#", ".py": "Python", ".pyw": "Python", ".ts": "TypeScript",
    ".tsx": "TypeScript", ".mts": "TypeScript", ".js": "JavaScript", ".jsx": "JavaScript", ".mjs": "JavaScript",
    ".cjs": "JavaScript", ".rs": "Rust", ".zig": "Zig", ".go": "Go", ".java": "Java", ".kt": "Kotlin",
    ".lua": "Lua", ".sh": "Shell", ".bash": "Shell", ".ps1": "PowerShell", ".psm1": "PowerShell",
    ".bat": "Batchfile", ".cmd": "Batchfile", ".cmake": "CMake", ".html": "HTML", ".htm": "HTML",
    ".css": "CSS", ".scss": "SCSS", ".glsl": "GLSL", ".vert": "GLSL", ".frag": "GLSL", ".m": "Objective-C",
    ".mm": "Objective-C++", ".swift": "Swift", ".nix": "Nix", ".rb": "Ruby", ".php": "PHP", ".dart": "Dart",
    ".gd": "GDScript", ".vue": "Vue", ".svelte": "Svelte", ".asm": "Assembly", ".s": "Assembly",
}
NAME_LANG = {"cmakelists.txt": "CMake", "dockerfile": "Dockerfile", "makefile": "Makefile"}
# Linguist colours for languages that may only show up through contributions.
LANG_COLOR = {
    "C": "#555555", "C++": "#f34b7d", "C#": "#178600", "Python": "#3572A5", "TypeScript": "#3178c6",
    "JavaScript": "#f1e05a", "Rust": "#dea584", "Zig": "#ec915c", "Go": "#00ADD8", "Java": "#b07219",
    "Kotlin": "#A97BFF", "Lua": "#000080", "Shell": "#89e051", "PowerShell": "#012456", "Batchfile": "#C1F12E",
    "CMake": "#DA3434", "HTML": "#e34c26", "CSS": "#663399", "SCSS": "#c6538c", "GLSL": "#5686a5",
    "Objective-C": "#438eff", "Objective-C++": "#6866fb", "Swift": "#F05138", "Nix": "#7e7eff",
    "Ruby": "#701516", "PHP": "#4F5D95", "Dart": "#00B4AB", "GDScript": "#355570", "Vue": "#41b883",
    "Svelte": "#ff3e00", "Assembly": "#6E4C13", "Dockerfile": "#384d54", "Makefile": "#427819",
}
VENDOR = re.compile(r"(^|/)(vendor|third[_-]?party|node_modules|external|deps|dist|build)/", re.I)


def language_of(path: str) -> str | None:
    if VENDOR.search(path):
        return None                      # Linguist treats vendored code as not mine either
    name = path.rsplit("/", 1)[-1].lower()
    if name in NAME_LANG:
        return NAME_LANG[name]
    dot = name.rfind(".")
    return EXT_LANG.get(name[dot:]) if dot > 0 else None


def added_bytes(patch: str) -> int:
    """Bytes of the lines a diff adds (what I wrote), not the whole file."""
    total, in_hunk, added = 0, False, False
    # Git patches delimit lines with LF. str.splitlines() also splits control
    # characters and Unicode separators inside source, dropping added bytes.
    for line in patch.split("\n"):
        if line.startswith("@@"):
            in_hunk = True
        if line.startswith("\\ No newline at end of file"):
            if added:
                total -= 1
            added = False
            continue
        # A +++ file header is metadata only before the first hunk. Inside a
        # hunk, ++ at the start of source code is an ordinary added line.
        added = line.startswith("+") and (in_hunk or not line.startswith("+++ "))
        if added:
            total += len(line[1:].encode()) + 1
    return total


def commit_languages(owner: str, name: str, sha: str, private: bool, stats: Counter,
                     secrets: set[str] | None = None) -> Counter:
    """Language -> bytes I added in one commit. Paginates the file list. Files
    without a textual patch (binary, or too large for the API) are counted in
    stats['unmeasured'] and contribute nothing rather than an estimate."""
    out = Counter()
    for page in range(1, 31):               # the API serves at most 3000 files
        c = rest(f"/repos/{owner}/{name}/commits/{sha}?per_page=100&page={page}", private=private)
        if c is None:
            stats["unavailable"] += 1
            return out
        if page == 1 and len(c.get("parents") or []) > 1:
            # A merge's diff is the merged branch: code already counted in its own
            # commits, or written by someone else. Still a commit, but not my bytes.
            stats["merges"] += 1
            return out
        files = c.get("files") or []
        for f in files:
            if private and secrets is not None:
                secrets.update(filter(None, (f["filename"], f.get("previous_filename"))))
            lang = language_of(f["filename"])
            if not lang or f.get("status") == "removed":
                continue
            if f.get("patch") is None:
                stats["unmeasured"] += 1
                continue
            out[lang] += added_bytes(f["patch"])
        if len(files) < 100:
            return out
    return out


def repo_commit_counts(repo: str, author: str) -> tuple[int, int]:
    """Personal and all-author counts on the same default-branch tip, in one call."""
    d = gql(ALLTIME_Q, owner=USER, name=repo, author=author)
    ref = (d["repository"] or {}).get("defaultBranchRef")
    if not ref:
        return 0, 0
    counts = ref["target"]
    return counts["yours"]["totalCount"], counts["total"]["totalCount"]


def my_commit_count(repo: str, author: str) -> int:
    return repo_commit_counts(repo, author)[0]


# ---------------------------------------------------------------- data
def collect():
    d = gql(PROFILE_Q, login=USER)["user"]
    author = d["id"]
    repos = [r for r in d["repositories"]["nodes"] if r["name"].lower() != USER.lower()]
    since = NOW - dt.timedelta(days=365)

    # Every repository I may have committed to, keyed "owner/name" so a repo reached
    # through several sources is read once. kind "own" = my non-fork repo (its
    # languages come from Linguist); "foreign" = my fork or someone else's repo
    # (only the code I added there counts).
    sources: dict[str, tuple[str, str, bool, str]] = {}
    public_metadata = {}
    for r in repos:
        sources[f"{USER}/{r['name']}".lower()] = (USER, r["name"], False, "foreign" if r["isFork"] else "own")
        public_metadata[f"{USER}/{r['name']}".lower()] = set(filter(None, (
            r["name"], r.get("description"), f"{USER}/{r['name']}", f"https://github.com/{USER}/{r['name']}")))
    # This profile repo is left out of the project lists, but my commits to it are
    # real commits (the bot's are filtered out by the author filter).
    sources.setdefault(f"{USER}/{USER}".lower(), (USER, USER, False, "own"))

    tracked = {p["repo"] for p in CONFIG["featured"] + CONFIG["more"]}
    commit_counts = {r["name"]: repo_commit_counts(r["name"], author) for r in repos if r["name"] in tracked}
    my_commits = {name: counts[0] for name, counts in commit_counts.items()}
    total_commits = {name: counts[1] for name, counts in commit_counts.items()}

    def releases_of(r, label):
        out = []
        for rel in r["releases"]["nodes"]:
            if rel["isDraft"] or not rel["publishedAt"]:
                continue
            out.append({
                "repo": label, "tag": rel.get("tagName"),
                "at": dt.datetime.fromisoformat(rel["publishedAt"].replace("Z", "+00:00")),
                "downloads": sum(a["downloadCount"] for a in rel["releaseAssets"]["nodes"]),
            })
        return out

    releases = [x for r in repos for x in releases_of(r, r["name"])]

    # Private repos: only with a token that belongs to the profile owner.
    private_langs, secrets = [], set()
    has_private = gql(VIEWER_Q)["viewer"]["login"].lower() == USER.lower() if TOKEN else False
    if has_private:
        after = None
        while True:
            page = gql(PRIVATE_Q, private=True, after=after)["viewer"]["repositories"]
            for r in page["nodes"]:
                secrets.update(filter(None, (r["name"], r["description"], r["url"],
                                             f"{r['owner']['login']}/{r['name']}")))
                own = r["owner"]["login"].lower() == USER.lower()
                if not own:
                    secrets.add(r["owner"]["login"])
                # languages and releases of my own repos: same rule as public ones
                if own and not r["isFork"]:
                    private_langs.append(r["languages"]["edges"])
                    releases += releases_of(r, PRIVATE)
                sources.setdefault(f"{r['owner']['login']}/{r['name']}".lower(),
                                   (r["owner"]["login"], r["name"], True, "own" if own and not r["isFork"] else "foreign"))
            if not page["pageInfo"]["hasNextPage"]:
                break
            after = page["pageInfo"]["endCursor"]

    # Other people's and organisations' repos, public and (with an owner token) private.
    for r in external_repos():
        if r["isPrivate"]:
            secrets.update(filter(None, (r["name"], r["description"], r["url"], r["owner"]["login"],
                                         f"{r['owner']['login']}/{r['name']}")))
        sources.setdefault(f"{r['owner']['login']}/{r['name']}".lower(),
                           (r["owner"]["login"], r["name"], r["isPrivate"], "foreign"))
        if not r["isPrivate"]:
            public_metadata[f"{r['owner']['login']}/{r['name']}".lower()] = set(filter(None, (
                r["name"], r.get("description"), r.get("url"), f"{r['owner']['login']}/{r['name']}")))
    # Repos known to forge my authorship (profile.json): never read, whatever source lists them.
    for key in (k.lower() for k in CONFIG.get("exclude_repos", [])):
        sources.pop(key, None)

    # Commits I authored, each SHA once wherever it appears (fork and upstream,
    # mirrors, a repo reached twice). Commits by others are filtered out by the
    # API's author filter.
    commits: dict[str, dt.datetime] = {}
    own_shas: set[str] = set()
    foreign: dict[str, tuple[str, str, bool]] = {}
    repository_totals = {}
    for owner, name, private, kind in sources.values():
        for sha, when in my_commits_in(name, author, owner=owner, private=private, totals=repository_totals):
            if private:
                secrets.add(sha)
            commits.setdefault(sha, when)
            if kind == "own":
                own_shas.add(sha)
            else:
                foreign.setdefault(sha, (owner, name, private))
    try:
        gitea = gitea_stats.collect(GITEA_TOKEN, sources, set(commits), own_shas,
                                  language_of, added_bytes, secrets, github_public=public_metadata)
    except Exception:
        raise SystemExit("Gitea collection failed; private diagnostics suppressed; nothing was written") from None
    if gitea["status"] == "authenticated":
        print(f"Gitea: authenticated; {gitea['raw']['repositories']} private repositories, "
              f"{gitea['raw']['personal_commits']} personal commits, "
              f"{gitea['included']['repositories']} canonical repositories included, "
              f"{gitea['mirrors_excluded']} mirrors excluded; "
              f"{sum(gitea['languages'].values())} language bytes included")
    else:
        print("Gitea: GITEA_TOKEN missing; publishing explicitly GitHub-only statistics")
    times = [t for t in {**gitea["times"], **commits}.values() if t >= since]

    # Languages of my code in forks and other people's repos: bytes my commits add.
    # A commit that also lives in one of my own repos is already in Linguist's numbers.
    contrib_langs, lang_stats = Counter(), Counter()
    for sha, (owner, name, private) in foreign.items():
        if sha in own_shas or sha in gitea["own_shas"]:
            continue
        lang_stats["commits"] += 1
        contrib_langs.update(commit_languages(owner, name, sha, private, lang_stats, secrets))
    print(f"my code in forks and other repos: {lang_stats['commits']} commits, "
          f"{sum(contrib_langs.values())} bytes added, {lang_stats['unmeasured']} files without a diff, "
          f"{lang_stats['merges']} merges skipped, {lang_stats['unavailable']} commits unavailable")
    github_languages = Counter(contrib_langs)
    for edges in [r["languages"]["edges"] for r in repos if not r["isFork"]] + private_langs:
        github_languages.update({e["node"]["name"]: e["size"] for e in edges})
    providers = {"github": {"repositories": len(repository_totals), "personal_commits": len(commits),
        "total_repository_commits": sum(repository_totals.values()), "language_bytes": dict(github_languages),
        "releases": len(releases), "downloads": sum(r["downloads"] for r in releases)},
        "gitea": {key: gitea[key] for key in ("status", "raw", "included")}}
    # Keep GitHub's calendar unchanged: Gitea activity has its own dated counters,
    # while the commit rhythm, language shares and shipping totals combine both.
    providers["gitea"]["activity"] = dict(sorted(Counter(t.date().isoformat() for t in gitea["times"].values()).items()))
    providers["gitea"]["mirrors_excluded"] = gitea.get("mirrors_excluded", 0)
    providers["gitea"]["diagnostics"] = gitea.get("diagnostics", {})
    contrib_langs.update(gitea["languages"])
    releases += gitea["releases"]
    return {"cc": d["contributionsCollection"], "repos": repos, "times": times,
            "my_commits": my_commits, "total_commits": total_commits,
            "releases": releases, "private_langs": private_langs,
            "contrib_langs": contrib_langs, "has_private": has_private, "secrets": secrets,
            "providers": providers}


# ---------------------------------------------------------------- svg helpers
SANS = "-apple-system,BlinkMacSystemFont,'Segoe UI','Noto Sans',Helvetica,Arial,sans-serif"
MONO = "ui-monospace,SFMono-Regular,'SF Mono',Menlo,Consolas,'Liberation Mono',monospace"


def svg(w: int, h: int, body: str, title: str) -> str:
    css = (f".f{{font-family:{SANS}}}.mono{{font-family:{MONO}}}"
           f".t{{fill:{T['text']}}}.mu{{fill:{T['muted']}}}.su{{fill:{T['subtle']}}}"
           f".bl{{fill:{T['blue']}}}.gr{{fill:{T['green']}}}.num{{font-variant-numeric:tabular-nums}}")
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
            f'role="img" aria-label="{escape(title)}">\n<title>{escape(title)}</title>\n'
            f'<style>{css}</style>\n{body}\n</svg>\n')


def box(w: int, h: int) -> str:
    return (f'<rect x=".5" y=".5" width="{w - 1}" height="{h - 1}" rx="6" '
            f'fill="{T["card"]}" stroke="{T["border"]}"/>')


def text(x, y, s, cls="f t", size=14, weight=None, anchor=None, extra="") -> str:
    a = f' text-anchor="{anchor}"' if anchor else ""
    wt = f' font-weight="{weight}"' if weight else ""
    return f'<text x="{x:.1f}" y="{y:.1f}" class="{cls}" font-size="{size}"{wt}{a}{extra}>{escape(str(s))}</text>'


def box_header(w: int, title: str, note: str = "") -> list[str]:
    """Primer Box-header: title left, muted note right, hairline below."""
    out = [text(20, 34, title, "f t", 16, 600),
           f'<line x1="1" y1="54.5" x2="{w - 1}" y2="54.5" stroke="{T["border"]}"/>']
    if note:
        out.append(text(w - 20, 34, note, "f mu", 12.5, anchor="end"))
    return out


def counters(x0, y, items, total_w) -> list[str]:
    """A row of number + label pairs separated by hairlines."""
    out, col = [], total_w / len(items)
    for i, (value, label) in enumerate(items):
        x = x0 + i * col
        if i:
            out.append(f'<line x1="{x - 16:.1f}" y1="{y - 28}" x2="{x - 16:.1f}" y2="{y + 14}" stroke="{T["border"]}"/>')
        out.append(text(x, y, value, "f t num", 24, 600))
        out.append(text(x, y + 20, label, "f mu", 12.5))
    return out


def star(cx, cy, r, color) -> str:
    pts = []
    for i in range(10):
        rr = r if i % 2 == 0 else r * 0.45
        a = math.pi / 2 + i * math.pi / 5
        pts.append(f"{cx + rr * math.cos(a):.2f},{cy - rr * math.sin(a):.2f}")
    return f'<polygon points="{" ".join(pts)}" fill="none" stroke="{color}" stroke-width="1.3" stroke-linejoin="round"/>'


def fork_icon(x, y, color) -> str:
    return (f'<g fill="none" stroke="{color}" stroke-width="1.3" stroke-linecap="round">'
            f'<circle cx="{x + 2.5}" cy="{y + 2}" r="1.8"/><circle cx="{x + 9.5}" cy="{y + 2}" r="1.8"/>'
            f'<circle cx="{x + 6}" cy="{y + 12}" r="1.8"/>'
            f'<path d="M{x + 2.5} {y + 4}v1.5a2 2 0 0 0 2 2h3a2 2 0 0 0 2-2V{y + 4}M{x + 6} {y + 7.5}v2.7"/></g>')


def repo_icon(x, y, color) -> str:
    return (f'<g fill="none" stroke="{color}" stroke-width="1.3" stroke-linejoin="round">'
            f'<path d="M{x + 2} {y + 13}V{y + 2.5}a1.5 1.5 0 0 1 1.5-1.5H{x + 13}v10H{x + 3.5}a1.5 1.5 0 0 0-1.5 1.5'
            f'a1.5 1.5 0 0 0 1.5 1.5H{x + 6}"/><path d="M{x + 8} {y + 12.5}v3l1.3-1 1.3 1v-3"/></g>')


def tag_icon(x, y, color) -> str:
    return (f'<g fill="none" stroke="{color}" stroke-width="1.3" stroke-linejoin="round">'
            f'<path d="M{x + 1} {y + 1.5}v5l7 7 5-5-7-7z"/><circle cx="{x + 4.3}" cy="{y + 4.8}" r=".9"/></g>')


def pill(x, y, label, color, anchor_end=False) -> tuple[list[str], float]:
    w = width_of(label, 12) + 16
    x = x - w if anchor_end else x
    return ([f'<rect x="{x:.1f}" y="{y - 14:.1f}" width="{w:.1f}" height="20" rx="10" fill="none" stroke="{color}"/>',
             text(x + w / 2, y, label, "f", 12, 500, anchor="middle", extra=f' fill="{color}"')], w)


def width_of(s: str, size: float, mono=False) -> float:
    if mono:
        return len(s) * size * 0.6
    narrow = sum(1 for c in s if c in "ijlrtf.,:;'|!·( )-")
    wide = sum(1 for c in s if c in "mwMW@%")
    caps = sum(1 for c in s if c.isupper() or c.isdigit())
    return size * (0.52 * (len(s) - narrow - wide - caps) + 0.28 * narrow + 0.82 * wide + 0.62 * caps)


def wrap(s: str, max_w: float, size: float) -> list[str]:
    lines, cur = [], ""
    for word in s.split():
        trial = f"{cur} {word}".strip()
        if width_of(trial, size) > max_w and cur:
            lines.append(cur)
            cur = word
        else:
            cur = trial
    return lines + ([cur] if cur else [])


def ago(ts: dt.datetime) -> str:
    days = (NOW - ts).days
    if days < 1:
        return "today"
    if days == 1:
        return "yesterday"
    if days < 60:
        return f"{days} days ago"
    return f"{days // 30} months ago"


def fmt(n: int) -> str:
    return f"{n:,}"


def levels(values: list[int]) -> list[int]:
    """GitHub-style 0..4 levels: quartiles of the non-zero values."""
    nz = sorted(v for v in values if v)
    if not nz:
        return [0] * len(values)
    q = [nz[min(len(nz) - 1, int(len(nz) * f))] for f in (.25, .5, .75)]
    return [0 if v == 0 else 1 + sum(v > t for t in q) for v in values]


def legend(x_end, y) -> list[str]:
    out, x = [], x_end - 28
    out.append(text(x, y + 9, "More", "f mu", 12))
    for c in reversed(T["heat"]):
        x -= 14
        out.append(f'<rect x="{x:.1f}" y="{y}" width="10" height="10" rx="2" fill="{c}"/>')
    out.append(text(x - 6, y + 9, "Less", "f mu", 12, anchor="end"))
    return out


# ---------------------------------------------------------------- project identity
def display_name(repo: str) -> str:
    for p in CONFIG["featured"] + CONFIG["more"]:
        if p["repo"] == repo:
            return p.get("name", repo)
    return repo


def project_order() -> list[str]:
    return [p["repo"] for p in CONFIG["featured"] + CONFIG["more"]]


def color_of(repo: str) -> str:
    if repo == PRIVATE:
        return T["private"]
    order = project_order()
    return T["projects"][order.index(repo) % len(T["projects"])] if repo in order else T["subtle"]


# ---------------------------------------------------------------- panels
def provider_scope(data):
    status = data.get("providers", {}).get("gitea", {}).get("status")
    return "GitHub + Gitea" if status == "authenticated" else (
        "GitHub only · Gitea token missing" if status == "missing_token" else "GitHub")


def panel_header(data) -> str:
    h = 214
    rel = sorted((r for r in data["releases"] if r["repo"] != PRIVATE), key=lambda r: r["at"], reverse=True)
    latest, seen = [], set()
    for r in rel:
        if r["repo"] not in seen:
            seen.add(r["repo"])
            latest.append(r)
        if len(latest) == 4:
            break

    parts = [box(W, h),
             text(28, 66, "enderlit", "f t", 34, 600, extra=' letter-spacing="-.5"'),
             text(28, 96, "endercodezz", "f mu", 20, 300),
             text(28, 140, "Ports, recompilers and desktop tools", "f t", 16, 500),
             text(28, 163, "that make things run where they shouldn’t.", "f mu", 16)]
    # status-like line, the way GitHub shows a profile status
    parts += [f'<circle cx="34" cy="186" r="4" fill="{T["green"]}"/>',
              text(46, 190, provider_scope(data), "f mu", 13)]

    # "Latest releases" list, framed like a Primer Box
    bx, by, bw = 456, 20, 364
    rows = len(latest)
    bh = 44 + rows * 36
    parts.append(f'<rect x="{bx + .5}" y="{by + .5}" width="{bw - 1}" height="{bh - 1}" rx="6" '
                 f'fill="{T["canvas"]}" stroke="{T["border"]}"/>')
    parts.append(text(bx + 16, by + 27, "Latest releases", "f t", 13, 600))
    parts.append(text(bx + bw - 16, by + 27, "live from the API", "f mu", 12, anchor="end"))
    for i, r in enumerate(latest):
        y = by + 44 + i * 36
        parts.append(f'<line x1="{bx + 1}" y1="{y + .5}" x2="{bx + bw - 1}" y2="{y + .5}" stroke="{T["border"]}"/>')
        parts.append(tag_icon(bx + 16, y + 11, T["muted"]))
        parts.append(text(bx + 38, y + 23, display_name(r["repo"])[:18], "f bl", 13.5, 600))
        parts.append(text(bx + 200, y + 23, r["tag"][:14], "mono mu", 12.5))
        parts.append(text(bx + bw - 16, y + 23, ago(r["at"]), "f mu", 12, anchor="end"))
    return svg(W, h, "\n".join(parts), "enderlit (endercodezz): ports, recompilers and desktop tools")


def panel_activity(data) -> tuple[str, dict]:
    cc = data["cc"]
    cal = cc["contributionCalendar"]
    weeks = cal["weeks"]
    days = [x for w in weeks for x in w["contributionDays"]]
    total = cal["totalContributions"]
    private = cc["restrictedContributionsCount"]
    active = sum(1 for x in days if x["contributionCount"])
    peak = max(days, key=lambda x: x["contributionCount"])
    last30 = sum(x["contributionCount"] for x in days[-30:])
    share = round(100 * last30 / total) if total else 0

    head_label = "contributions" if private else "public contributions"
    note = (f"incl. {fmt(private)} in private repos, counted anonymously" if private
            else "public repos only · private contributions are hidden on this profile")
    # Commits: with a private-capable token, count my commits on the default
    # branches of every repo it can read (the same set as the rhythm panel).
    # PR and issue counts from the API never include private repos.
    full = data["has_private"] or data.get("providers", {}).get("gitea", {}).get("status") == "authenticated"
    commits = len(data["times"]) if full else cc["totalCommitContributions"]
    pre = "public " if (private or full) else ""
    commit_label = "commits, both hosts" if data.get("providers", {}).get("gitea", {}).get("status") == "authenticated" else (
        "commits, all repos" if full else pre + "commits")
    items = [(fmt(total), head_label),
             (fmt(commits), commit_label),
             (fmt(cc["totalPullRequestContributions"]), pre + "pull requests"),
             (fmt(cc["totalIssueContributions"]), pre + "issues"), (fmt(active), "active days")]

    h = 336
    parts = [box(W, h), *box_header(W, "GitHub contributions in the last year", note),
             *counters(28, 100, items, W - 40)]

    # heatmap, GitHub calendar geometry
    cell, gap = 11, 3
    gx, gy = 58, 168
    lv = levels([x["contributionCount"] for x in days])
    k = 0
    last_month = None
    for wi, wk in enumerate(weeks):
        x = gx + wi * (cell + gap)
        for dday in wk["contributionDays"]:
            y = gy + dday["weekday"] * (cell + gap)
            parts.append(f'<rect x="{x}" y="{y}" width="{cell}" height="{cell}" rx="2" fill="{T["heat"][lv[k]]}"/>')
            k += 1
        first = dt.date.fromisoformat(wk["contributionDays"][0]["date"])
        if first.month != last_month and first.day <= 7 and wi < len(weeks) - 2:
            parts.append(text(x, gy - 10, f"{first:%b}", "f mu", 12))
            last_month = first.month
        elif last_month is None:
            last_month = first.month
    for i, name in ((1, "Mon"), (3, "Wed"), (5, "Fri")):
        parts.append(text(28, gy + i * (cell + gap) + 9, name, "f mu", 12))
    base = gy + 7 * (cell + gap) + 16
    pd = dt.date.fromisoformat(peak["date"])
    parts.append(text(28, base + 9, f"Busiest day: {fmt(peak['contributionCount'])} on {pd:%b} {pd.day}"
                      f" · {share}% of the year in the last 30 days", "f mu", 12))
    parts += legend(W - 20, base)

    stats = {"total": total, "private": private, "label": head_label, "active": active,
             "commits": commits, "commits_all": full, "prs": cc["totalPullRequestContributions"],
             "issues": cc["totalIssueContributions"], "peak": peak, "last30_share": share}
    return svg(W, h, "\n".join(parts), f"{total} {head_label} in the last year"), stats


def panel_languages(data) -> tuple[str, list]:
    size, colors = Counter(), {}
    sources = [r["languages"]["edges"] for r in data["repos"] if not r["isFork"]]
    sources += data["private_langs"]
    for edges in sources:
        for e in edges:
            size[e["node"]["name"]] += e["size"]
            colors[e["node"]["name"]] = e["node"]["color"] or T["other"]
    # plus the code I wrote in forks and other people's repos (bytes my commits add)
    for lang, b in data.get("contrib_langs", {}).items():
        size[lang] += b
        colors.setdefault(lang, LANG_COLOR.get(lang) or T["other"])
    tot = sum(size.values()) or 1
    top = size.most_common(8)
    # Highlighted languages (profile.json) get their own row with their real share,
    # but only if they are present in the data.
    shown = {n for n, _ in top}
    top += sorted(((n, size[n]) for n in CONFIG.get("language_highlight", []) if n in size and n not in shown),
                  key=lambda x: -x[1])
    other = tot - sum(v for _, v in top)
    rows = top + ([("Other", other)] if other else [])
    colors["Other"] = T["other"]

    cols = 3
    nrows = math.ceil(len(rows) / cols)
    h = 112 + nrows * 28
    scope = provider_scope(data)
    parts = [box(W, h), *box_header(W, "Languages", f"share of bytes · {scope} + my commits in other repos")]
    # GitHub's repo language bar: 8px, rounded ends, hairline gaps
    bx0, bx1, by = 20, W - 20, 76
    parts.append(f'<clipPath id="bar"><rect x="{bx0}" y="{by}" width="{bx1 - bx0}" height="8" rx="4"/></clipPath>'
                 '<g clip-path="url(#bar)">')
    x = float(bx0)
    avail = bx1 - bx0 - 2 * (len(rows) - 1)
    for n, v in rows:
        sw = max(avail * v / tot, 1)
        parts.append(f'<rect x="{x:.1f}" y="{by}" width="{sw:.1f}" height="8" fill="{colors[n]}"/>')
        x += sw + 2
    parts.append("</g>")
    colw = (W - 40) / cols
    for i, (n, v) in enumerate(rows):
        cx = 20 + (i % cols) * colw
        cy = 118 + (i // cols) * 28
        pct = 100 * v / tot
        parts.append(f'<circle cx="{cx + 5}" cy="{cy - 4.5}" r="5" fill="{colors[n]}"/>')
        parts.append(text(cx + 18, cy, n, "f t", 13.5, 600))
        parts.append(text(cx + colw - 28, cy, f"{pct:.1f}%" if pct >= .1 else "<0.1%", "f mu num", 13.5, anchor="end"))
    summary = [(n, round(100 * v / tot, 1), colors[n]) for n, v in rows]
    return svg(W, h, "\n".join(parts), "Languages by share of bytes: " +
               ", ".join(f"{n} {p}%" for n, p, _ in summary)), summary


def panel_rhythm(data) -> tuple[str, dict]:
    grid = [[0] * 24 for _ in range(7)]
    for t in data["times"]:
        lt = t.astimezone(TZ)
        grid[lt.weekday()][lt.hour] += 1
    n = len(data["times"])
    wd, hr = max(((d, h) for d in range(7) for h in range(24)), key=lambda p: grid[p[0]][p[1]])
    night = sum(grid[d][h] for d in range(7) for h in range(0, 6))
    weekend = sum(sum(grid[d]) for d in (5, 6))
    names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    pct = (lambda v: round(100 * v / n) if n else 0)

    h = 382
    scope = provider_scope(data)
    parts = [box(W, h), *box_header(W, "Commit rhythm", f"{fmt(n)} commits · {scope} · last 12 months · hours in {TZ_LABEL}"),
             *counters(28, 100, [(f"{names[wd]} {hr:02d}:00", f"busiest hour, {TZ_LABEL}"),
                                 (f"{pct(night)}%", f"between 00:00 and 06:00 {TZ_LABEL}"),
                                 (f"{pct(weekend)}%", "on weekends")], W - 40)]
    flat = [grid[d][hh] for d in range(7) for hh in range(24)]
    lv = levels(flat)
    gx, gy, gap = 58, 150, 3
    cw = (W - 20 - gx - gap * 23) / 24
    ch = 22
    for d in range(7):
        parts.append(text(28, gy + d * (ch + gap) + 15, names[d], "f mu", 12))
        for hh in range(24):
            x = gx + hh * (cw + gap)
            y = gy + d * (ch + gap)
            parts.append(f'<rect x="{x:.1f}" y="{y}" width="{cw:.1f}" height="{ch}" rx="3" fill="{T["heat"][lv[d * 24 + hh]]}"/>')
    yb = gy + 7 * (ch + gap) + 18
    for hh in range(0, 24, 3):
        parts.append(text(gx + hh * (cw + gap), yb, f"{hh:02d}:00", "f mu", 12))
    parts += legend(W - 20, yb + 14)
    stats = {"n": n, "peak": f"{names[wd]} {hr:02d}:00 {TZ_LABEL}", "night": pct(night), "weekend": pct(weekend), "grid": grid}
    return svg(W, h, "\n".join(parts), f"Commit rhythm: busiest hour {stats['peak']}, "
               f"{stats['night']}% between midnight and 6am"), stats


def panel_shipping(data) -> tuple[str, dict]:
    rels = data["releases"]
    by_repo = defaultdict(list)
    for r in rels:
        by_repo[r["repo"]].append(r)
    lanes = [p for p in project_order() if p in by_repo]
    lanes += sorted((r for r in by_repo if r not in lanes and r != PRIVATE), key=lambda r: -len(by_repo[r]))
    if PRIVATE in by_repo:
        lanes.append(PRIVATE)   # all private repos share one lane, always last
    start = min(r["at"] for r in rels).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = NOW
    total_dl = sum(r["downloads"] for r in rels)

    lane_h = 36
    top = 96
    h = top + lane_h * len(lanes) + 44
    parts = [box(W, h), *box_header(W, "Releases",
                                    f"{len(rels)} published since {start:%b %Y} · {fmt(total_dl)} asset downloads")]
    x0, x1 = 196, 660
    span = (end - start).total_seconds()

    def X(t):
        return x0 + (x1 - x0) * (t - start).total_seconds() / span

    parts.append(text(x1 + 70, top - 14, "releases", "f mu", 12, anchor="end"))
    parts.append(text(W - 20, top - 14, "downloads", "f mu", 12, anchor="end"))
    m, ticks = start, []
    while m <= end:
        ticks.append(m)
        m = (m + dt.timedelta(days=32)).replace(day=1)
    step = max(1, math.ceil(len(ticks) / 7))
    bottom = top + lane_h * len(lanes)
    for i, t in enumerate(ticks):
        x = X(t)
        parts.append(f'<line x1="{x:.1f}" y1="{top - 4}" x2="{x:.1f}" y2="{bottom}" stroke="{T["muted_border"]}"/>')
        if i % step == 0:
            parts.append(text(x + 4, bottom + 20, f"{t:%b %Y}" if i == 0 or t.month == 1 else f"{t:%b}", "f mu", 12))
    for i, repo in enumerate(lanes):
        y = top + i * lane_h + lane_h / 2
        if i:
            parts.append(f'<line x1="20" y1="{top + i * lane_h:.1f}" x2="{W - 20}" y2="{top + i * lane_h:.1f}" stroke="{T["muted_border"]}"/>')
        c = color_of(repo)
        rs = sorted(by_repo[repo], key=lambda r: r["at"])
        parts.append(f'<circle cx="26" cy="{y:.1f}" r="5" fill="{c}"/>')
        if repo == PRIVATE:
            parts.append(text(40, y + 4.5, PRIVATE, "mono mu", 12.5, 600, extra=' letter-spacing=".5"'))
        else:
            parts.append(text(40, y + 4.5, display_name(repo)[:20], "f t", 13.5, 600))
        if len(rs) > 1:
            parts.append(f'<line x1="{X(rs[0]["at"]):.1f}" y1="{y:.1f}" x2="{X(rs[-1]["at"]):.1f}" y2="{y:.1f}" '
                         f'stroke="{c}" stroke-opacity=".5" stroke-width="2"/>')
        for r in rs:
            parts.append(f'<circle cx="{X(r["at"]):.1f}" cy="{y:.1f}" r="5" fill="{c}" stroke="{T["card"]}" stroke-width="2"/>')
        parts.append(text(x1 + 70, y + 4.5, str(len(rs)), "f t num", 13.5, 600, anchor="end"))
        parts.append(text(W - 20, y + 4.5, fmt(sum(r["downloads"] for r in rs)), "f mu num", 13.5, anchor="end"))
    stats = {"releases": len(rels), "downloads": total_dl, "since": f"{start:%b %Y}",
             "per_repo": {r: (len(by_repo[r]), sum(x["downloads"] for x in by_repo[r])) for r in lanes}}
    return svg(W, h, "\n".join(parts), f"{len(rels)} releases and {total_dl} downloads since {start:%b %Y}"), stats


def repo_stats(data, name):
    r = next(x for x in data["repos"] if x["name"] == name)
    rels = sorted((x for x in data["releases"] if x["repo"] == name), key=lambda x: x["at"], reverse=True)
    return r, rels


def card_footer(x, y, r, extra=()) -> list[str]:
    """Pinned-repo footer: language dot, stars, forks, then extra counters."""
    out = []
    lang = r["primaryLanguage"]
    if lang:
        out.append(f'<circle cx="{x + 6}" cy="{y - 4.5}" r="6" fill="{lang["color"] or T["other"]}"/>')
        out.append(text(x + 18, y, lang["name"], "f mu", 13))
        x += 18 + width_of(lang["name"], 13) + 18
    out.append(star(x + 7, y - 5, 7.5, T["muted"]))
    out.append(text(x + 20, y, fmt(r["stargazerCount"]), "f mu num", 13))
    x += 20 + width_of(fmt(r["stargazerCount"]), 13) + 18
    if r["forkCount"]:
        out.append(fork_icon(x, y - 12, T["muted"]))
        out.append(text(x + 18, y, fmt(r["forkCount"]), "f mu num", 13))
        x += 18 + width_of(fmt(r["forkCount"]), 13) + 18
    for value, label in extra:
        s = f"{value} {label}"
        out.append(text(x, y, s, "f mu num", 13))
        x += width_of(s, 13) + 18
    return out


def card_featured(data, p) -> str:
    r, rels = repo_stats(data, p["repo"])
    h = 222
    dl = sum(x["downloads"] for x in rels)
    name = p.get("name", p["repo"])
    parts = [box(W, h), repo_icon(24, 22, T["muted"]),
             text(48, 35, name, "f bl", 20, 600)]
    pl, _ = pill(48 + width_of(name, 20) + 14, 33, "Public", T["muted"])
    parts += pl
    parts.append(text(W - 24, 35, p["kicker"], "f mu", 12.5, anchor="end"))
    parts.append(text(24, 70, p["tagline"], "f t", 15.5, 500))
    for i, f in enumerate(p["facts"]):
        y = 102 + i * 26
        parts.append(f'<circle cx="31" cy="{y - 4.5}" r="2.5" fill="{T["subtle"]}"/>')
        parts.append(text(44, y, f, "f mu", 14))
    parts.append(f'<line x1="1" y1="{h - 52.5}" x2="{W - 1}" y2="{h - 52.5}" stroke="{T["border"]}"/>')
    counts = (f"{fmt(data['my_commits'].get(p['repo'], 0))} mine / "
              f"{fmt(data.get('total_commits', {}).get(p['repo'], 0))}")
    parts += card_footer(24, h - 21, r, [(counts, "total"),
                                         (len(rels), "releases"), (fmt(dl), "downloads")])
    if rels:
        right = W - 24
        s = f"{rels[0]['tag']} · {ago(rels[0]['at'])}"
        parts.append(text(right, h - 21, s, "f mu", 13, anchor="end"))
        pl, _ = pill(right - width_of(s, 13) - 10, h - 21, "Latest", T["green"], anchor_end=True)
        parts += pl
    return svg(W, h, "\n".join(parts), f"{p['repo']}: {p['tagline']}")


def card_small(data, p) -> str:
    r, rels = repo_stats(data, p["repo"])
    w, h = 410, 176
    name = p.get("name", p["repo"])
    lines = wrap(p["desc"], w - 44, 13.5)[:3]
    parts = [box(w, h), repo_icon(20, 20, T["muted"]), text(42, 33, name, "f bl", 15.5, 600)]
    pl, _ = pill(w - 20, 32, "Public", T["muted"], anchor_end=True)
    parts += pl
    for i, ln in enumerate(lines):
        parts.append(text(20, 64 + i * 20, ln, "f mu", 13.5))
    parts += card_footer(20, h - 20, r)
    if rels:
        parts.append(text(w - 20, h - 20, rels[0]["tag"], "mono mu", 12, anchor="end"))
        parts.append(tag_icon(w - 20 - width_of(rels[0]["tag"], 12, mono=True) - 20, h - 31, T["muted"]))
    return svg(w, h, "\n".join(parts), f"{p['repo']}: {p['desc']}")


# ---------------------------------------------------------------- README block
def export_json(data, act, langs, rhythm, ship) -> str:
    """Aggregated, public-safe data for enderlit.com. Private repos appear only
    as the PRIVATE bucket (no names, no tags); everything else is public info."""
    days = [x for w in data["cc"]["contributionCalendar"]["weeks"] for x in w["contributionDays"]]
    projects = []
    for p in CONFIG["featured"] + CONFIG["more"]:
        r, rels = repo_stats(data, p["repo"])
        projects.append({
            "repo": p["repo"], "name": p.get("name", p["repo"]),
            "featured": p in CONFIG["featured"], "color_index": project_order().index(p["repo"]),
            "stars": r["stargazerCount"], "forks": r["forkCount"],
            "commits": data["my_commits"].get(p["repo"], 0),
            "total_commits": data.get("total_commits", {}).get(p["repo"], 0),
            "language": r["primaryLanguage"], "pushed": r["pushedAt"],
            "releases": len(rels), "downloads": sum(x["downloads"] for x in rels),
            "latest": {"tag": rels[0]["tag"], "at": rels[0]["at"].isoformat()} if rels else None,
        })
    tracked = {p["repo"] for p in projects}
    others = [{"repo": r["name"], "description": r["description"], "stars": r["stargazerCount"],
               "language": r["primaryLanguage"], "pushed": r["pushedAt"]}
              for r in data["repos"] if not r["isFork"] and r["name"] not in tracked]
    payload = {
        "v": 1, "generated": NOW.isoformat(timespec="seconds"), "user": USER,
        "contributions": {
            "total": act["total"], "private": act["private"], "active_days": act["active"],
            "commits": act["commits"], "commits_all_repos": act["commits_all"],
            "pull_requests": act["prs"], "issues": act["issues"],
            "start": days[0]["date"], "counts": [x["contributionCount"] for x in days],
        },
        "languages": [{"name": n, "pct": p, "color": c} for n, p, c in langs],
        "rhythm": {"commits": rhythm["n"], "grid": rhythm["grid"],
                   "tz": TZ_LABEL},
        "releases": [{"repo": r["repo"], "tag": None if r["repo"] == PRIVATE else r["tag"],
                      "at": r["at"].isoformat(), "downloads": r["downloads"]}
                     for r in sorted(data["releases"], key=lambda r: r["at"])],
        "projects": projects, "other_repos": others,
        "providers": data.get("providers", {}),
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def numbers_block(act, langs, rhythm, ship, providers=None) -> str:
    lines = ["| | |", "|---|---|"]
    tot = f"{fmt(act['total'])} {act['label']}"
    if act["private"]:
        tot += f" (incl. {fmt(act['private'])} private, counted anonymously)"
    commits = f"{fmt(act['commits'])} commits" + (" across public and private repos" if act["commits_all"] else "")
    pub = "public " if act["commits_all"] or act["private"] else ""
    lines += [f"| Last year | {tot} on {act['active']} active days |",
              f"| Breakdown | {commits} · {fmt(act['prs'])} {pub}pull requests · {fmt(act['issues'])} {pub}issues |",
              f"| Releases | {ship['releases']} published since {ship['since']} · {fmt(ship['downloads'])} asset downloads |",
              f"| Commit rhythm | busiest hour {rhythm['peak']} · {rhythm['night']}% between 00:00 and 06:00 {TZ_LABEL} · {rhythm['weekend']}% on weekends |",
              "| Languages | " + " · ".join(f"{n} {p}%" for n, p, _ in langs) + " |"]
    stamp = NOW.strftime("%Y-%m-%d %H:%M UTC")
    scope = provider_scope({"providers": providers or {}})
    return ("<details>\n<summary>The same numbers as text</summary>\n\n" + "\n".join(lines) +
            f"\n\n<sub>Generated {stamp}. Sources: {scope}. Contribution calendar: GitHub; commit rhythm, languages and releases: listed sources.</sub>\n</details>")


def leak_check(files: dict[str, str], secrets: set[str]) -> None:
    """Abort before writing if any private name, description or URL is in the output.

    The message never says which secret matched (Actions logs are public).
    """
    needles = [s.lower() for s in secrets if len(s) >= 3]
    token_shape = re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")
    for fname, content in files.items():
        # The literal token is only searched when it looks like a real one: a short
        # placeholder would match ordinary words ("test" in "Latest").
        if any(token and token in content for token in (GITEA_TOKEN, TOKEN if TOKEN and len(TOKEN) >= 20 else None)) or token_shape.search(content):
            sys.exit(f"leak check failed in {fname}: a token was found; nothing was written")
        hay = content.lower()
        for s in needles:
            if re.search(r"(?<![a-z0-9])" + re.escape(s) + r"(?![a-z0-9])", hay):
                group = ""
                if fname == "stats.json":
                    try:
                        payload = json.loads(content)
                    except ValueError:
                        payload = {}
                    if not isinstance(payload, dict):
                        payload = {}
                    for field in ("projects", "other_repos", "providers", "releases", "languages", "contributions", "rhythm"):
                        if re.search(r"(?<![a-z0-9])" + re.escape(s) + r"(?![a-z0-9])", json.dumps(payload.get(field), ensure_ascii=False).lower()):
                            group = " in " + field
                            break
                sys.exit(f"leak check failed in {fname}: private repo metadata found{group}; nothing was written")
    print(f"leak check passed: {len(files)} files, {len(needles)} private strings checked")


def render_all(data) -> tuple[dict[str, str], tuple]:
    global T
    out, stats = {}, None
    for theme in ("light", "dark"):
        T = THEMES[theme]
        act_svg, act = panel_activity(data)
        lang_svg, langs = panel_languages(data)
        rh_svg, rhythm = panel_rhythm(data)
        sh_svg, ship = panel_shipping(data)
        panels = {"header": panel_header(data), "activity": act_svg, "languages": lang_svg,
                  "rhythm": rh_svg, "shipping": sh_svg}
        for p in CONFIG["featured"]:
            panels[f"card-{p['repo'].lower()}"] = card_featured(data, p)
        for p in CONFIG["more"]:
            panels[f"card-{p['repo'].lower()}"] = card_small(data, p)
        out.update({f"{k}-{theme}.svg": v for k, v in panels.items()})
        stats = (act, langs, rhythm, ship)
    return out, stats


def main():
    if not TOKEN:
        sys.exit("GH_TOKEN is not set")
    data = collect()
    out, (act, langs, rhythm, ship) = render_all(data)

    readme = ROOT / "README.md"
    src = readme.read_text("utf-8")
    block = numbers_block(act, langs, rhythm, ship, data.get("providers"))
    src, n = re.subn(r"(<!-- numbers:start -->\n).*?(<!-- numbers:end -->)",
                     lambda m: m.group(1) + block + "\n" + m.group(2), src, flags=re.S)
    if n != 1:
        sys.exit("README is missing the <!-- numbers:start/end --> markers")

    out["stats.json"] = export_json(data, act, langs, rhythm, ship)
    leak_check({**out, "README.md": src}, data["secrets"])
    ASSETS.mkdir(exist_ok=True)
    for old in [*ASSETS.glob("*.svg"), *ASSETS.glob("*.json")]:
        if old.name not in out:
            old.unlink()
    for name, content in out.items():
        (ASSETS / name).write_text(content, "utf-8", newline="\n")
    readme.write_text(src, "utf-8", newline="\n")
    print(f"wrote {len(out)} svgs · {act['total']} {act['label']} · {ship['releases']} releases")


if __name__ == "__main__":
    main()
