"""Combined activity uses event counts, never fabricated GitHub contributions."""
import copy
import datetime as dt
import io
import json
import sys
import unittest
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import generate as g
from test_generate import FakeGitHub
from test_gitea import FakeGitea, commit


def when(value):
    return dt.datetime.fromisoformat(value)


def fixture():
    start = dt.date(2026, 9, 29)
    github_counts = {"2026-09-29": 3, "2026-11-04": 2, "2026-11-05": 4}
    days = [{"date": (start + dt.timedelta(days=i)).isoformat(),
             "weekday": ((start + dt.timedelta(days=i)).weekday() + 1) % 7,
             "contributionCount": github_counts.get((start + dt.timedelta(days=i)).isoformat(), 0)}
            for i in range(40)]
    weeks = []
    for day in days:
        if not weeks or day["weekday"] == 0:
            weeks.append({"contributionDays": []})
        weeks[-1]["contributionDays"].append(day)
    github = {"shared-sha": when("2026-11-04T12:00:00+00:00"),
              "another-github-sha": when("2026-11-05T12:00:00+00:00")}
    gitea = {"shared-sha": when("2026-11-03T12:00:00+00:00"),
             "private-only-sha": when("2026-11-03T12:00:00+00:00"),
             "private-offset-sha": when("2026-11-04T00:30:00+02:00"),
             "private-same-day-sha": when("2026-11-05T12:00:00+00:00"),
             "private-same-day-sha-2": when("2026-11-05T13:00:00+00:00")}
    return {"cc": {"contributionCalendar": {"totalContributions": 9, "weeks": weeks},
                   "restrictedContributionsCount": 1, "totalPullRequestContributions": 2,
                   "totalIssueContributions": 3, "totalCommitContributions": 4,
                   "totalPullRequestReviewContributions": 0},
            "providers": {"gitea": {"status": "authenticated"}},
            "additional_gitea_activity": g.additional_gitea_activity(gitea, github),
            "times": list({**gitea, **github}.values()), "has_private": True,
            "repos": [], "my_commits": {}, "private_langs": [], "contrib_langs": Counter(),
            "releases": [{"repo": g.PRIVATE, "tag": None, "at": when("2026-11-01T12:00:00+00:00"), "downloads": 0}]}


class ActivityTest(unittest.TestCase):
    def test_sha_deduplication_precedes_date_aggregation_and_uses_utc(self):
        data = fixture()
        self.assertEqual(data["additional_gitea_activity"], {"2026-11-03": 2, "2026-11-05": 2})
        # The shared SHA has a different date on Gitea. It must not add an event
        # there, and two different commits on the same day must both survive.
        self.assertEqual(sum(data["additional_gitea_activity"].values()), 4)
        self.assertEqual(len(data["times"]), 6)

    def test_gitea_only_and_shared_days_recalculate_all_daily_statistics(self):
        data = fixture()
        before = copy.deepcopy(data["cc"])
        activity = g.development_activity(data)
        by_date = {day["date"]: day for day in activity["days"]}
        self.assertEqual(by_date["2026-11-03"]["contributionCount"], 2)
        self.assertEqual(by_date["2026-11-05"]["contributionCount"], 6)
        self.assertEqual(activity["total"], 13)
        self.assertEqual(activity["active"], 4)
        self.assertEqual(activity["github_active"], 3)
        self.assertEqual(activity["peak"]["date"], "2026-11-05")
        self.assertEqual(activity["last30_total"], 10)
        self.assertEqual(activity["last30_share"], 77)
        self.assertEqual(data["cc"], before)

    def test_original_github_total_is_not_used_as_combined_event_count(self):
        data = fixture()
        data["cc"]["contributionCalendar"]["totalContributions"] = 99
        _, summary = g.panel_activity(data)
        self.assertEqual(summary["total"], 99)
        self.assertEqual(summary["activity"]["total"], 13)

    def test_only_dates_in_original_calendar_are_included(self):
        data = fixture()
        data["additional_gitea_activity"].update({"2026-09-28": 50, "2026-11-08": 50})
        self.assertEqual(g.development_activity(data)["total"], 13)

    def test_missing_gitea_token_ignores_stale_extra_counts_and_labels_github_only(self):
        data = fixture()
        data["providers"]["gitea"]["status"] = "missing_token"
        svg, summary = g.panel_activity(data)
        self.assertEqual(summary["activity"]["source"], "GitHub")
        self.assertEqual(summary["activity"]["total"], 9)
        self.assertEqual(summary["active"], 3)
        self.assertEqual(summary["peak"]["contributionCount"], 4)
        self.assertIn("GitHub only", svg)
        self.assertNotIn("GitHub + Gitea", svg)

    def test_authenticated_provider_without_deduplicated_series_fails_closed(self):
        data = fixture()
        del data["additional_gitea_activity"]
        with self.assertRaisesRegex(SystemExit, "Deduplicated Gitea activity unavailable"):
            g.panel_activity(data)

    def test_svg_labels_heatmap_levels_and_layout(self):
        data = fixture()
        for theme in g.THEMES.values():
            with mock.patch.object(g, "T", theme):
                svg, _ = g.panel_activity(data)
            self.assertIn("Development activity", svg)
            self.assertIn("unique commits · GitHub + Gitea", svg)
            self.assertIn("GitHub contributions", svg)
            self.assertIn("GitHub public pull requests", svg)
            self.assertIn("GitHub public issues", svg)
            self.assertNotIn("commits, both hosts", svg)
            root = ET.fromstring(svg)
            cells = [e for e in root.iter() if e.tag.endswith("rect") and e.get("width") == "11"]
            self.assertEqual(len(cells), 40)
            self.assertEqual(sum(e.get("fill") != theme["heat"][0] for e in cells), 4)
            for row in (120.0, 184.0):
                labels = [e for e in root.iter() if e.tag.endswith("text") and float(e.get("y")) == row]
                self.assertEqual(len(labels), 3)
                for label in labels:
                    self.assertLess(g.width_of(label.text, 12.5), (g.W - 40) / 3 - 16)

    def test_export_preserves_original_calendar_and_adds_combined_activity(self):
        data = fixture()
        with mock.patch.object(g, "CONFIG", {**g.CONFIG, "featured": [], "more": []}):
            out, stats = g.render_all(data)
            payload = json.loads(g.export_json(data, *stats))
            readme = g.numbers_block(*stats, data["providers"])
        self.assertEqual(payload["contributions"]["total"], 9)
        self.assertEqual(payload["contributions"]["active_days"], 3)
        self.assertEqual(payload["github_calendar"]["total"], 9)
        self.assertEqual(payload["github_calendar"]["counts"], payload["contributions"]["counts"])
        self.assertEqual(payload["activity"]["total"], 13)
        self.assertEqual(payload["activity"]["gitea_commits"], 4)
        self.assertEqual(payload["activity"]["active_days"], 4)
        self.assertEqual(payload["activity"]["busiest_day"], {
            "date": "2026-11-05", "count": 6, "github_contributions": 4, "gitea_commits": 2})
        self.assertIn("13 events on 4 active days", readme)
        self.assertIn("9 GitHub contributions", readme)
        out.update({"stats.json": json.dumps(payload), "README.md": readme})
        with mock.patch("sys.stdout", new_callable=io.StringIO):
            g.leak_check(out, {"private-only-sha", "private-offset-sha", "private-project"})

    def test_empty_activity_has_no_invented_busiest_date(self):
        data = fixture()
        data["additional_gitea_activity"] = {}
        data["cc"]["contributionCalendar"]["totalContributions"] = 0
        for week in data["cc"]["contributionCalendar"]["weeks"]:
            for day in week["contributionDays"]:
                day["contributionCount"] = 0
        _, summary = g.panel_activity(data)
        self.assertIsNone(summary["peak"])
        self.assertEqual(summary["active"], 0)

    def test_collection_excludes_github_sha_even_when_gitea_date_differs(self):
        gh = FakeGitHub()
        gitea = {"status": "authenticated", "raw": {"repositories": 2, "personal_commits": 2},
                 "included": {"repositories": 2}, "mirrors_excluded": 0,
                 "times": {"a1": when("2026-11-03T12:00:00+00:00"),
                           "gitea-only": when("2026-11-03T12:00:00+00:00")},
                 "own_shas": set(), "languages": Counter(), "releases": []}
        with mock.patch.object(g, "gql", gh.gql), mock.patch.object(g, "rest", gh.rest), \
                mock.patch.object(g, "TOKEN", "test"), mock.patch.object(g.gitea_stats, "collect", return_value=gitea), \
                mock.patch.object(g, "CONFIG", {**g.CONFIG, "exclude_repos": ["acidvegas/farming"]}), \
                mock.patch("sys.stdout", new_callable=io.StringIO):
            data = g.collect()
        self.assertEqual(data["additional_gitea_activity"], {"2026-11-03": 1})

    def test_mirror_only_commit_is_excluded_before_daily_aggregation(self):
        api = FakeGitea()
        api.commits["mirror-hidden"] = [commit("mirror-only-private-sha")]
        result = g.gitea_stats.collect("test", {"endercodezz/alpha": None}, set(), set(),
            g.language_of, g.added_bytes, set(), api=api, mirrors={})
        self.assertEqual(result["mirrors_excluded"], 1)
        self.assertNotIn("mirror-only-private-sha", result["times"])
        self.assertEqual(sum(g.additional_gitea_activity(result["times"], {}).values()), 5)


if __name__ == "__main__":
    unittest.main()
