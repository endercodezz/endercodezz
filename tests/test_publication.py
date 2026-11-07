import datetime as dt
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import generate as g


STAMP = dt.datetime(2026, 11, 7, 21, 16, tzinfo=dt.timezone.utc)
README = ('Public copy\n<picture><source media="(prefers-color-scheme: dark)" srcset="assets/header-dark.svg">'
          '<source media="(prefers-color-scheme: light)" srcset="assets/header-light.svg">'
          '<img src="assets/header-dark.svg"></picture>\n'
          '<!-- numbers:start -->\nsafe numbers\n<!-- numbers:end -->\n')


def outputs():
    return {"header-dark.svg": '<svg>dark</svg>\n', "header-light.svg": '<svg>light</svg>\n',
            "stats.json": json.dumps({"generated": STAMP.isoformat()})}


class PublicationTest(unittest.TestCase):
    def test_full_content_hashes_update_all_src_and_srcset_and_preserve_themes(self):
        files, readme = g.prepare_publication(outputs(), README, STAMP)
        for theme in ("dark", "light"):
            content = outputs()[f"header-{theme}.svg"]
            name = f"header-{theme}.{hashlib.sha256(content.encode()).hexdigest()}.svg"
            self.assertIn(name, files)
            self.assertIn(f'srcset="assets/{name}"', readme)
            self.assertIn(f'(prefers-color-scheme: {theme})', readme)
        self.assertEqual(readme.count('header-dark.'), 2)
        self.assertNotIn('?v=', readme)
        self.assertIn('Last updated: November 7, 2026 at 21:16 UTC', readme)

    def test_refresh_only_never_changes_svg_identity(self):
        before, readme = g.prepare_publication(outputs(), README, STAMP)
        after, updated = g.prepare_publication(outputs(), readme, STAMP + dt.timedelta(minutes=5))
        self.assertEqual(before, after)
        self.assertIn('21:21 UTC', updated)
        self.assertEqual(updated.count('<!-- refresh:start -->'), 1)
        changed = outputs()
        changed['header-dark.svg'] += '<!-- changed -->'
        changed, _ = g.prepare_publication(changed, readme, STAMP)
        self.assertEqual({n for n in before if 'light' in n}, {n for n in changed if 'light' in n})
        self.assertNotEqual({n for n in before if 'dark' in n}, {n for n in changed if 'dark' in n})

    def test_query_versions_are_replaced_with_content_identity(self):
        _, readme = g.prepare_publication(outputs(), README.replace('.svg"', '.svg?v=old"'), STAMP)
        self.assertNotIn('?v=', readme)

    def test_missing_image_and_duplicate_markers_fail_before_publication(self):
        with self.assertRaises(SystemExit):
            g.prepare_publication({'header-dark.svg': 'dark'}, README, STAMP)
        duplicate = README + g.refresh_block(STAMP) + g.refresh_block(STAMP)
        with self.assertRaises(SystemExit):
            g.prepare_publication(outputs(), duplicate, STAMP)

    def test_privacy_allows_only_generated_urls_and_refresh_changes(self):
        files, readme = g.prepare_publication(outputs(), README, STAMP)
        with mock.patch('builtins.print'):
            g.leak_check({**files, 'README.md': readme}, {'private-project'}, README)
        with self.assertRaises(SystemExit):
            g.leak_check({'README.md': readme.replace('Public copy', 'private-project')}, {'private-project'}, README)
        with self.assertRaises(SystemExit):
            g.leak_check({'README.md': readme.replace('Last updated:', 'private-project')}, {'private-project'}, README)

    def test_cleanup_is_bounded_and_unchanged_images_are_not_rewritten(self):
        with tempfile.TemporaryDirectory(dir=g.ROOT, prefix='.test-publication-') as folder:
            root = Path(folder).resolve()
            self.assertTrue(root.is_relative_to(g.ROOT.resolve()))
            assets = root / 'assets'
            assets.mkdir()
            (root / 'README.md').write_text(README)
            (assets / 'header-dark.svg').write_text('old')
            (assets / ('header-dark.' + '0' * 64 + '.svg')).write_text('old hash')
            (assets / 'logo.svg').write_text('custom')
            files, readme = g.prepare_publication(outputs(), README, STAMP)
            with mock.patch.object(g, 'ROOT', root), mock.patch.object(g, 'ASSETS', assets):
                g.publish_outputs(files, readme)
                image = next(assets.glob('header-dark.*.svg'))
                before = image.stat().st_mtime_ns
                g.publish_outputs(files, readme)
                self.assertEqual(image.stat().st_mtime_ns, before)
            self.assertFalse((assets / 'header-dark.svg').exists())
            self.assertFalse((assets / ('header-dark.' + '0' * 64 + '.svg')).exists())
            self.assertEqual((assets / 'logo.svg').read_text(), 'custom')
            self.assertEqual(len(list(assets.glob('header-dark.*.svg'))), 1)

    def test_write_failure_restores_previous_timestamp_and_artifacts(self):
        with tempfile.TemporaryDirectory(dir=g.ROOT, prefix='.test-publication-') as folder:
            root = Path(folder).resolve()
            self.assertTrue(root.is_relative_to(g.ROOT.resolve()))
            assets = root / 'assets'
            assets.mkdir()
            (root / 'README.md').write_text(README)
            old = '{"generated":"previous-success"}'
            (assets / 'stats.json').write_text(old)
            (assets / 'header-dark.svg').write_text('previous SVG')
            files, readme = g.prepare_publication(outputs(), README, STAMP)
            replace = g.os.replace
            def fail_readme(src, dst):
                if Path(dst).name == 'README.md':
                    raise OSError('simulated publication failure')
                return replace(src, dst)
            with mock.patch.object(g, 'ROOT', root), mock.patch.object(g, 'ASSETS', assets), \
                    mock.patch.object(g.os, 'replace', side_effect=fail_readme), self.assertRaises(SystemExit):
                g.publish_outputs(files, readme)
            self.assertEqual((assets / 'stats.json').read_text(), old)
            self.assertEqual((root / 'README.md').read_text(), README)
            self.assertEqual((assets / 'header-dark.svg').read_text(), 'previous SVG')
            self.assertEqual(list(assets.glob('header-dark.*.svg')), [])

    def test_timestamp_converts_to_utc(self):
        local = STAMP.astimezone(dt.timezone(dt.timedelta(hours=2)))
        self.assertEqual(g.refresh_block(local), g.refresh_block(STAMP))


if __name__ == '__main__':
    unittest.main()
