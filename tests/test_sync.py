from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import book_sync  # noqa: E402
import boox_collections  # noqa: E402
import reading_progress_sync  # noqa: E402


class TitleMatchingTests(unittest.TestCase):
    def test_normalizes_punctuation_and_width(self):
        self.assertEqual(book_sync.normalized("Ａ Book：Test.epub"), "abooktest")

    def test_exact_core_title_scores_highly(self):
        score = book_sync.title_score("Designing Data-Intensive Applications", "Designing Data-Intensive Applications (EPUB)")
        self.assertGreaterEqual(score, 0.94)

    def test_serial_issues_do_not_cross_match(self):
        score = book_sync.title_score("Example Weekly Vol. 12", "Example Weekly Vol. 13")
        self.assertEqual(score, 0.0)

    def test_collection_matcher_uses_same_rules(self):
        left, right = "Example Book", "Example Book (EPUB)"
        self.assertEqual(boox_collections.title_score(left, right), book_sync.title_score(left, right))


class ProviderParsingTests(unittest.TestCase):
    def test_provider_row_keeps_commas_in_title(self):
        output = "Row: 0 id=7, title=One, Two, nativeAbsolutePath=/sdcard/Books/a.epub, status=0\n"
        rows = boox_collections.parse_rows(output, ("id", "title", "nativeAbsolutePath", "status"))
        self.assertEqual(rows[0]["title"], "One, Two")
        self.assertEqual(rows[0]["nativeAbsolutePath"], "/sdcard/Books/a.epub")


class ProgressTests(unittest.TestCase):
    def test_parses_boox_progress(self):
        current, total, ratio = reading_progress_sync.parse_boox_progress("25/100")
        self.assertEqual((current, total), (25, 100))
        self.assertEqual(ratio, 0.25)

    def test_invalid_progress_is_zero(self):
        self.assertEqual(reading_progress_sync.parse_boox_progress("bad"), (0, 0, 0.0))

    def test_progress_is_clamped(self):
        self.assertEqual(reading_progress_sync.parse_boox_progress("120/100")[2], 1.0)

    def test_union_find_connects_aliases(self):
        union = reading_progress_sync.UnionFind()
        union.union(("apple", 1), ("boox", "/a.epub"))
        union.union(("apple", 2), ("boox", "/a.epub"))
        self.assertEqual(union.find(("apple", 1)), union.find(("apple", 2)))


if __name__ == "__main__":
    unittest.main()
