import unittest

from helpers import TOOLS  # noqa: F401

import language
import source_text as st


class SourceTextTest(unittest.TestCase):
    def test_kotlin_comments_removed_strings_kept(self):
        src = 'val a = "http://x" // pg_advisory_lock\n/* outer /* nested */ still */ val b = 1\n' \
              'val q = """\n  SELECT now() -- sql\n"""\nval c = \'/\''
        code, lits = st.kotlin_scan(src)
        self.assertNotIn("pg_advisory_lock", code)
        self.assertNotIn("still", code)
        self.assertIn('"http://x"', code)
        self.assertIn("val b = 1", code)
        self.assertEqual(code.count("\n"), src.count("\n"))
        self.assertIn("http://x", lits)
        self.assertTrue(any("SELECT now()" in s for s in lits))

    def test_kotlin_template_with_quotes(self):
        code, lits = st.kotlin_scan('val s = "a ${map["k"]} b" // c\nval t = 2')
        self.assertNotIn("// c", code)
        self.assertIn("val t = 2", code)

    def test_sql_comments(self):
        out = st.strip_sql("SELECT 1; -- LOCK TABLE x\n/* CONFIRMED */ SELECT '--keep'")
        self.assertNotIn("LOCK TABLE", out)
        self.assertNotIn("CONFIRMED", out)
        self.assertIn("'--keep'", out)


class LanguageTest(unittest.TestCase):
    def test_strips_code_quote_url_path(self):
        text = "구현했습니다.\n```kotlin\nfun englishCodeHereLong() {}\n```\n`inlineCode` 를 보세요.\n" \
               "> quoted english line here\nhttps://example.com/x 와 src/main/kotlin/Foo.kt 참고"
        r = language.ratio(text, 5)
        self.assertEqual(r["latin"], 0)
        self.assertEqual(r["ratio"], 1.0)

    def test_english_turn_below_threshold(self):
        r = language.ratio("I implemented the hold API and all tests pass now. 좋아요", 20)
        self.assertLess(r["ratio"], 0.5)

    def test_korean_slash_prose_is_not_a_path(self):
        r = language.ratio("선점/확정 흐름과 마감/정리 배치를 구현했습니다", 5)
        self.assertEqual(r["hangul"], 20)
        self.assertEqual(r["ratio"], 1.0)
        r = language.ratio("설정은 src/main/resources/application.yml 에 넣었습니다", 5)
        self.assertEqual(r["latin"], 0)

    def test_short_turn_excluded(self):
        self.assertIsNone(language.ratio("완료", 20)["ratio"])
        self.assertIsNone(language.ratio("```\ncode only block that is long enough\n```", 20)["ratio"])


if __name__ == "__main__":
    unittest.main()
