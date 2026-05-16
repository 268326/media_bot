import tempfile
import unittest
from pathlib import Path

from ass_mux_config import AssMuxSettings
from ass_mux_planner import build_manual_mux_plan, build_mux_plan


class AssMuxPlanSmokeTests(unittest.TestCase):
    def _settings(self, root: Path, recursive: bool = True) -> AssMuxSettings:
        return AssMuxSettings(
            target_dir=root,
            tmp_dir=root / '.tmp',
            plan_path=root / '.plan.json',
            recursive=recursive,
            jobs=2,
            default_lang='chs',
            default_group='TestGroup',
            delete_external_subs_default=False,
            allow_cross_fs=False,
            notify_chat_id='',
            mkvmerge_bin='mkvmerge',
        )

    def test_auto_plan_matches_same_directory_subtitles(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            season = root / 'Show'
            season.mkdir(parents=True)
            (season / 'Demo.Show.S01E01.1080p.mkv').write_bytes(b'')
            (season / 'Demo.Show.S01E01.chs.ass').write_text('dummy', encoding='utf-8')
            (season / 'Demo.Show.S01E01.sc.ass').write_text('dummy', encoding='utf-8')
            settings = self._settings(root, recursive=True)

            plan = build_mux_plan(settings)

            self.assertEqual(plan.total_mkvs, 1)
            self.assertEqual(plan.matched_mkvs, 1)
            self.assertEqual(plan.total_sub_tracks, 2)
            self.assertEqual(len(plan.items), 1)
            self.assertEqual(len(plan.items[0].subs), 2)

    def test_manual_plan_lists_all_mkvs_without_subtitles(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / 'A').mkdir()
            (root / 'B').mkdir()
            (root / 'A' / 'ep01.mkv').write_bytes(b'')
            (root / 'B' / 'ep02.mkv').write_bytes(b'')
            (root / 'subs.ass').write_text('dummy', encoding='utf-8')
            settings = self._settings(root, recursive=True)

            plan = build_manual_mux_plan(settings)

            self.assertEqual(plan.total_mkvs, 2)
            self.assertEqual(plan.matched_mkvs, 0)
            self.assertEqual(plan.total_sub_tracks, 0)
            self.assertEqual(len(plan.items), 2)
            self.assertTrue(all(not item.subs for item in plan.items))


if __name__ == '__main__':
    unittest.main()
