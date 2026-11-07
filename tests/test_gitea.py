"""Offline Gitea contract, accounting, privacy and failure regressions."""
import datetime as dt
import io
import json
import sys
import unittest
import urllib.error
from collections import Counter
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import generate as g
import gitea_stats as s
from test_generate import FakeGitHub, fake_cc

DATE = (g.NOW - dt.timedelta(days=2)).isoformat()


def commit(sha, author=7, merge=False):
    return {"sha": sha, "author": {"id": author} if author else None,
            "parents": [{}, {}] if merge else [{}],
            "commit": {"author": {"date": DATE}}, "files": [{"filename": "private-source.rs", "status": "added"}]}


def repo(name, owner="local-account", owner_id=7, **extra):
    return {"id": name, "full_name": owner + "/" + name, "name": name,
            "owner": {"id": owner_id, "login": owner}, "private": True, "fork": False,
            "description": "private description " + name, "default_branch": "development",
            "html_url": s.BASE + "/" + owner + "/" + name, "stars_count": 2, "forks_count": 1,
            **extra}


class FakeGitea:
    def __init__(self):
        self.repos = [repo("own-hidden"), repo("foreign-hidden", "other-private-owner", 9),
                      repo("mirror-hidden", original_url="https://github.com/endercodezz/alpha.git", mirror=True)]
        self.commits = {"own-hidden": [commit("own-sha"), commit("other-sha", 9), commit("merge-own-sha", merge=True)],
            "foreign-hidden": [commit("foreign-sha"), commit("someone-else-sha", 9),
                               commit("merge-sha", merge=True), commit("missing-sha"), commit("unlinked-sha", None)],
            "mirror-hidden": [commit("github-sha")]}
        self.calls = []

    def get(self, path, **kwargs):
        self.calls.append(path)
        if path == "/user":
            return {"id": 7, "login": "local-account", "email": "hidden@example.test"}
        name = path.split("/")[3]
        if path.endswith("/languages"):
            assert name != "foreign-hidden", "External repositories must never contribute full language sizes"
            return {"Rust": 5000 if name == "mirror-hidden" else 1000}
        sha = path.rsplit("/", 1)[1].removesuffix(".diff")
        if path.endswith(".diff"):
            if sha == "missing-sha":
                return None
            assert sha == "foreign-sha", "Another contributor or merge must never supply bytes"
            return "diff --git a/private-source.rs b/private-source.rs\n--- /dev/null\n+++ b/private-source.rs\n@@ -0,0 +1 @@\n+é\n"
        return next(c for commits in self.commits.values() for c in commits if c["sha"] == sha)

    def pages(self, path, field=None):
        self.calls.append(path)
        if path.startswith("/repos/search?"):
            assert "private=true" in path and "is_private=true" in path
            yield from self.repos + [self.repos[0]]  # Duplicate discovery must be harmless.
            return
        name = path.split("/")[3]
        if "/commits?" in path:
            assert "sha=development" in path
            yield from self.commits[name]
        elif path.endswith("/releases"):
            yield {"draft": False, "published_at": DATE, "name": "private-release-" + name,
                   "tag_name": "private-tag", "assets": [{"name": "private-binary", "download_count": 4}]}
            yield {"draft": True, "published_at": DATE, "assets": []}
        elif path.endswith("/push_mirrors"):
            yield {"remote_address": "https://github.com/endercodezz/alpha.git"}
        else:
            raise AssertionError("Unexpected endpoint")


class GiteaTest(unittest.TestCase):
    def setUp(self):
        self.api = FakeGitea()
        self.secrets = set()

    def collect(self, **kwargs):
        return s.collect("secret", {"endercodezz/alpha": None}, {"github-sha"}, {"github-sha"},
                         g.language_of, g.added_bytes, self.secrets, api=self.api, mirrors=kwargs.pop("mirrors", {}), **kwargs)

    def test_own_full_sizes_external_own_diffs_and_provider_audit(self):
        data = self.collect()
        self.assertEqual(data["languages"], Counter(Rust=1003))
        self.assertEqual(data["raw"]["language_bytes"], {"Rust": 6003})
        self.assertEqual(data["included"]["total_repository_commits"], 8)
        self.assertEqual(data["included"]["personal_commits"], 5)
        self.assertEqual(data["included"]["external_personal_commits"], 3)
        self.assertEqual(data["mirrors_excluded"], 1)
        self.assertEqual(data["included"]["repositories"], 2)
        self.assertEqual(data["included"]["activity"], {DATE[:10]: 5})
        self.assertEqual(data["diagnostics"], {"merges_skipped": 1, "unavailable_diffs": 1, "unmeasured_files": 0})
        self.assertEqual(len(data["releases"]), 2)
        self.assertTrue(all(r["repo"] == g.PRIVATE and r["tag"] is None for r in data["releases"]))

    def test_explicit_mapping_works_when_mirror_names_differ(self):
        self.api.repos[2].pop("original_url")
        self.api.repos[2]["mirror"] = False
        data = self.collect(mirrors={"local-account/mirror-hidden": "github:endercodezz/alpha"})
        self.assertEqual(data["included"]["language_bytes"], {"Rust": 1003})

    def test_public_canonical_name_collision_preserves_private_mirror_identity(self):
        mirror = self.api.repos[2]
        mirror["name"] = "alpha"
        mirror["full_name"] = "local-account/alpha"
        mirror["description"] = "known public description"
        self.api.commits["alpha"] = self.api.commits["mirror-hidden"]
        self.collect(github_public={"endercodezz/alpha": {"alpha", "known public description"}})
        self.assertNotIn("alpha", self.secrets)
        self.assertNotIn("known public description", self.secrets)
        self.assertIn("local-account/alpha", self.secrets)
        self.assertIn(mirror["html_url"], self.secrets)
        with mock.patch("sys.stdout", new_callable=io.StringIO):
            g.leak_check({"stats.json": '{"github_project":"alpha"}'}, self.secrets)
        with self.assertRaises(SystemExit):
            g.leak_check({"stats.json": mirror["html_url"]}, self.secrets)

    def test_public_mirror_exception_never_relaxes_existing_github_private_guards(self):
        self.secrets.add("alpha")
        mirror = self.api.repos[2]
        mirror["name"] = "alpha"
        self.collect(github_public={"endercodezz/alpha": {"alpha"}})
        self.assertIn("alpha", self.secrets)

    def test_push_mirror_metadata_identifies_repository(self):
        self.api.repos[2].pop("original_url")
        self.api.repos[2]["mirror"] = False
        self.api.repos[2]["permissions"] = {"admin": True}
        self.assertEqual(self.collect()["mirrors_excluded"], 1)

    def test_gitea_to_gitea_mirror_deduplicates_full_sizes_and_releases(self):
        self.api.repos = [self.api.repos[0], self.api.repos[2]]
        self.api.repos[1]["original_url"] = s.BASE + "/local-account/own-hidden.git"
        self.api.commits["mirror-hidden"] = [commit("own-sha")]
        data = self.collect()
        self.assertEqual(data["included"]["repositories"], 1)
        self.assertEqual(data["languages"]["Rust"], 1000)
        self.assertEqual(len(data["releases"]), 1)

    def test_own_fork_is_measured_as_external_code(self):
        self.api.repos = [repo("foreign-hidden", fork=True)]
        self.assertEqual(self.collect()["languages"]["Rust"], 3)

    def test_shared_history_without_repository_identity_fails_closed(self):
        self.api.repos[2].pop("original_url")
        self.api.repos[2]["mirror"] = False
        with self.assertRaisesRegex(SystemExit, "shared history requires GITEA_MIRRORS"):
            self.collect()

    def test_unavailable_canonical_repository_and_cycles_fail_closed(self):
        for mappings in ({"local-account/mirror-hidden": "github:absent/repo"},
                         {"local-account/mirror-hidden": "gitea:local-account/mirror-hidden"}):
            with self.assertRaises(SystemExit):
                self.collect(mirrors=mappings)

    def test_empty_repository_has_zero_commits_and_no_language_query(self):
        self.api.repos = [repo("empty-hidden", empty=True, has_releases=False)]
        data = self.collect()
        self.assertEqual(data["raw"]["total_repository_commits"], 0)
        self.assertEqual(data["languages"], Counter())
        self.assertEqual(self.api.calls, ["/user", "/repos/search?private=true&is_private=true&sort=id&order=asc"])

    def test_missing_token_is_explicit_and_does_not_query(self):
        data = s.collect(None, {}, set(), set(), g.language_of, g.added_bytes, set(), api=self.api)
        self.assertEqual(data["status"], "missing_token")
        self.assertIsNone(data["raw"])
        self.assertEqual(self.api.calls, [])
        scope = g.provider_scope({"providers": {"gitea": {"status": data["status"]}}})
        self.assertIn("GitHub only", scope)
        self.assertIn("Gitea token missing", scope)

    def test_full_integration_and_public_output_is_aggregate_only(self):
        gh = FakeGitHub()
        with mock.patch.object(g, "gql", gh.gql), mock.patch.object(g, "rest", gh.rest), \
                mock.patch.object(g, "TOKEN", "test"), mock.patch.object(g, "GITEA_TOKEN", "private-token-value"), \
                mock.patch.object(s, "API", return_value=self.api), \
                mock.patch.dict("os.environ", {"GITEA_MIRRORS": "{}"}), \
                mock.patch.object(g, "CONFIG", {**g.CONFIG, "exclude_repos": ["acidvegas/farming"]}), \
                mock.patch("sys.stdout", new_callable=io.StringIO):
            data = g.collect()
        self.assertEqual(data["providers"]["gitea"]["status"], "authenticated")
        self.assertEqual(data["providers"]["github"]["personal_commits"], 10)
        self.assertEqual(len(data["times"]), 14)  # 9 recent GH commits + 5 Gitea commits.
        self.assertEqual(data["contrib_langs"]["Rust"], 1015)
        data["cc"] = fake_cc()
        with mock.patch.object(g, "CONFIG", {**g.CONFIG, "featured": [], "more": []}):
            out, stats = g.render_all(data)
            out["stats.json"] = g.export_json(data, *stats)
            out["README.md"] = g.numbers_block(*stats, data["providers"])
        self.assertIn("GitHub + Gitea", out["header-dark.svg"])
        with mock.patch("sys.stdout", new_callable=io.StringIO):
            g.leak_check(out, data["secrets"])
        public = json.loads(out["stats.json"])
        self.assertEqual(public["contributions"]["total"], fake_cc()["contributionCalendar"]["totalContributions"])
        self.assertEqual(public["providers"]["gitea"]["included"]["language_bytes"], {"Rust": 1003})

    def test_privacy_check_catches_gitea_token_and_identifiers(self):
        self.collect()
        for secret in ("private-source.rs", "foreign-sha", "private-binary", "own-hidden"):
            self.assertIn(secret, self.secrets)
            with self.assertRaises(SystemExit):
                g.leak_check({"a.svg": secret}, self.secrets)
        with mock.patch.object(g, "GITEA_TOKEN", "abcdef0123456789"), self.assertRaises(SystemExit):
            g.leak_check({"a.svg": "abcdef0123456789"}, set())

    def test_diff_hunks_missing_binary_quoted_paths_and_unicode(self):
        patch = self.api.get("/repos/x/y/git/commits/foreign-sha.diff")
        for raw, expected in ((patch, 3), (patch.replace("+1 @@", "+1,2 @@"), 0),
                              (patch.replace("+++ b/private-source.rs", '+++ "b/private-source.rs"'), 0),
                              ("Binary files differ", 0)):
            stats = Counter()
            result = s.diff_languages(raw, [{"filename": "private-source.rs", "status": "added"}],
                                      g.language_of, g.added_bytes, set(), stats)
            self.assertEqual(result["Rust"], expected)
            self.assertEqual(stats["unmeasured_files"], int(expected == 0))


class ApiTest(unittest.TestCase):
    def test_existing_public_readme_prose_is_preserved_but_new_private_values_fail(self):
        before = 'Public framework\n<!-- numbers:start -->\nold\n<!-- numbers:end -->\n'
        after = before.replace('old\n', '123 commits\n')
        with mock.patch("sys.stdout", new_callable=io.StringIO):
            g.leak_check({"README.md": after}, {"framework"}, public_readme=before)
        with self.assertRaises(SystemExit):
            g.leak_check({"README.md": after.replace('123 commits', 'private-project')},
                         {"private-project"}, public_readme=before)
        with self.assertRaisesRegex(SystemExit, 'outside the generated block'):
            g.leak_check({"README.md": after + 'private-project'}, {"private-project"}, public_readme=before)
        with mock.patch.object(g, "GITEA_TOKEN", "token-value"), self.assertRaises(SystemExit):
            g.leak_check({"README.md": after.replace('Public', 'token-value')}, set(), public_readme=before)

    def test_aggregate_schema_language_keys_and_numbers_are_not_repo_metadata(self):
        public = {"providers": {"gitea": {"status": "authenticated", "raw": {
            "repositories": 352, "personal_commits": 352,
            "language_bytes": {"Nix": 123, "Rust": 456}, "activity": {"2026-11-07": 352}}}}}
        with mock.patch("sys.stdout", new_callable=io.StringIO):
            g.leak_check({"stats.json": json.dumps(public)}, {"repositories", "352", "Nix", "authenticated"})

    def test_private_json_values_and_unexpected_keys_remain_protected(self):
        for value in ({"projects": [{"name": "hidden-project"}]},
                      {"providers": {"gitea": {"hidden-project": 1}}},
                      {"providers": {"gitea": {"raw": {"language_bytes": {"hidden-owner/project": 10}}}}}):
            with self.assertRaises(SystemExit):
                g.leak_check({"stats.json": json.dumps(value)}, {"hidden-project", "hidden-owner/project"})

    def test_failed_collection_never_writes_public_artifacts(self):
        with mock.patch.object(g, "TOKEN", "test"), \
                mock.patch.object(g, "collect", side_effect=SystemExit("Gitea authentication/access failed")), \
                mock.patch.object(Path, "write_text") as write, self.assertRaises(SystemExit):
            g.main()
        write.assert_not_called()

    def test_authentication_failure_hides_private_response_and_prevents_writes(self):
        api = s.API("private-token")
        error = urllib.error.HTTPError("private-repo-url", 401, "private-body", {}, None)
        with mock.patch.object(api.opener, "open", side_effect=error), self.assertRaises(SystemExit) as cm:
            api.get("/user")
        self.assertIn("authentication/access failed", str(cm.exception))
        self.assertNotIn("private", str(cm.exception))

    def test_pagination_continues_even_when_server_caps_page_size(self):
        api = s.API("t")
        with mock.patch.object(api, "get", side_effect=[[1], [2], []]) as get:
            self.assertEqual(list(api.pages("/user/repos")), [1, 2])
        self.assertEqual(get.call_count, 3)

    def test_authorization_is_header_only_and_redirects_are_disabled(self):
        api = s.API("private-token")
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"id":7}'
        with mock.patch.object(api.opener, "open", return_value=response) as open_:
            self.assertEqual(api.get("/user"), {"id": 7})
        request = open_.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "token private-token")
        self.assertNotIn("private-token", request.full_url)
        self.assertIsNone(s.NoRedirect().redirect_request(None, None, None, None, None, None))


if __name__ == "__main__":
    unittest.main()
