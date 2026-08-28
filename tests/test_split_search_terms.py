from __future__ import annotations

import unittest

from app.services.mita_search import split_search_terms


class SplitSearchTermsTests(unittest.TestCase):
    def test_splits_space_comma_顿号_ampersand_and_slash(self) -> None:
        self.assertEqual(
            split_search_terms("行业政策, 财报、市场数据 & 监管 / 授信"),
            ["行业政策", "财报", "市场数据", "监管", "授信"],
        )

    def test_dedupes_and_ignores_empty(self) -> None:
        self.assertEqual(split_search_terms("  政策，政策 / / 财报  "), ["政策", "财报"])

    def test_splits_english_comma_and_slash(self) -> None:
        self.assertEqual(split_search_terms("航空,政策/监管"), ["航空", "政策", "监管"])
        self.assertEqual(split_search_terms(""), [])
        self.assertEqual(split_search_terms("   ,、&/  "), [])
