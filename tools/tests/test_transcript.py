import tempfile
import unittest
from pathlib import Path

from helpers import assistant, boundary, summary, tool_result, user, write_jsonl

import transcript


class TranscriptTest(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())

    def load(self, rows):
        write_jsonl(self.d / "t.jsonl", rows)
        return transcript.load(self.d / "t.jsonl")[0]

    def test_bad_line_counted_not_dropped(self):
        (self.d / "t.jsonl").write_text('{"type":"user"}\nnot json\n', encoding="utf-8")
        entries, bad = transcript.load(self.d / "t.jsonl")
        self.assertEqual((len(entries), bad), (2, 1))
        self.assertIsNone(entries[1])

    def test_user_text_excludes_tool_result_summary_meta(self):
        self.assertEqual(transcript.user_text(user("안녕")), "안녕")
        self.assertIsNone(transcript.user_text(tool_result()))
        self.assertIsNone(transcript.user_text(summary()))
        self.assertIsNone(transcript.user_text(user("x", isMeta=True)))

    def test_boundaries_before_prompt_and_mid_turn(self):
        msgs = ["M0", "M1", "M2"]
        rows = [user("M0"), assistant("a0"),                         # 턴 0: 줄 0..1
                boundary(), summary(), user("M1"), assistant("a1"),  # 턴 1: 경계가 프롬프트 앞
                user("M2"), assistant(tools=[("Bash", {"command": "ls"})]), tool_result(),
                boundary(pre=90000), summary(), assistant("a2")]     # 턴 2: 도중
        entries = self.load(rows)
        b = transcript.locate_boundaries(entries, [2, 6, 12], msgs)
        self.assertEqual([(x["turn"], x["position"]) for x in b], [(1, "before_prompt"), (2, "mid_turn")])
        self.assertTrue(all(x["has_summary"] for x in b))
        self.assertEqual(b[1]["pre_tokens"], 90000)
        self.assertEqual([transcript.boundary_coord(x) for x in b], [0.5, 2.0])

    def test_boundary_unknown_when_prompt_missing(self):
        entries = self.load([boundary(), assistant("x")])
        b = transcript.locate_boundaries(entries, [2], ["안 보낸 메시지"])
        self.assertEqual(b[0]["position"], "unknown")
        self.assertEqual(transcript.boundary_coord(b[0]), 0.0)

    def test_assistant_text_dedupes_same_message_block(self):
        a = assistant("한국어 응답", mid="x")
        entries = self.load([a, a, assistant("둘째", mid="y")])
        self.assertEqual(transcript.assistant_text(entries), "한국어 응답\n\n둘째")

    def test_tool_uses(self):
        entries = self.load([assistant(tools=[("Read", {"file_path": "/a"}), ("WebFetch", {"url": "u"})])])
        self.assertEqual([n for _, n, _ in transcript.tool_uses(entries)], ["Read", "WebFetch"])


if __name__ == "__main__":
    unittest.main()
