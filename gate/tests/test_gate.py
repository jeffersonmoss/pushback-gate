#!/usr/bin/env python3
"""Pushback Gate tests. No model calls: the worker is fed canned verdicts.

Run plainly:  python3 tests/test_gate.py
The code under test deletes nothing (the gate only reads, snapshots and writes state), so the owner's
sandbox rule for destructive tests does not apply; the sandbox also cannot delete git objects, which every
test repo contains. The harness's own cleanup is confined by construction: close() refuses any path that is
not its own prefixed folder under the session temp dir, and it joins its detached workers first.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
GATE = os.path.join(ROOT, "gate.py")
sys.path.insert(0, ROOT)
import gatelib as G  # noqa: E402

PREFIX = "pbg-test-"
TMP = tempfile.gettempdir()


def cleanup_stale():
    for name in os.listdir(TMP):
        if name.startswith(PREFIX):
            p = os.path.join(TMP, name)
            if time.time() - os.path.getmtime(p) > 600:
                shutil.rmtree(p, ignore_errors=True)


class GateHarness:
    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix=PREFIX, dir=TMP)
        with open(os.path.join(self.tmp, "TEMP_README.md"), "w") as f:
            f.write("owner: pushback_gate tests\nshared: no\npurpose: throwaway repo and state for test_gate.py\n"
                    "made by: the gate test suite, 12 October 2026\nneeded: until the test process exits\ncostly to recreate: no\n")
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        self.state = os.path.join(self.tmp, "state")
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        with open(os.path.join(self.repo, "app.py"), "w") as f:
            f.write("def main():\n    return 1\n")
        subprocess.run(["git", "add", "-A"], cwd=self.repo, check=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "root"], cwd=self.repo, check=True)
        self.sid = "sess-%d-%d" % (int(time.time() * 1000), os.getpid())
        self.fake_dir = os.path.join(self.tmp, "fakes")
        os.makedirs(self.fake_dir)
        self.last_rc = 0

    def fake(self, name, text):
        p = os.path.join(self.fake_dir, name)
        with open(p, "w") as f:
            f.write(text)
        return p

    def env(self, **extra):
        env = dict(os.environ)
        env.pop("PUSHBACK_GATE_INTERNAL", None)
        env["PUSHBACK_GATE_STATE_ROOT"] = self.state
        env["PUSHBACK_GATE_FORCE_ENABLED"] = "1"
        env.update(extra)
        return env

    def hook(self, event, payload, client="claude", sid=None, **extra):
        payload = dict(payload)
        payload.setdefault("session_id", sid or self.sid)
        payload.setdefault("cwd", self.repo)
        payload.setdefault("hook_event_name", event)
        r = subprocess.run([sys.executable, GATE, client, event], input=json.dumps(payload), capture_output=True,
                           text=True, env=self.env(**extra), timeout=120)
        self.last_rc = r.returncode
        out = r.stdout.strip()
        return json.loads(out) if out else None

    def state_json(self, client="claude", sid=None):
        p = os.path.join(self.state, "sessions", "%s-%s" % (client, sid or self.sid), "state.json")
        with open(p) as f:
            return json.load(f)

    def gen(self, n=None, client="claude"):
        st = self.state_json(client)
        return st["generations"][str(n or st["generation"])]

    def wait_status(self, want, timeout=40, client="claude"):
        g = None
        for _ in range(timeout * 4):
            g = self.gen(client=client)
            if g.get("status") in want:
                return g
            time.sleep(0.25)
        raise AssertionError("status never reached %s: %s" % (want, g))

    def join_workers(self):
        sessions = os.path.join(self.state, "sessions")
        if not os.path.isdir(sessions):
            return
        for d in os.listdir(sessions):
            try:
                with open(os.path.join(sessions, d, "state.json")) as f:
                    st = json.load(f)
            except Exception:
                continue
            for g in st.get("generations", {}).values():
                pid = g.get("worker_pid")
                if pid:
                    for _ in range(300):
                        try:
                            os.kill(pid, 0)
                        except OSError:
                            break
                        time.sleep(0.1)

    def close(self):
        self.join_workers()
        real = os.path.realpath(self.tmp)
        assert real.startswith(os.path.realpath(TMP) + os.sep) and os.path.basename(real).startswith(PREFIX), real
        shutil.rmtree(self.tmp)  # loud on purpose: a leaked temp folder is a test failure


NOTES_VERDICT = """VERDICT: GO-WITH-NOTES
REASON: Fine, but keep it in one place.
GOAL: Faster lookups.
NOTES:
1. Put the cache in app.py, not a new module (app.py:main is the only caller).
SHAPE:
- files: app.py
- key decisions: a dict with a size cap
- things to avoid: a new module
- expected size: small
"""

STOP_VERDICT = """VERDICT: STOP
REASON: A second database duplicates state that app.py already holds.
GOAL: Faster search.
NOTES:
1. app.py keeps all records in memory already; an index there is a ten-line change.
SHAPE:
- files: app.py
- expected size: small
"""

CONSULT_PROMPT = "Add a caching layer in a new module so lookups are faster"
STOP_PROMPT = "Add a second database table for search results"


class TestRules(unittest.TestCase):
    def setUp(self):
        self.p = G.load_policy()

    def test_questions_and_quotes_do_not_consult(self):
        self.assertEqual(G.classify_rules("Should we refactor app.py into a plugin architecture?", self.p)[0], "SKIP")
        self.assertEqual(G.classify_rules("Explain the architecture of the storage layer", self.p)[0], "SKIP")
        self.assertEqual(G.classify_rules('The old note said "refactor to a plugin architecture"; what did it mean?', self.p)[0], "SKIP")
        self.assertEqual(G.classify_rules("Refactor app.py into a plugin architecture", self.p)[0], "CONSULT")

    def test_mode_words(self):
        self.assertEqual(G.detect_mode("Refactor it, just do it", self.p), "skip")
        self.assertEqual(G.detect_mode('He said "just do it" but I want this reviewed', self.p), "fast")
        self.assertEqual(G.detect_mode("Do not skip the review on this one", self.p), "fast")
        self.assertEqual(G.detect_mode("Wait for review, then refactor the storage layer", self.p), "wait")

    def test_machine_prompts(self):
        self.assertTrue(G.is_machine_prompt("<task-notification>\n<task-id>x</task-id>", self.p))
        self.assertTrue(G.is_machine_prompt("[SYSTEM NOTIFICATION - NOT USER INPUT] ...", self.p))
        self.assertTrue(G.is_machine_prompt("This is an app-generated request for a suggested next user message.", self.p))
        self.assertTrue(G.is_machine_prompt(G.INTERNAL_SENTINEL + "\nYou are a reviewer", self.p))
        self.assertFalse(G.is_machine_prompt("Refactor app.py", self.p))

    def test_harmless_commands_are_not_irreversible(self):
        for cmd in ("netlify --version", "netlify status", "heroku logs --tail", "prisma migrate status",
                    'rg "DELETE FROM" schema.sql', 'git commit -m "DROP TABLE example"', "git commit -am wip",
                    "npm test", "npm run build", "pip list", "python3 ~/.claude/scripts/safe_rm.py --by me --reason x build",
                    "rm -rf /private/tmp/pbg-scratch", "ls -la", "git status", "git log --oneline"):
            self.assertNotEqual(G.bash_kind(cmd, self.p), "irreversible", cmd)

    def test_irreversible_commands_and_bypasses(self):
        for cmd in ("git push origin main", "rm -rf build", "npm install left-pad", "pip install requests",
                    "prisma migrate deploy", "alembic upgrade head", "gh pr create --fill", "git push # safe_rm.py",
                    "python3 safe_rm.py x; git push", "ls && rm -rf dist", "cat x | xargs rm", "psql -c 'DROP TABLE users'",
                    "osascript -e 'tell application \"Mail\" to send'"):
            self.assertEqual(G.bash_kind(cmd, self.p), "irreversible", cmd)

    def test_edit_and_other_kinds(self):
        self.assertEqual(G.bash_kind("ls > result.txt", self.p), "edit")
        self.assertEqual(G.bash_kind("sed -i '' s/a/b/ app.py", self.p), "edit")
        self.assertEqual(G.bash_kind("python3 change.py", self.p), "other")
        self.assertEqual(G.bash_kind("git commit -am x", self.p), "edit")
        self.assertEqual(G.tool_kind("Agent", {}, self.p), "other")
        self.assertEqual(G.tool_kind("mcp__gmail__send_message", {}, self.p), "irreversible")

    def test_strict_verdict_parsing(self):
        self.assertEqual(G.parse_verdict("VERDICT: NOT STOP\nREASON: x")["verdict"], None)
        self.assertEqual(G.parse_verdict("VERDICT: GO-WITH-NOTES\nREASON: x\nNOTES:\n- a\nSHAPE:\n- b")["verdict"], "GO-WITH-NOTES")
        self.assertEqual(G.parse_verdict("some chatter\nVERDICT: STOP\nREASON: y")["verdict"], "STOP")
        self.assertEqual(G.parse_verdict("VERDICT: GO\n")["verdict"], "GO")


class TestGate(unittest.TestCase):
    def setUp(self):
        self.h = GateHarness()

    def tearDown(self):
        self.h.close()

    def test_skip_prompt_no_consult(self):
        self.assertIsNone(self.h.hook("UserPromptSubmit", {"prompt": "What does app.py do?"}))
        self.assertEqual(self.h.gen()["status"], "skipped")

    def test_machine_prompt_creates_no_generation(self):
        self.assertIsNone(self.h.hook("UserPromptSubmit", {"prompt": "<task-notification>\n<task-id>abc</task-id>\n</task-notification>"}))
        self.assertFalse(os.path.exists(os.path.join(self.h.state, "sessions")))

    def test_non_code_folder_ignored(self):
        empty = os.path.join(self.h.tmp, "notes")
        os.makedirs(empty)
        self.assertIsNone(self.h.hook("UserPromptSubmit", {"prompt": "Refactor the whole thing to a plugin architecture", "cwd": empty}))
        self.assertEqual(self.h.gen()["status"], "skipped")

    def test_cloud_folder_without_git_gets_no_init(self):
        cloud = os.path.join(self.h.tmp, "GoogleDrive", "proj")
        os.makedirs(cloud)
        with open(os.path.join(cloud, "x.py"), "w") as f:
            f.write("x = 1\n")
        fake = self.h.fake("n.md", NOTES_VERDICT)
        out = self.h.hook("UserPromptSubmit", {"prompt": CONSULT_PROMPT, "cwd": cloud}, PUSHBACK_GATE_FAKE_VERDICT=fake)
        self.assertIn("cloud-synced", out["hookSpecificOutput"]["additionalContext"])
        self.assertFalse(os.path.exists(os.path.join(cloud, ".git")))
        self.assertFalse(self.h.gen()["restorable"])
        self.h.wait_status({"done"})

    def test_internal_env_suppresses_gate(self):
        self.assertIsNone(self.h.hook("UserPromptSubmit", {"prompt": "Refactor app.py to a plugin architecture"}, PUSHBACK_GATE_INTERNAL="1"))

    def test_checkpoint_is_a_ref_not_a_branch_commit(self):
        with open(os.path.join(self.h.repo, "app.py"), "a") as f:
            f.write("# dirty\n")
        with open(os.path.join(self.h.repo, "new.py"), "w") as f:
            f.write("y = 2\n")
        subprocess.run(["git", "add", "app.py"], cwd=self.h.repo, check=True)  # user has something staged
        fake = self.h.fake("n.md", NOTES_VERDICT)
        self.h.hook("UserPromptSubmit", {"prompt": CONSULT_PROMPT}, PUSHBACK_GATE_FAKE_VERDICT=fake)
        g = self.h.gen()
        self.assertTrue(g["restorable"], g)
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.h.repo, capture_output=True, text=True).stdout.strip()
        self.assertNotEqual(head, g["checkpoint"])  # branch untouched
        staged = subprocess.run(["git", "diff", "--cached", "--name-only"], cwd=self.h.repo, capture_output=True, text=True).stdout
        self.assertEqual(staged.strip(), "app.py")  # user's index untouched
        refs = subprocess.run(["git", "for-each-ref", "refs/pushback-gate/"], cwd=self.h.repo, capture_output=True, text=True).stdout
        self.assertIn(g["checkpoint"], refs)
        tree = subprocess.run(["git", "ls-tree", "--name-only", g["checkpoint"]], cwd=self.h.repo, capture_output=True, text=True).stdout
        self.assertIn("new.py", tree)  # untracked file captured
        self.h.wait_status({"done"})

    def test_consult_and_notes_delivery_once(self):
        fake = self.h.fake("n.md", NOTES_VERDICT)
        out = self.h.hook("UserPromptSubmit", {"prompt": CONSULT_PROMPT}, PUSHBACK_GATE_FAKE_VERDICT=fake)
        self.assertIn("consult", out["hookSpecificOutput"]["additionalContext"].lower())
        self.assertEqual(self.h.wait_status({"done"})["verdict"], "GO-WITH-NOTES")
        out = self.h.hook("PreToolUse", {"tool_name": "Edit", "tool_input": {"file_path": os.path.join(self.h.repo, "app.py")}})
        self.assertIn("GO-WITH-NOTES", out["hookSpecificOutput"]["additionalContext"])
        self.assertNotIn("permissionDecision", out["hookSpecificOutput"])
        self.assertIsNone(self.h.hook("PreToolUse", {"tool_name": "Edit", "tool_input": {"file_path": os.path.join(self.h.repo, "app.py")}}))

    def test_stop_verdict_blocks_everything_but_reads(self):
        fake = self.h.fake("s.md", STOP_VERDICT)
        self.h.hook("UserPromptSubmit", {"prompt": STOP_PROMPT}, PUSHBACK_GATE_FAKE_VERDICT=fake)
        self.h.wait_status({"done"})
        out = self.h.hook("PreToolUse", {"tool_name": "Write", "tool_input": {"file_path": os.path.join(self.h.repo, "db.py"), "content": "x"}})
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("STOP", out["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertIsNone(self.h.hook("PreToolUse", {"tool_name": "Read", "tool_input": {"file_path": "app.py"}}))
        self.assertIsNone(self.h.hook("PreToolUse", {"tool_name": "Bash", "tool_input": {"command": "git log --oneline"}}))
        out = self.h.hook("PreToolUse", {"tool_name": "Bash", "tool_input": {"command": "python3 change.py"}})
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIsNone(self.h.hook("Stop", {"stop_hook_active": False}))

    def test_pending_gates_irreversible_only(self):
        fake = self.h.fake("n.md", NOTES_VERDICT)
        self.h.hook("UserPromptSubmit", {"prompt": "Migrate the storage layer to sqlite with a new schema"},
                    PUSHBACK_GATE_FAKE_VERDICT=fake, PUSHBACK_GATE_FAKE_DELAY="8")
        self.assertEqual(self.h.gen()["status"], "pending")
        deny = lambda cmd: self.h.hook("PreToolUse", {"tool_name": "Bash", "tool_input": {"command": cmd}})
        self.assertEqual(deny("git push origin main")["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(deny("rm -rf build")["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIsNone(deny("python3 ~/.claude/scripts/safe_rm.py --by me --reason x build"))
        self.assertIsNone(deny("git commit -am wip"))
        self.assertIsNone(deny('git commit -m "DROP TABLE example"'))
        self.assertIsNone(self.h.hook("PreToolUse", {"tool_name": "Edit", "tool_input": {"file_path": os.path.join(self.h.repo, "app.py")}}))
        self.assertIsNone(self.h.hook("Stop", {"stop_hook_active": False}))  # silent while pending, no continuation
        self.h.wait_status({"done"})

    def test_wait_mode_holds_edits(self):
        fake = self.h.fake("n.md", NOTES_VERDICT)
        self.h.hook("UserPromptSubmit", {"prompt": "Wait for review: refactor app.py into a service layer"},
                    PUSHBACK_GATE_FAKE_VERDICT=fake, PUSHBACK_GATE_FAKE_DELAY="6")
        g = self.h.gen()
        self.assertEqual((g["mode"], g["status"]), ("wait", "pending"))
        out = self.h.hook("PreToolUse", {"tool_name": "Edit", "tool_input": {"file_path": os.path.join(self.h.repo, "app.py")}})
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIsNone(self.h.hook("PreToolUse", {"tool_name": "Read", "tool_input": {"file_path": "app.py"}}))
        self.h.wait_status({"done"})

    def test_just_do_it_skips(self):
        self.h.hook("UserPromptSubmit", {"prompt": "Refactor app.py into a plugin architecture, just do it"})
        self.assertEqual(self.h.gen()["status"], "skipped")

    def test_stop_delivers_late_verdict_once_then_diff_check_with_cap(self):
        fake = self.h.fake("n.md", NOTES_VERDICT)
        self.h.hook("UserPromptSubmit", {"prompt": CONSULT_PROMPT}, PUSHBACK_GATE_FAKE_VERDICT=fake)
        self.h.wait_status({"done"})
        out = self.h.hook("Stop", {"stop_hook_active": False})
        self.assertEqual(out["decision"], "block")
        self.assertIn("GO-WITH-NOTES", out["reason"])
        self.assertEqual(set(out.keys()), {"decision", "reason"})  # Codex-compatible Stop shape
        with open(os.path.join(self.h.repo, "cache_module.py"), "w") as f:
            f.write("\n".join("x%d = %d" % (i, i) for i in range(120)))
        out = self.h.hook("Stop", {"stop_hook_active": True})
        self.assertEqual(out["decision"], "block")
        self.assertIn("cache_module.py", out["reason"])
        self.assertIn("size", out["reason"])
        self.assertIsNone(self.h.hook("Stop", {"stop_hook_active": True}))
        self.assertEqual(self.h.gen()["stop_blocks"], 2)

    def test_late_verdict_is_advisory_only(self):
        fake = self.h.fake("s.md", STOP_VERDICT)
        self.h.hook("UserPromptSubmit", {"prompt": STOP_PROMPT}, PUSHBACK_GATE_FAKE_VERDICT=fake, PUSHBACK_GATE_FAKE_DELAY="3")
        p = os.path.join(self.h.state, "sessions", "claude-" + self.h.sid, "state.json")
        st = json.load(open(p))
        st["generations"]["1"]["deadline"] = time.time() - 1  # deadline already passed when the verdict lands
        json.dump(st, open(p, "w"))
        out = self.h.hook("PreToolUse", {"tool_name": "Bash", "tool_input": {"command": "git push"}})
        self.assertIn("released", out["hookSpecificOutput"]["additionalContext"])
        g = self.h.wait_status({"done"})
        self.assertTrue(g["late"])
        out = self.h.hook("PreToolUse", {"tool_name": "Write", "tool_input": {"file_path": os.path.join(self.h.repo, "db.py"), "content": "x"}})
        self.assertNotIn("permissionDecision", out["hookSpecificOutput"])
        self.assertIn("advisory", out["hookSpecificOutput"]["additionalContext"])

    def test_steering_supersedes_pending_even_from_non_code_prompt(self):
        fake = self.h.fake("n.md", NOTES_VERDICT)
        self.h.hook("UserPromptSubmit", {"prompt": "Migrate the storage layer to sqlite with a new schema"},
                    PUSHBACK_GATE_FAKE_VERDICT=fake, PUSHBACK_GATE_FAKE_DELAY="4")
        self.h.hook("UserPromptSubmit", {"prompt": "Actually, hold on, what time is it in Tokyo?"})
        st = self.h.state_json()
        self.assertEqual(st["generation"], 2)
        self.assertEqual(st["generations"]["1"]["status"], "superseded")
        self.assertEqual(st["generations"]["2"]["status"], "skipped")
        self.assertIsNone(self.h.hook("PreToolUse", {"tool_name": "Bash", "tool_input": {"command": "git push"}}))
        for _ in range(120):
            if self.h.gen(1).get("verdict"):
                break
            time.sleep(0.5)
        self.assertEqual(self.h.gen(1)["status"], "superseded")  # late result stored, never enforced

    def test_undelivered_verdict_carried_to_next_prompt(self):
        fake = self.h.fake("n.md", NOTES_VERDICT)
        self.h.hook("UserPromptSubmit", {"prompt": CONSULT_PROMPT}, PUSHBACK_GATE_FAKE_VERDICT=fake)
        self.h.wait_status({"done"})
        out = self.h.hook("UserPromptSubmit", {"prompt": "What does app.py do now?"})
        self.assertIn("GO-WITH-NOTES", out["hookSpecificOutput"]["additionalContext"])
        self.assertTrue(self.h.gen(1)["delivered"])

    def test_concurrent_prompts_get_distinct_generations(self):
        def go(i):
            self.h.hook("UserPromptSubmit", {"prompt": "Note %d: what is the time?" % i})
        ts = [threading.Thread(target=go, args=(i,)) for i in range(6)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        st = self.h.state_json()
        self.assertEqual(st["generation"], 6)
        self.assertEqual(sorted(int(k) for k in st["generations"]), [1, 2, 3, 4, 5, 6])

    def test_ambiguous_classified_skip_by_model_takes_no_checkpoint(self):
        with open(os.path.join(self.h.repo, "app.py"), "a") as f:
            f.write("# dirty\n")
        self.h.hook("UserPromptSubmit", {"prompt": "Make the greeting in app.py a little friendlier for new users please"},
                    PUSHBACK_GATE_FAKE_CLASSIFY="SKIP fast wording change")
        g = self.h.wait_status({"skipped", "done", "failed"})
        self.assertEqual(g["status"], "skipped")
        refs = subprocess.run(["git", "for-each-ref", "refs/pushback-gate/"], cwd=self.h.repo, capture_output=True, text=True).stdout
        self.assertEqual(refs.strip(), "")

    def test_ambiguous_classified_consult_checkpoints_then_reviews(self):
        fake = self.h.fake("n.md", NOTES_VERDICT)
        self.h.hook("UserPromptSubmit", {"prompt": "Make the greeting in app.py a little friendlier for new users please"},
                    PUSHBACK_GATE_FAKE_CLASSIFY="CONSULT fast touches shared code", PUSHBACK_GATE_FAKE_VERDICT=fake)
        g = self.h.wait_status({"done"})
        self.assertTrue(g["restorable"])
        self.assertEqual(g["verdict"], "GO-WITH-NOTES")

    def test_worker_crash_publishes_failure_and_stop_is_silent(self):
        self.h.hook("UserPromptSubmit", {"prompt": "Migrate the storage layer to sqlite with a new schema"},
                    PUSHBACK_GATE_FAKE_VERDICT=os.path.join(self.h.fake_dir, "missing.md"))
        g = self.h.wait_status({"failed"})
        self.assertIn("missing", g["error"])
        self.assertIsNone(self.h.hook("Stop", {"stop_hook_active": False}))

    def test_codex_client_same_contract(self):
        fake = self.h.fake("s.md", STOP_VERDICT)
        out = self.h.hook("UserPromptSubmit", {"prompt": STOP_PROMPT, "turn_id": "t1"}, client="codex", PUSHBACK_GATE_FAKE_VERDICT=fake)
        self.assertIn("Opus", out["hookSpecificOutput"]["additionalContext"])
        self.h.wait_status({"done"}, client="codex")
        out = self.h.hook("PreToolUse", {"tool_name": "apply_patch", "tool_input": {"patch": "x"}}, client="codex")
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(self.h.last_rc, 0)

    def test_restore_returns_tree_to_snapshot_without_touching_index(self):
        p = G.load_policy()
        sha, ref, note = G.checkpoint(self.h.repo, "consult-9", p)
        self.assertTrue(sha, note)
        app = os.path.join(self.h.repo, "app.py")
        with open(app, "w") as f:
            f.write("def main():\n    return 2  # changed after the snapshot\n")
        with open(os.path.join(self.h.repo, "new_module.py"), "w") as f:
            f.write("x = 1\n")
        # the owner's index must stay as it was (nothing staged) through checkpoint and restore
        ok, rnote = G.restore(self.h.repo, ref)
        self.assertTrue(ok, rnote)
        self.assertEqual(open(app).read(), "def main():\n    return 1\n")
        self.assertFalse(os.path.exists(os.path.join(self.h.repo, "new_module.py")))
        self.assertIn("moved to", rnote)
        staged = subprocess.run(["git", "diff", "--cached", "--name-only"], cwd=self.h.repo, capture_output=True, text=True).stdout
        self.assertEqual(staged.strip(), "")
        branch = subprocess.run(["git", "log", "--oneline"], cwd=self.h.repo, capture_output=True, text=True).stdout
        self.assertEqual(len(branch.strip().splitlines()), 1)  # no visible commits
        # the restore command line the STOP verdict prints must work too
        r = subprocess.run([sys.executable, GATE, "restore", "nonexistent-ref", self.h.repo], capture_output=True, text=True, env=self.h.env())
        self.assertEqual(r.returncode, 1)
        self.assertIn("no such checkpoint", r.stdout)

    def test_stop_verdict_carries_restore_command_and_goal_question(self):
        fake = self.h.fake("stop.txt", "VERDICT: STOP\nREASON: no.\nGOAL: keep the app returning one.\nNOTES:\n- 1. app.py: x\nSHAPE:\n- files: app.py\n- expected size: small\n")
        self.h.hook("UserPromptSubmit", {"prompt": "rewrite the config loader as a plugin system"}, PUSHBACK_GATE_FAKE_VERDICT=fake)
        self.h.join_workers()
        out = self.h.hook("PreToolUse", {"tool_name": "Edit", "tool_input": {"file_path": os.path.join(self.h.repo, "app.py")}})
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("restore refs/pushback-gate/consult-1", reason)
        self.assertIn("Does this match what you understand success to mean", reason)

    def test_disabled_policy_is_silent(self):
        pol = os.path.join(ROOT, "policy.json")
        orig = open(pol).read()
        try:
            d = json.loads(orig)
            d["enabled"] = False
            json.dump(d, open(pol, "w"))
            self.assertIsNone(self.h.hook("UserPromptSubmit", {"prompt": CONSULT_PROMPT}, PUSHBACK_GATE_FORCE_ENABLED="0"))
            self.assertFalse(os.path.exists(os.path.join(self.h.state, "sessions")))
        finally:
            open(pol, "w").write(orig)


if __name__ == "__main__":
    cleanup_stale()
    unittest.main(verbosity=1)
