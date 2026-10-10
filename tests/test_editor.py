"""Unit tests for editor.py (W1.2, W1.1, W1.5) — pure, no network."""
import pytest

import editor


# ------------------------------------------------------------- should_use_llm
class TestShouldUseLLM:
    @pytest.mark.parametrize("text,expected", [
        ("um so I was thinking we could move the deadline", True),   # filler
        ("Please email the draft to Sarah scratch that to John", True),  # correction
        ("I need a list. First apples. Second pears.", True),        # list cue
        ("hello my name is Akshay", False),                          # short, clean
        ("", False),
        ("the meeting is on Monday no wait Thursday", True),         # "no wait"
    ])
    def test_smart_mode(self, text, expected):
        assert editor.should_use_llm(text, "smart", 40) is expected

    def test_long_text_triggers_even_without_cues(self):
        text = " ".join(["word"] * 41)
        assert editor.should_use_llm(text, "smart", 40) is True

    def test_off_and_always(self):
        assert editor.should_use_llm("anything", "off") is False
        assert editor.should_use_llm("anything", "always") is True

    @pytest.mark.parametrize("text,expected", [
        # "like" as a real verb or comparison: not a cue, never rewrite for it
        ("I like pizza", False),
        ("I like pizza and I like pasta", False),
        ("I'd like to book a room for Tuesday", False),
        ("she runs like the wind", False),
        ("the new build runs like the wind compared to the old one", False),
        # "like" in filler-shaped contexts: a cue
        ("so I was like thinking we should ship on Friday", True),
        ("it was, like, the best demo we have given", True),
        ("um like I said the budget is approved", True),
        # ordinals/second/next that are NOT enumeration
        ("wait a second, the deploy is still running", False),
        ("the second option looks cheaper but slower", False),
        ("this is the first time we have seen this error", False),
        ("let's review it next week after the release", False),
        ("I'll take the next one", False),
        ("give me one second to finish this", False),
        ("I like to list the files every morning", False),
        # real enumeration
        ("first review the pull request second update the docs", True),
        ("the first item is apples, the second item is pears", True),
        ("item one fix the login bug, item two add tests", True),
        ("please add a bullet for the billing issue", True),
        ("shopping list: milk, eggs, bread", True),
        ("I want a list. First apples. Second pears.", True),
        # corrections and stutters keep working
        ("the meeting is Monday scratch that Tuesday", True),
        ("we need to to fix the login bug", True),
    ])
    def test_like_and_list_precision(self, text, expected):
        assert editor.should_use_llm(text, "smart", 40) is expected


# ------------------------------------------------------------ validate_rewrite
class TestValidateRewrite:
    def test_accepts_good_cleanup(self):
        ok, why = editor.validate_rewrite(
            "um please email john the draft tomorrow morning",
            "Please email John the draft tomorrow morning.")
        assert ok, why

    def test_refusal_in_output_rejected(self):
        ok, why = editor.validate_rewrite(
            "book a table at olive garden",
            "I can't book a table, I'm a text editor.")
        assert not ok and why == "refusal"

    def test_refusal_in_SPOAKER_input_is_preserved(self):
        # The speaker said "I can't" — output keeping it must NOT be rejected.
        ok, why = editor.validate_rewrite(
            "tell him I can't make it Tuesday",
            "Tell him I can't make it Tuesday.")
        assert ok, why

    def test_im_sorry_spoken_is_preserved(self):
        ok, why = editor.validate_rewrite(
            "I'm sorry for the delay, please resend the file",
            "I'm sorry for the delay. Please resend the file.")
        assert ok, why

    def test_preamble_rejected(self):
        ok, why = editor.validate_rewrite(
            "hello world",
            "Here's the corrected text: Hello world.")
        assert not ok and why == "preamble"

    def test_answered_instead_of_edited_rejected(self):
        ok, why = editor.validate_rewrite(
            "what time is the meeting tomorrow",
            "The meeting is at 3 PM in conference room B.")
        assert not ok and why == "invented"

    def test_empty_output_rejected(self):
        ok, why = editor.validate_rewrite("some words", "   ")
        assert not ok and why == "empty"

    def test_number_normalisation_passes(self):
        ok, why = editor.validate_rewrite(
            "the meeting is at three pm tomorrow",
            "The meeting is at 3 PM tomorrow.")
        assert ok, why

    def test_growth_rejected(self):
        raw = " ".join(["word"] * 20)
        out = raw + " " + " ".join(["extra"] * 60)
        ok, why = editor.validate_rewrite(raw, out)
        assert not ok and why in ("invented", "grew")

    def test_transcript_tags_stripped_before_validation(self):
        ok, why = editor.validate_rewrite(
            "hello there",
            "<transcript>Hello there.</transcript>")
        assert ok, why


# ------------------------------------------------------------ chunk_transcript
class TestChunkTranscript:
    def test_short_text_single_chunk(self):
        assert editor.chunk_transcript("hello world") == ["hello world"]

    def test_long_text_split_at_sentences(self):
        text = ". ".join([f"Sentence number {i} has words." for i in range(30)])
        chunks = editor.chunk_transcript(text, 40)
        assert len(chunks) > 1
        for c in chunks:
            assert editor.word_count(c) <= 40

    def test_monster_sentence_hard_split(self):
        text = " ".join(["word"] * 300)
        chunks = editor.chunk_transcript(text, 150)
        assert all(editor.word_count(c) <= 150 for c in chunks)


# -------------------------------------------------------------------- clean
class TestDecisionCounts:
    """Smart-mode skip vs LLM decisions — counters, no transcript text."""

    @pytest.fixture(autouse=True)
    def _clean_counters(self):
        editor.DECISION_COUNTS.reset()
        yield
        editor.DECISION_COUNTS.reset()

    def _llm(self, reply):
        def fn(model, raw, timeout_s, keep_alive, url):
            return reply, "stop"
        return fn

    def test_smart_skip_counted_without_touching_the_llm(self):
        text, status = editor.clean("hello my name is Akshay", mode="smart",
                                    llm_fn=self._llm("SHOULD NOT BE CALLED"))
        assert status == "raw:skipped-llm"
        assert editor.decision_counts() == {"smart_skips": 1,
                                            "smart_llm_calls": 0}

    def test_smart_llm_decision_counted(self):
        text, status = editor.clean("um hello there", mode="smart",
                                    llm_fn=self._llm("Hello there."))
        assert status == "cleaned"
        assert editor.decision_counts() == {"smart_skips": 0,
                                            "smart_llm_calls": 1}

    def test_other_modes_do_not_touch_smart_counters(self):
        editor.clean("hello my name is Akshay", mode="off",
                     llm_fn=self._llm("nope"))
        editor.clean("hello my name is Akshay", mode="always",
                     llm_fn=self._llm("Hello my name is Akshay."))
        assert editor.decision_counts() == {"smart_skips": 0,
                                            "smart_llm_calls": 0}

    def test_snapshot_never_contains_transcript_text(self):
        editor.clean("um please email the secret launch date", mode="smart",
                     llm_fn=self._llm("Please email the secret launch date."))
        snapshot = editor.decision_counts()
        assert all(isinstance(v, int) for v in snapshot.values())
        assert "secret" not in str(snapshot)


class TestCleanPipeline:
    def _llm(self, reply, done_reason="stop", calls=None):
        def fn(model, raw, timeout_s, keep_alive, url):
            if calls is not None:
                calls.append(raw)
            return reply, done_reason
        return fn

    def test_smart_skip_returns_tidy(self):
        text, status = editor.clean("hello my name is Akshay", mode="smart",
                                    llm_fn=self._llm("SHOULD NOT BE CALLED"))
        assert text == "Hello my name is Akshay"
        assert status == "raw:skipped-llm"

    def test_good_cleanup_used(self):
        calls = []
        fn = self._llm("Please email John the draft.", calls=calls)
        text, status = editor.clean(
            "um please email john the draft", mode="always", llm_fn=fn)
        assert text == "Please email John the draft."
        assert status == "cleaned"

    def test_transcript_wrapper_in_transport(self):
        # W1.3: the user turn is wrapped in <transcript> tags — verified at the
        # transport layer (_ollama_chat) with a stubbed requests.post.
        import editor as ed
        captured = {}
        class FakeResp:
            def raise_for_status(self):
                pass
            def json(self):
                return {"message": {"content": "Hi."}, "done_reason": "stop"}
        orig = ed.requests.post
        ed.requests.post = lambda url, json=None, timeout=None: captured.update(json=json) or FakeResp()
        try:
            out, reason = ed._ollama_chat("m", "hello there", 5, "5m")
        finally:
            ed.requests.post = orig
        assert out == "Hi." and reason == "stop"
        assert captured["json"]["messages"][1]["content"] == "<transcript>hello there</transcript>"
        assert captured["json"]["options"]["num_ctx"] == ed.NUM_CTX

    def test_truncation_falls_back_to_raw(self):
        fn = self._llm("partial output that got cut", done_reason="length")
        raw = "scratch that make it this is a long dictation " + "word " * 30
        text, status = editor.clean(raw, mode="always", llm_fn=fn)
        assert status == "raw:truncated"
        assert text == editor.tidy(raw)

    def test_timeout_falls_back_to_raw(self):
        class Boom:
            def __call__(self, *a, **k):
                raise editor.requests.Timeout("timed out")
        text, status = editor.clean("scratch that make it Thursday",
                                    mode="always", llm_fn=Boom())
        assert status == "raw:llm-unavailable"
        assert "Thursday" in text

    def test_guard_refusal_falls_back_to_raw(self):
        fn = self._llm("I can't book a table at Olive Garden.")
        raw = "can you book a table at Olive Garden for me and Priya on Saturday " + "ok " * 10
        text, status = editor.clean(raw, mode="always", llm_fn=fn)
        assert status == "raw:guard-refusal"
        assert "Priya" in text                            # raw preserved

    def test_empty_input(self):
        text, status = editor.clean("   ", mode="always", llm_fn=self._llm("x"))
        assert text == "" and status == "dropped:empty"

    def test_multi_chunk_mixed(self):
        seen = []
        def fn(model, raw, t, k, u):
            seen.append(raw)
            # clean the first chunk properly, break the second
            return ("First chunk clean.", "stop") if len(seen) == 1 \
                else ("I can't help with that at all.", "stop")
        long_raw = (". ").join([f"this is sentence {i} with some words" for i in range(25)])
        text, status = editor.clean(long_raw, mode="always", llm_fn=fn)
        assert status in ("raw:guard-refusal", "raw:mixed")
        assert "First chunk clean." in text               # good chunk survived


if __name__ == "__main__":
    pytest.main([__file__])
