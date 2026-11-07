"""Tests for scripts/generate.py. Stdlib only, no network: GitHub is a fake.

    python -m unittest discover -s tests -v
"""
import datetime as dt
import io
import json
import sys
import unittest
import urllib.error
import xml.etree.ElementTree as ET
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
    "up/stream": [[("s1", RECENT), ("u1", RECENT), ("z1", RECENT)]],             # s1 merged upstream
    "other/lib": [[("a1", RECENT)]],                                             # same commit as alpha
    "endercodezz/secret-own": [[("p1", RECENT)]],                                # my private repo
    "orgx/secret-org": [[("o1", RECENT)]],                                       # private org repo
    "acidvegas/farming": [[("sp1", RECENT), ("sp2", RECENT)]],                  # forged authorship
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
                                                           "nodes": [ext("other", "lib"), ext("acidvegas", "farming")]},
                             "contributionsCollection": {"commitContributionsByRepository": []}}}
        if query is g.HISTORY_Q:
            assert missing_ok, "history must tolerate missing repositories"
            pages = HISTORY.get(f"{v['owner']}/{v['name']}".lower())
            if pages is None:
                return {"repository": None}                  # what gql(missing_ok=True) yields on NOT_FOUND
            i = 0 if v["after"] is None else int(v["after"])
            return {"repository": {"defaultBranchRef": {"target": {"allCommits": {"totalCount": sum(map(len, pages))}, "history": {
                "totalCount": sum(map(len, pages)),
                "pageInfo": {"hasNextPage": i + 1 < len(pages), "endCursor": str(i + 1)},
                "nodes": [{"oid": s, "authoredDate": d, "author": {"user": {"id": "ME"}}}
                          for s, d in pages[i]]}}}}}
        if query is g.ALLTIME_Q:
            return {"repository": {"defaultBranchRef": {"target": {
                "yours": {"totalCount": 7}, "total": {"totalCount": 19}}}}}
        raise AssertionError("unexpected query")

    def rest(self, path, private=False):
        if path.startswith("/search/"):
            raise AssertionError("commit search must not be used: it trusts forgeable author e-mails")
        self.calls.append(("REST", path, private))
        sha = path.split("/commits/")[1].split("?")[0]
        if sha not in DIFFS:
            return None
        return {"files": DIFFS[sha], "parents": [{}, {}] if sha == "z1" else [{}]}


class CollectTest(unittest.TestCase):
    def setUp(self):
        self.gh = FakeGitHub()
        patches = [mock.patch.object(g, "gql", self.gh.gql), mock.patch.object(g, "rest", self.gh.rest),
                   mock.patch.object(g, "TOKEN", "test"),
                   mock.patch.object(g, "CONFIG", {**g.CONFIG, "exclude_repos": ["acidvegas/farming"]}),
                   mock.patch("sys.stdout", new_callable=io.StringIO)]
        self.out = [p.start() for p in patches][-1]
        for p in patches:
            self.addCleanup(p.stop)
        self.data = g.collect()

    def test_each_commit_counted_once(self):
        # unique SHAs: a1 a2 a3 f1 s1 u1 p1 o1 z1 e1; a2 is older than a year
        self.assertEqual(len(self.data["times"]), 9)

    def test_excluded_repo_is_never_read(self):
        # listed by a trusted source, but forges my authorship: no history, no diffs
        farming = [c for c in self.gh.calls if (c[0] is g.HISTORY_Q and c[3]["name"] == "farming")
                   or (c[0] == "REST" and "farming" in c[1])]
        self.assertEqual(farming, [])

    def test_commit_search_is_not_a_source(self):
        self.assertFalse(any(c[0] == "REST" and "/search/" in c[1] for c in self.gh.calls))

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
        data = {**self.data, "cc": fake_cc(), "my_commits": {"alpha": 7}, "total_commits": {"alpha": 19},
                "releases": [{"repo": "alpha", "tag": "v1", "at": when, "downloads": 5},
                             {"repo": g.PRIVATE, "tag": None, "at": when, "downloads": 2}]}
        config = {**g.CONFIG, "language_highlight": [], "more": [],
                  "featured": [{"repo": "alpha", "kicker": "k", "tagline": "t", "facts": ["f"]}]}
        with mock.patch.object(g, "CONFIG", config):
            out, stats = g.render_all(data)
            out["stats.json"] = g.export_json(data, *stats)
        project = json.loads(out["stats.json"])["projects"][0]
        self.assertEqual((project["commits"], project["total_commits"]), (7, 19))
        for theme in ("light", "dark"):
            self.assertIn("7 mine / 19 total", out[f"card-alpha-{theme}.svg"])
        with mock.patch("sys.stdout", new_callable=io.StringIO):
            g.leak_check(out, self.data["secrets"])          # exits on any private name


def fake_cc():
    days = [{"date": (dt.date(2026, 1, 4) + dt.timedelta(days=i)).isoformat(), "contributionCount": i % 3,
             "weekday": (i % 7)} for i in range(364)]
    return {"totalCommitContributions": 1, "totalIssueContributions": 0, "totalPullRequestContributions": 0,
            "totalPullRequestReviewContributions": 0, "restrictedContributionsCount": 0,
            "contributionCalendar": {"totalContributions": sum(d["contributionCount"] for d in days),
                                     "weeks": [{"contributionDays": days[i:i + 7]} for i in range(0, 364, 7)]}}


class FeaturedCommitTest(unittest.TestCase):
    def counts_payload(self, yours=7, total=19):
        return {"repository": {"defaultBranchRef": {"target": {
            "yours": {"totalCount": yours}, "total": {"totalCount": total}}}}}

    def test_counts_use_same_default_branch_with_only_personal_author_filter(self):
        self.assertIn("defaultBranchRef", g.ALLTIME_Q)
        self.assertIn("yours: history(first: 1, author: {id: $author})", g.ALLTIME_Q)
        self.assertIn("total: history(first: 1) { totalCount }", g.ALLTIME_Q)
        self.assertNotIn('ref(qualifiedName:', g.ALLTIME_Q)
        with mock.patch.object(g, "gql", return_value=self.counts_payload()) as query:
            self.assertEqual(g.repo_commit_counts("project", "ME"), (7, 19))
        query.assert_called_once_with(g.ALLTIME_Q, owner=g.USER, name="project", author="ME")

    def test_existing_personal_count_stays_personal(self):
        with mock.patch.object(g, "gql", return_value=self.counts_payload()):
            self.assertEqual(g.my_commit_count("project", "ME"), 7)

    def test_empty_or_missing_repository_has_zero_counts(self):
        for payload in ({"repository": None}, {"repository": {"defaultBranchRef": None}},
                        self.counts_payload(0, 0)):
            with mock.patch.object(g, "gql", return_value=payload):
                self.assertEqual(g.repo_commit_counts("empty", "ME"), (0, 0))

    def test_collection_adds_total_without_changing_existing_statistics(self):
        gh = FakeGitHub()
        config = {**g.CONFIG, "featured": [], "more": [], "exclude_repos": ["acidvegas/farming"]}
        with mock.patch.object(g, "gql", gh.gql), mock.patch.object(g, "rest", gh.rest), \
                mock.patch.object(g, "TOKEN", "test"), mock.patch("sys.stdout", new_callable=io.StringIO), \
                mock.patch.object(g, "CONFIG", config):
            before = g.collect()
            config["featured"] = [{"repo": "alpha"}]
            after = g.collect()
        self.assertEqual(after["my_commits"], {"alpha": 7})
        self.assertEqual(after["total_commits"], {"alpha": 19})
        for key in before.keys() - {"my_commits", "total_commits"}:
            self.assertEqual(before[key], after[key], key)
        calls = [c for c in gh.calls if c[0] is g.ALLTIME_Q]
        self.assertEqual(len(calls), 1)

    def test_featured_svg_displays_both_counts_without_footer_overlap(self):
        project = {"repo": "alpha", "kicker": "k", "tagline": "t", "facts": ["a", "b", "c"]}
        repo = repo_node("alpha")
        repo.update(primaryLanguage={"name": "C++", "color": "#f34b7d"}, stargazerCount=61, forkCount=4)
        data = {"repos": [repo], "my_commits": {"alpha": 171}, "total_commits": {"alpha": 320},
                "releases": [{"repo": "alpha", "tag": "v0.3.3", "at": g.NOW - dt.timedelta(days=2),
                              "downloads": 599}]}
        for theme in g.THEMES.values():
            with mock.patch.object(g, "T", theme):
                svg = g.card_featured(data, project)
            self.assertIn("171 mine / 320 total", svg)
            self.assertNotIn("yours", svg)
            root = ET.fromstring(svg)
            self.assertEqual(root.attrib["viewBox"], "0 0 840 222")
            footer = [e for e in root.iter() if e.tag.endswith("text") and e.attrib.get("y") == "201.0"
                      and e.attrib.get("text-anchor") != "middle"]
            previous_end = 24
            for e in footer:
                x = float(e.attrib["x"])
                if e.attrib.get("text-anchor") == "end":
                    # Leave room for the Latest pill to the left of the tag.
                    start = x - g.width_of(e.text, 13) - 10 - (g.width_of("Latest", 12) + 16)
                else:
                    start = x
                self.assertGreaterEqual(start, previous_end)
                previous_end = x + g.width_of(e.text, 13)


class PrivateExternalTest(unittest.TestCase):
    """Synthetic data only: no identifiers or code from a real private repo."""

    def collect_private(self, accessible=True, contributed=False, affiliated=True):
        gh = FakeGitHub()
        base = gh.gql
        project = {**repo_node("hidden-project"), **ext("outside-user", "hidden-project", True)}
        shas = [f"{i:040x}" for i in range(10, 16)]

        def gql(query, private=False, missing_ok=False, **v):
            if query is g.PRIVATE_Q:
                # An unrelated private project on page one; the external project
                # must be found on page two even if contribution discovery omits it.
                if v["after"] is None:
                    result = base(query, private=private, missing_ok=missing_ok, **v)
                    result["viewer"]["repositories"]["pageInfo"] = {
                        "hasNextPage": True, "endCursor": "private-next"}
                    return result
                return {"viewer": {"repositories": {
                    "nodes": [project] if accessible and affiliated else [],
                    "pageInfo": {"hasNextPage": False, "endCursor": None}}}}
            if query is g.EXTERNAL_Q and contributed:
                return {"user": {"repositoriesContributedTo": {
                    "nodes": [project], "pageInfo": {"hasNextPage": False}},
                    "contributionsCollection": {"commitContributionsByRepository": [
                        {"repository": project}]}}}
            if query is g.HISTORY_Q and v["name"] == project["name"]:
                gh.calls.append((query, private, missing_ok, v))
                if not accessible:
                    return {"repository": None}
                return {"repository": {"defaultBranchRef": {"target": {"allCommits": {"totalCount": len(shas)}, "history": {
                    "nodes": [{"oid": sha, "authoredDate": RECENT,
                               "author": {"user": {"id": "ME"}}} for sha in shas],
                    "pageInfo": {"hasNextPage": False}}}}}}
            return base(query, private=private, missing_ok=missing_ok, **v)

        def rest(path, private=False):
            if "hidden-project/commits/" in path:
                gh.calls.append(("REST", path, private))
                return {"parents": [{}], "files": [{"filename": "hidden-path.rs",
                    "status": "added", "patch": "+é"}]}
            return gh.rest(path, private=private)

        with mock.patch.object(g, "gql", gql), mock.patch.object(g, "rest", rest), \
                mock.patch.object(g, "TOKEN", "test"), \
                mock.patch.object(g, "CONFIG", {**g.CONFIG, "exclude_repos": ["acidvegas/farming"]}), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as logs:
            data = g.collect()
        return data, gh.calls, shas, logs.getvalue()

    def test_six_private_commits_from_affiliation_discovery_reach_panel(self):
        data, calls, shas, logs = self.collect_private()
        self.assertEqual(data["contrib_langs"]["Rust"], 12 + 6 * len("é\n".encode()))
        self.assertEqual(len([c for c in calls if c[0] == "REST" and "hidden-project" in c[1]]), 6)
        self.assertTrue(all(c[2] for c in calls if c[0] == "REST" and "hidden-project" in c[1]))
        self.assertTrue(set(shas).issubset(data["secrets"]))
        self.assertIn("hidden-path.rs", data["secrets"])
        self.assertNotIn("hidden", logs)
        svg, summary = g.panel_languages(data)
        shares = {name: pct for name, pct, _ in summary}
        self.assertAlmostEqual(shares["Rust"], round(100 * 30 / 2041, 1))
        self.assertIn("Zig", shares)
        with mock.patch("sys.stdout", new_callable=io.StringIO):
            g.leak_check({"languages.svg": svg}, data["secrets"])

    def test_private_repository_from_multiple_sources_is_measured_once(self):
        data, calls, _, _ = self.collect_private(contributed=True)
        self.assertEqual(data["contrib_langs"]["Rust"], 30)
        histories = [c for c in calls if c[0] is g.HISTORY_Q and c[3]["name"] == "hidden-project"]
        self.assertEqual(len(histories), 1)

    def test_private_contribution_discovery_can_supply_repository(self):
        data, _, _, _ = self.collect_private(contributed=True, affiliated=False)
        self.assertEqual(data["contrib_langs"]["Rust"], 30)

    def test_unreadable_private_repository_does_not_invent_contributions(self):
        data, calls, _, _ = self.collect_private(accessible=False, contributed=True)
        self.assertEqual(data["contrib_langs"]["Rust"], 12)
        self.assertFalse(any(c[0] == "REST" and "hidden-project" in c[1] for c in calls))

    def test_larger_raw_rust_count_can_still_round_to_same_percentage(self):
        data, _, _, _ = self.collect_private()
        data["repos"] = [repo_node("large")]
        data["repos"][0]["languages"]["edges"][0]["size"] = 982000
        data["private_langs"] = []
        data["contrib_langs"] = Counter({"Rust": 18000, "Zig": 1})
        _, before = g.panel_languages(data)
        data["contrib_langs"]["Rust"] += 18
        _, after = g.panel_languages(data)
        self.assertEqual(next(p for n, p, _ in before if n == "Rust"), 1.8)
        self.assertEqual(next(p for n, p, _ in after if n == "Rust"), 1.8)


class HelpersTest(unittest.TestCase):
    def test_private_discovery_includes_member_owned_repositories(self):
        self.assertIn("ownerAffiliations: [OWNER, COLLABORATOR, ORGANIZATION_MEMBER]", g.PRIVATE_Q)

    def test_history_requires_resolved_primary_account_and_paginates(self):
        nodes = [{"oid": str(i), "authoredDate": RECENT, "author": author}
                 for i, author in enumerate([
                     {"user": {"id": "ME"}}, {"user": {"id": "OTHER"}},
                     {"user": None}, None])]
        pages = [{"repository": {"defaultBranchRef": {"target": {"history": {
            "nodes": page, "pageInfo": {"hasNextPage": i == 0, "endCursor": "next"}}}}}}
                 for i, page in enumerate([nodes[:2], nodes[2:]])]
        with mock.patch.object(g, "gql", side_effect=pages) as query:
            commits = g.my_commits_in("sample", "ME", owner="someone", private=True)
        self.assertEqual([sha for sha, _ in commits], ["0"])
        self.assertEqual([c.kwargs["after"] for c in query.call_args_list], [None, "next"])
        self.assertTrue(all(c.kwargs["private"] for c in query.call_args_list))
        self.assertIn("defaultBranchRef", g.HISTORY_Q)

    def test_language_of(self):
        cases = {"a/b.cpp": "C++", "x.h": "C", "lib.rs": "Rust", "build.zig": "Zig", "CMakeLists.txt": "CMake",
                 "Dockerfile": "Dockerfile", "README.md": None, "data.json": None, "third_party/x.c": None,
                 "node_modules/a.js": None, "noext": None, ".hidden": None}
        for path, lang in cases.items():
            self.assertEqual(g.language_of(path), lang, path)

    def test_added_bytes_counts_only_added_lines(self):
        patch = "--- a/f\n+++ b/f\n@@ -1,2 +1,2 @@\n ctx\n-gone\n+new line\n+é"
        self.assertEqual(g.added_bytes(patch), len("new line") + 1 + len("é".encode()) + 1)

    def test_added_line_starting_with_two_pluses_is_code(self):
        self.assertEqual(g.added_bytes("+++ b/f\n@@ -0,0 +1 @@\n+++counter;"), len("++counter;\n"))
        self.assertEqual(g.added_bytes("+++ b/f\n@@ -0,0 +1 @@\n+++ counter;"), len("++ counter;\n"))

    def test_added_bytes_without_final_newline(self):
        patch = "@@ -1 +1 @@\n-old\n\\ No newline at end of file\n+é\n\\ No newline at end of file"
        self.assertEqual(g.added_bytes(patch), len("é".encode()))

    def test_source_line_separators_are_counted_as_source_bytes(self):
        source = 'let s = "a\vb\fc\x85d\u2028e\u2029f";'
        self.assertEqual(g.added_bytes("@@ -0,0 +1 @@\n+" + source), len(source.encode()) + 1)

    def test_unavailable_and_merge_diffs_never_estimate_bytes(self):
        for response, counter in [(None, "unavailable"),
                                  ({"parents": [{}, {}], "files": DIFFS["f1"]}, "merges"),
                                  ({"parents": [{}], "files": [{"filename": "big.rs"}]}, "unmeasured")]:
            stats = Counter()
            with mock.patch.object(g, "rest", return_value=response):
                self.assertEqual(g.commit_languages("o", "n", "sha", True, stats), Counter())
            self.assertEqual(stats[counter], 1)

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

    def test_external_discovery_errors_hide_private_metadata(self):
        body = {"errors": [{"type": "INTERNAL", "message": "hidden-owner/hidden-repo"}]}
        with self.payload(body), self.assertRaises(SystemExit) as cm:
            g.external_repos()
        self.assertNotIn("hidden", str(cm.exception))

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
