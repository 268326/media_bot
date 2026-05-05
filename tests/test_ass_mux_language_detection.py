import unittest

from ass_mux_planner import infer_lang_raw_from_subtitle_name, parse_lang


class SubtitleLanguageDetectionTests(unittest.TestCase):
    def test_parse_lang_accepts_sc_tc_aliases(self):
        self.assertEqual(parse_lang('sc'), ('zh-Hans', '简中'))
        self.assertEqual(parse_lang('SC'), ('zh-Hans', '简中'))
        self.assertEqual(parse_lang('tc'), ('zh-Hant', '繁中'))
        self.assertEqual(parse_lang('TC'), ('zh-Hant', '繁中'))

    def test_infer_lang_detects_sc_tc_single_language_tokens_case_insensitive(self):
        cases = {
            'Show.01.sc.ass': 'chs',
            'Show.01.SC.ass': 'chs',
            'Show.01.tc.ass': 'cht',
            'Show.01.TC.ass': 'cht',
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(infer_lang_raw_from_subtitle_name(name, 'fallback'), expected)

    def test_infer_lang_detects_sc_tc_bilingual_tokens(self):
        cases = {
            'Show.01.sc&jp.ass': 'chs_jpn',
            'Show.01.SC+JPN.ass': 'chs_jpn',
            'Show.01.tc&jp.ass': 'cht_jpn',
            'Show.01.TC+ENG.ass': 'cht_eng',
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(infer_lang_raw_from_subtitle_name(name, 'fallback'), expected)


if __name__ == '__main__':
    unittest.main()
