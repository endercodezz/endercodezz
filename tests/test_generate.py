"""Tests for scripts/generate.py. Stdlib only, no network: GitHub is a fake.

    python -m unittest discover -s tests -v
"""
import datetime as dt
import io
import json
import sys
import unittest
import urllib.error
import urllib.parse
from collections import Counter
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import generate as g  # noqa: E402

RECENT = (g.NOW - dt.timedelta(days=10)).isoformat()
OLD = (g.NOW - dt.timedelta(days=500)).isoformat()


def repo_node(name, fork=False):
    return {"name": name, "isFork": fork, "stargazerCount": 0, "forkCount": 0, "pushedAt": RECENT,
            "createdAt": OLD, "description": None, "primaryLanguage": None,
            "languages": {"edges": [{"size": 1000, "node": {"name": "C++", "color": "#f34b7d"}}]},
            "releases": {"totalCount": 0, "nodes": []}}


def ext(owner, name, private=False):
    return {"name": name, "owner": {"login": owner}, "isPrivate": private, "isFork": False,
            "description": f"{name} description" if private else None, "url": f"https://github.com/{owner}/{name}"}


# Commit history per "owner/name": list of pages, each a list of (sha, date).
HISTORY = {
    "endercodezz/alpha": [[("a1", RECENT), ("a2", OLD)], [("a3", RECENT)]],    # two pages
    "endercodezz/fork": [[("f1", RECENT), ("s1", RECENT)]],                      # my fork
    "up/stream": [[("s1", RECENT), ("u1", RECENT)]],                             # s1 merged upstream
    "other/lib": [[("a1", RECENT)]],                                             # same commit as alpha
    "endercodezz/secret-own": [[("p1", RECENT)]],                                # my private repo
    "orgx/secret-org": [[("o1", RECENT)]],                                       # private org repo
    "far/away": [[("z1", RECENT)]],                                            # only found by commit search
    "endercodezz/endercodezz": [[("e1", RECENT)]],                             # this profile repo
    # "gone/repo" is missing on purpose
}

DIFFS = {
    "f1": [{"filename": "src/x.rs", "status": "added", "patch": "@@ -0,0 +1,2 @@\n+fn a(){}\n+//"}],
    "s1": [{"filename": "main.zig", "status": "modified", "patch": "@@ -1 +1 @@\n+const x=1;\n context\n-old"}],
    "u1": [{"filename": "README.md", "status": "modified", "patch": "+hello"},          # prose: ignored
           {"filename": "vendor/lib.c", "status": "added", "patch": "+int x;"},         # vendored: ignored
           {"filename": "a.py", "status": "removed", "patch": "-print()"},             # removed: ignored
           {"filename": "big.cpp", "status": "modified"}],                             # no patch: unmeasured
    "z1": [{"filename": "merged.cpp", "status": "added", "patch": "+" + "x" * 500}],  # a merge: skipped
    # o1: commit unreadable -> REST returns None
}


class FakeGitHub:
    def __init__(self):
        self.calls = []

    def gql(self, query, private=False, missing_ok=False, **v):
        self.calls.append((query, private, missing_ok, v))
        if query is g.PROFILE_Q:
            return {"user": {"id": "ME", "createdAt": "2024-01-08T20:18:27Z", "contributionsCollection": {},
                             "repositories": {"nodes": [repo_node("alpha"), repo_node("fork", fork=True)]}}}
        if query is g.VIEWER_Q:
            return {"viewer": {"login": g.USER}}
        if query is g.PRIVATE_Q:
            own = {**repo_node("secret-own"), "owner": {"login": g.USER}, "url": "https://github.com/x/secret-own",
                   "description": "my secret thing"}
            org = {**repo_node("secret-org"), "owner": {"login": "orgx"}, "url": "https://github.com/orgx/secret-org",
                   "description": None}
            return {"viewer": {"repositories": {"pageInfo": {"hasNextPage": False, "endCursor": None},
                                                "nodes": [own, org]}}}
        if query is g.EXTERNAL_Q:
            if v["after"] is None:
                return {"user": {
                    "repositoriesContributedTo": {"pageInfo": {"hasNextPage": True, "endCursor": "c1"},
                                                  "nodes": [ext("up", "stream"), ext("gone", "repo")]},
                    "contributionsCollection": {"commitContributionsByRepository": [
                        {"repository": ext("up", "stream")},                  # duplicate source
                        {"repository": ext("orgx", "secret-org", private=True)},
                        {"repository": ext(g.USER, "alpha")}]}}}           # own repo: not external
            return {"user": {"repositoriesContributedTo": {"pageInfo": {"hasNextPage": False, "endCursor": None},
                                                           "nodes": [ext("other", "lib")]},
                             "contributionsCollection": {"commitContributionsByRepository": []}}}
        if query is g.HISTORY_Q:
            assert missing_ok, "history must tolerate missing repositories"
            pages = HISTORY.get(f"{v['owner']}/{v['name']}".lower())
            if pages is None:
                return {"repository": None}                  # what gql(missing_ok=True) yields on NOT_FOUND
            i = 0 if v["after"] is None else int(v["after"])
            return {"repository": {"defaultBranchRef": {"target": {"history": {
                "totalCount": sum(map(len, pages)),
                "pageInfo": {"hasNextPage": i + 1 < len(pages), "endCursor": str(i + 1)},
                "nodes": [{"oid": s, "authoredDate": d} for s, d in pages[i]]}}}}}
        if query is g.ALLTIME_Q:
            return {"repository": {"defaultBranchRef": {"target": {"history": {"totalCount": 0}}}}}
        raise AssertionError("unexpected query")

    def rest(self, path, private=False):
        if path.startswith("/search/commits"):
            self.calls.append(("SEARCH", path, private))
            hit = lambda full, priv: {"repository": {"full_name": full, "name": full.split("/")[1],
                                                     "owner": {"login": full.split("/")[0]}, "private": priv,
                                                     "fork": False, "description": None, "html_url": "u"}}
            return {"total_count": 3, "items": [hit("far/away", False), hit("endercodezz/alpha", False),
                                                 hit("up/stream", False)]}
        self.calls.append(("REST", path, private))
        sha = path.split("/commits/")[1].split("?")[0]
        if sha not in DIFFS:
            return None
        return {"files": DIFFS[sha], "parents": [{}, {}] if sha == "z1" else [{}]}


class CollectTest(unittest.TestCase):
    def setUp(self):
        self.gh = FakeGitHub()
        patches = [mock.patch.object(g, "gql", self.gh.gql), mock.patch.object(g, "rest", self.gh.rest),
                   mock.patch.object(g, "TOKEN", "test"), mock.patch.object(g, "SEARCH_PAUSE", 0),
                   mock.patch("sys.stdout", new_callable=io.StringIO)]
        self.out = [p.start() for p in patches][-1]
        for p in patches:
            self.addCleanup(p.stop)
        self.data = g.collect()

    def test_each_commit_counted_once(self):
        # unique SHAs: a1 a2 a3 f1 s1 u1 p1 o1 z1 e1; a2 is older than a year
        self.assertEqual(len(self.data["times"]), 9)

    def test_repo_found_only_by_commit_search_is_read(self):
        away = [c for c in self.gh.calls if c[0] is g.HISTORY_Q and c[3]["name"] == "away"]
        self.assertEqual(len(away), 1)

    def test_merge_commits_add_no_language_bytes(self):
        self.assertNotIn("C++", self.data["contrib_langs"])
        self.assertIn("1 merges skipped", self.out.getvalue())

    def test_history_is_paginated(self):
        alpha = [c for c in self.gh.calls if c[0] is g.HISTORY_Q and c[3]["name"] == "alpha"]
        self.assertEqual([c[3]["after"] for c in alpha], [None, "1"])

    def test_repo_reached_twice_is_read_once(self):
        stream = [c for c in self.gh.calls if c[0] is g.HISTORY_Q and c[3]["name"] == "stream"]
        self.assertEqual(len(stream), 1)

    def test_missing_repo_is_skipped(self):
        gone = [c for c in self.gh.calls if c[0] is g.HISTORY_Q and c[3]["name"] == "repo"]
        self.assertEqual(len(gone), 1)          # asked once, returned nothing, no crash

    def test_languages_only_from_my_diffs_outside_own_repos(self):
        self.assertEqual(self.data["contrib_langs"], Counter({"Rust": 12, "Zig": 11}))
        fetched = sorted(c[1].split("/commits/")[1].split("?")[0] for c in self.gh.calls if c[0] == "REST")
        # a1 lives in my own repo, p1 is my own private repo: Linguist already counts them
        # e1 is in this profile repo (mine); z1 is fetched but skipped as a merge
        self.assertEqual(fetched, ["f1", "o1", "s1", "u1", "z1"])

    def test_private_queries_and_secrets(self):
        priv = {c[3]["name"] for c in self.gh.calls if c[0] is g.HISTORY_Q and c[1]}
        self.assertEqual(priv, {"secret-own", "secret-org"})
        org_rest = [c for c in self.gh.calls if c[0] == "REST" and "secret-org" in c[1]]
        self.assertTrue(org_rest and all(c[2] for c in org_rest))
        for s in ("secret-own", "secret-org", "orgx", "orgx/secret-org", "my secret thing"):
            self.assertIn(s, self.data["secrets"])
        self.assertNotIn("stream", self.data["secrets"])

    def test_no_external_language_stats_used(self):
        # the fake gives every repo 1000 bytes of C++ in Linguist; only my own non-fork
        # repos (alpha public, secret-own private) may contribute those
        own_bytes = sum(e["size"] for r in self.data["repos"] if not r["isFork"] for e in r["languages"]["edges"])
        own_bytes += sum(e["size"] for edges in self.data["private_langs"] for e in edges)
        self.assertEqual(own_bytes, 2000)

    def test_language_panel_merges_contributions(self):
        with mock.patch.object(g, "CONFIG", {**g.CONFIG, "language_highlight": []}):
            _, summary = g.panel_languages(self.data)
        shares = {n: p for n, p, _ in summary}
        self.assertEqual(set(shares), {"C++", "Rust", "Zig"})
        self.assertAlmostEqual(shares["Rust"], round(100 * 12 / 2023, 1))

    def test_rendered_output_has_no_private_names(self):
        when = g.NOW - dt.timedelta(days=3)
        data = {**self.data, "cc": fake_cc(), "my_commits": {},
                "releases": [{"repo": "alpha", "tag": "v1", "at": when, "downloads": 5},
                             {"repo": g.PRIVATE, "tag": None, "at": when, "downloads": 2}]}
        config = {**g.CONFIG, "language_highlight": [], "more": [],
                  "featured": [{"repo": "alpha", "kicker": "k", "tagline": "t", "facts": ["f"]}]}
        with mock.patch.object(g, "CONFIG", config):
            out, stats = g.render_all(data)
            out["stats.json"] = g.export_json(data, *stats)
        with mock.patch("sys.stdout", new_callable=io.StringIO):
            g.leak_check(out, self.data["secrets"])          # exits on any private name


def fake_cc():
    days = [{"date": (dt.date(2026, 1, 4) + dt.timedelta(days=i)).isoformat(), "contributionCount": i % 3,
             "weekday": (i % 7)} for i in range(364)]
    return {"totalCommitContributions": 1, "totalIssueContributions": 0, "totalPullRequestContributions": 0,
            "totalPullRequestReviewContributions": 0, "restrictedContributionsCount": 0,
            "contributionCalendar": {"totalContributions": sum(d["contributionCount"] for d in days),
                                     "weeks": [{"contributionDays": days[i:i + 7]} for i in range(0, 364, 7)]}}


class HelpersTest(unittest.TestCase):
    def test_language_of(self):
        cases = {"a/b.cpp": "C++", "x.h": "C", "lib.rs": "Rust", "build.zig": "Zig", "CMakeLists.txt": "CMake",
                 "Dockerfile": "Dockerfile", "README.md": None, "data.json": None, "third_party/x.c": None,
                 "node_modules/a.js": None, "noext": None, ".hidden": None}
        for path, lang in cases.items():
            self.assertEqual(g.language_of(path), lang, path)

    def test_added_bytes_counts_only_added_lines(self):
        patch = "--- a/f\n+++ b/f\n@@ -1,2 +1,2 @@\n ctx\n-gone\n+new line\n+é"
        self.assertEqual(g.added_bytes(patch), len("new line") + 1 + len("é".encode()) + 1)

    def test_commit_file_list_is_paginated(self):
        pages = {1: [{"filename": f"f{i}.py", "status": "added", "patch": "+x"} for i in range(100)],
                 2: [{"filename": "last.py", "status": "added", "patch": "+yy"}]}
        calls = []

        def rest(path, private=False):
            calls.append(path)
            return {"files": pages[int(path.rsplit("page=", 1)[1])]}

        with mock.patch.object(g, "rest", rest):
            out = g.commit_languages("o", "n", "sha", False, Counter())
        self.assertEqual(len(calls), 2)
        self.assertEqual(out["Python"], 100 * 2 + 3)


class SearchWindowTest(unittest.TestCase):
    def test_window_over_1000_hits_is_split(self):
        windows = []

        def rest(path, private=False):
            q = urllib.parse.unquote(path.split("q=")[1].split("&")[0])
            span = q.split("author-date:")[1]
            windows.append(span)
            a, b = (dt.date.fromisoformat(x) for x in span.split(".."))
            hits = 1500 if (b - a).days > 200 else 5
            item = {"repository": {"full_name": f"o/{a.year}", "name": str(a.year), "owner": {"login": "o"},
                                   "private": False, "fork": False, "description": None, "html_url": "u"}}
            return {"total_count": hits, "items": [item] * min(hits, 5)}

        with mock.patch.object(g, "rest", rest), mock.patch.object(g, "SEARCH_PAUSE", 0):
            repos = g.search_commit_repos(dt.date(2025, 1, 1), dt.date(2025, 12, 31))
        def days(span):
            a, b = (dt.date.fromisoformat(x) for x in span.split(".."))
            return (b - a).days

        self.assertEqual(windows[0], "2025-01-01..2025-12-31")
        self.assertGreater(len(windows), 2)                                  # it was split
        self.assertTrue(all(days(w) < days(windows[0]) for w in windows[1:]))
        self.assertEqual({r["owner"]["login"] for r in repos}, {"o"})


class ApiErrorTest(unittest.TestCase):
    def payload(self, body):
        return mock.patch("urllib.request.urlopen",
                          return_value=mock.MagicMock(__enter__=lambda s: io.BytesIO(json.dumps(body).encode()),
                                                      __exit__=lambda *a: False))

    def test_not_found_tolerated_only_when_allowed(self):
        body = {"data": {"repository": None}, "errors": [{"type": "NOT_FOUND", "message": "x"}]}
        with mock.patch.object(g, "TOKEN", "t"), self.payload(body):
            self.assertEqual(g.gql("q", missing_ok=True), {"repository": None})
        with mock.patch.object(g, "TOKEN", "t"), self.payload(body), self.assertRaises(SystemExit):
            g.gql("q")

    def test_other_errors_still_fail(self):
        body = {"data": None, "errors": [{"type": "RATE_LIMITED", "message": "x"}]}
        with mock.patch.object(g, "TOKEN", "t"), self.payload(body), self.assertRaises(SystemExit):
            g.gql("q", missing_ok=True)

    def test_private_errors_hide_details(self):
        body = {"data": None, "errors": [{"type": "SOMETHING", "message": "orgx/secret-org"}]}
        with mock.patch.object(g, "TOKEN", "t"), self.payload(body):
            with self.assertRaises(SystemExit) as cm:
                g.gql("q", private=True)
        self.assertNotIn("secret", str(cm.exception))

    def test_rest_skips_missing_but_not_rate_limits(self):
        def raise_(code, remaining="10"):
            err = urllib.error.HTTPError("u", code, "x", {"X-RateLimit-Remaining": remaining}, None)
            return mock.patch("urllib.request.urlopen", side_effect=err)
        with mock.patch.object(g, "TOKEN", "t"):
            for code in (404, 409, 451, 403):
                with raise_(code):
                    self.assertIsNone(g.rest("/x"))
            with raise_(403, remaining="0"), self.assertRaises(SystemExit):
                g.rest("/x")
            with raise_(500), self.assertRaises(SystemExit):
                g.rest("/x")


class LeakCheckTest(unittest.TestCase):
    def test_catches_private_name_and_tokens(self):
        with self.assertRaises(SystemExit):
            g.leak_check({"a.svg": "<text>secret-org</text>"}, {"secret-org"})
        with self.assertRaises(SystemExit):
            g.leak_check({"a.svg": "ghp_" + "a" * 36}, set())
        with mock.patch("sys.stdout", new_callable=io.StringIO):
            g.leak_check({"a.svg": "<text>public</text>"}, {"secret-org"})


if __name__ == "__main__":
    unittest.main()
