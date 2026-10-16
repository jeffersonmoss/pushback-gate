#!/usr/bin/env python3
"""Waivers, review talk, loud failures and repeat-failure notices (16 October 2026). Canned verdicts, no model calls.
Notices go to a test command that writes a file; nothing is ever sent to a person."""
import json
import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
os.environ["PUSHBACK_GATE_NO_LOCAL"] = "1"
import test_gate as T  # noqa: E402
import gatelib as G  # noqa: E402

WAIVE = "python3 %s waive" % os.path.join(os.path.dirname(HERE), "gate.py")
DIP_QUESTION = ("Are you not able to waive the pushback gate review for changes that are minor and don't really need to be "
                "reviewed because the extension is already working code and the change is very minor, or are you forced to "
                "do so because of the word architectural? Because it said that it was architectural.")


class Waivers(unittest.TestCase):
    def setUp(self):
        self.h = T.GateHarness()

    def tearDown(self):
        self.h.close()

    def start_slow_consult(self, prompt=T.CONSULT_PROMPT, **extra):
        fake = self.h.fake("notes.md", T.NOTES_VERDICT)
        self.h.hook("UserPromptSubmit", {"prompt": prompt}, PUSHBACK_GATE_FAKE_VERDICT=fake, PUSHBACK_GATE_FAKE_DELAY="20", **extra)
        return self.h.wait_status({"pending"})

    def bash(self, cmd):
        return self.h.hook("PreToolUse", {"tool_name": "Bash", "tool_input": {"command": cmd}})

    def test_question_mentioning_architecture_starts_no_review(self):
        # his actual message: talks about the review, and ends with a statement rather than a question mark
        self.assertIsNone(self.h.hook("UserPromptSubmit", {"prompt": DIP_QUESTION}))
        self.assertFalse(os.path.exists(os.path.join(self.h.state, "sessions")) and self.h.state_json().get("generation"))
        q = "Is this change architectural or is it a small tweak to how the popup renders the list?"
        self.assertEqual(G.classify_rules(q, G.load_policy())[0], "SKIP")

    def test_architecture_word_alone_no_longer_forces_a_review(self):
        self.assertNotEqual(G.classify_rules("Keep the architectural notes in mind and tidy up the popup code", G.load_policy())[0], "CONSULT")

    def test_talking_about_the_review_does_not_replace_the_running_one(self):
        g = self.start_slow_consult()
        self.h.hook("UserPromptSubmit", {"prompt": "how long does the pushback reviewer usually take with this kind of thing"})
        self.assertEqual(self.h.state_json()["generation"], g["gen"])
        self.assertEqual(self.h.gen()["status"], "pending")

    def test_owner_waiver_in_his_own_words_releases_the_running_review(self):
        self.start_slow_consult()
        self.h.hook("UserPromptSubmit", {"prompt": "waive it, this one is tiny, just ship the change"})
        st = self.h.state_json()
        prev = st["generations"]["1"]
        self.assertEqual(prev["status"], "waived")
        self.assertEqual(prev["waived"]["by"], "owner")
        self.assertIsNone(self.bash("git push origin main"))

    def test_agent_waiver_by_command_releases_holds(self):
        self.start_slow_consult()
        out = self.bash('%s --by agent "one-line copy change in the popup"' % WAIVE)
        self.assertIn("waived by the agent", out["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(self.h.gen()["status"], "waived")
        self.assertIsNone(self.bash("npm install left-pad"))

    def test_mentioning_the_waive_command_in_text_does_not_waive(self):
        self.start_slow_consult()
        self.bash("cat >> README.md <<'EOF'\nRun `%s --by agent \"minor\"` to waive.\nEOF" % WAIVE)
        self.bash('echo "%s --by agent minor"' % WAIVE)
        self.assertEqual(self.h.gen()["status"], "pending")

    def test_agent_cannot_waive_a_review_the_owner_asked_for(self):
        self.start_slow_consult(prompt=T.CONSULT_PROMPT + ". Wait for review before editing.")
        out = self.bash('%s --by agent "small"' % WAIVE)
        self.assertIn("waiver refused", out["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(self.h.gen()["status"], "pending")
        out = self.bash('%s --by owner "he said skip it"' % WAIVE)
        self.assertEqual(self.h.gen()["status"], "waived")

    def test_agent_waiver_that_turns_out_large_is_flagged_once(self):
        self.start_slow_consult()
        self.bash('%s --by agent "tiny tweak"' % WAIVE)
        with open(os.path.join(self.h.repo, "app.py"), "a") as f:
            f.write("".join("x%d = %d\n" % (i, i) for i in range(150)))
        out = self.h.hook("Stop", {"stop_hook_active": False})
        self.assertIn("waived review as minor", out["reason"])
        self.assertIsNone(self.h.hook("Stop", {"stop_hook_active": True}))

    def test_agent_waiver_that_adds_a_dependency_is_flagged(self):
        self.start_slow_consult()
        self.bash('%s --by agent "tiny tweak"' % WAIVE)
        with open(os.path.join(self.h.repo, "requirements.txt"), "w") as f:
            f.write("requests>=2.31\n")
        out = self.h.hook("Stop", {"stop_hook_active": False})
        self.assertIn("possible new dependencies", out["reason"])
        self.assertIn("requests", out["reason"])

    def test_waived_status_survives_the_reviewer_finishing(self):
        fake = self.h.fake("notes.md", T.NOTES_VERDICT)
        self.h.hook("UserPromptSubmit", {"prompt": T.CONSULT_PROMPT}, PUSHBACK_GATE_FAKE_VERDICT=fake, PUSHBACK_GATE_FAKE_DELAY="4")
        self.h.wait_status({"pending"})
        self.bash('%s --by agent "tiny tweak"' % WAIVE)
        deadline = time.time() + 30
        while time.time() < deadline and not self.h.gen().get("verdict"):
            time.sleep(0.5)
        self.assertTrue(self.h.gen().get("verdict"))
        self.assertEqual(self.h.gen()["status"], "waived")
        self.assertIsNone(self.bash("git push origin main"))

    def test_small_agent_waiver_is_quiet(self):
        self.start_slow_consult()
        self.bash('%s --by agent "tiny tweak"' % WAIVE)
        with open(os.path.join(self.h.repo, "app.py"), "a") as f:
            f.write("# one line\n")
        self.assertIsNone(self.h.hook("Stop", {"stop_hook_active": False}))

    def test_failure_is_announced_loudly(self):
        self.h.hook("UserPromptSubmit", {"prompt": T.CONSULT_PROMPT}, PUSHBACK_GATE_FAKE_VERDICT=os.path.join(self.h.fake_dir, "missing.md"))
        self.h.wait_status({"failed"})
        out = self.h.hook("PreToolUse", {"tool_name": "Edit", "tool_input": {"file_path": os.path.join(self.h.repo, "app.py")}})
        text = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("FAILED", text)
        self.assertIn("Tell the owner", text)


class RepeatFailureNotices(unittest.TestCase):
    def setUp(self):
        self.h = T.GateHarness()
        self.old = (G.STATE_ROOT, G.SESSIONS_DIR, G.LOG_PATH)
        G.STATE_ROOT = os.path.join(self.h.tmp, "notice-state")
        G.SESSIONS_DIR = os.path.join(G.STATE_ROOT, "sessions")
        G.LOG_PATH = os.path.join(G.STATE_ROOT, "gate.log")
        self.sent = os.path.join(self.h.tmp, "sent.txt")
        self.policy = dict(G.load_policy(), notify_quiet_hours=[0, 0],
                           repeat_failure_notify_command=[sys.executable, "-c",
                                                          "import sys;open(%r,'a').write(sys.stdin.read()+'\\n---\\n')" % self.sent])

    def tearDown(self):
        G.STATE_ROOT, G.SESSIONS_DIR, G.LOG_PATH = self.old
        self.h.close()

    def sent_count(self):
        return open(self.sent).read().count("---") if os.path.exists(self.sent) else 0

    def test_one_failure_is_not_sent_two_same_cause_are_sent_once_a_day(self):
        err = "claude failed: Not logged in. Please run /login"
        G.record_review_outcome("codex", "Opus", False, err, self.policy)
        self.assertEqual(self.sent_count(), 0)
        G.record_review_outcome("codex", "Opus", False, err, self.policy)
        self.assertEqual(self.sent_count(), 1)
        self.assertIn("sign-in problem", open(self.sent).read())
        G.record_review_outcome("codex", "Opus", False, err, self.policy)
        self.assertEqual(self.sent_count(), 1)

    def test_different_causes_and_successes_reset_the_streak(self):
        G.record_review_outcome("claude", "Sol", False, "timeout after 600s", self.policy)
        G.record_review_outcome("claude", "Sol", False, "rate limit reached", self.policy)
        G.record_review_outcome("claude", "Sol", True, "", self.policy)
        G.record_review_outcome("claude", "Sol", False, "rate limit reached", self.policy)
        self.assertEqual(self.sent_count(), 0)

    def test_quiet_hours_hold_the_notice_until_later(self):
        quiet = dict(self.policy, notify_quiet_hours=[0, 24])
        G.record_review_outcome("codex", "Opus", False, "boom 1", quiet)
        G.record_review_outcome("codex", "Opus", False, "boom 2", quiet)
        self.assertEqual(self.sent_count(), 0)
        G.flush_notices(self.policy)
        self.assertEqual(self.sent_count(), 1)

    def test_reviewer_programs_are_found_without_a_useful_path(self):
        old = os.environ.get("PATH")
        os.environ["PATH"] = "/usr/bin:/bin"
        try:
            found = G.find_binary("claude")
        finally:
            os.environ["PATH"] = old
        if os.path.exists(os.path.expanduser("~/.local/bin/claude")):
            self.assertTrue(os.path.isabs(found), found)


if __name__ == "__main__":
    unittest.main(verbosity=1)
