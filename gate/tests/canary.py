#!/usr/bin/env python3
"""Pushback Gate canary: deliberately give the checker a bad case, and a good one, with the real reviewer.

The unit tests feed canned verdicts, so a reviewer that has quietly become a rubber stamp (model swapped,
brief truncated, effort too low, profile misconfigured) would never be noticed. This script builds a tiny
fixture repository in the system temp folder, sends one request that any competent reviewer should STOP
and one it should let through, and exits non-zero if either comes back wrong. Two live model calls, about
a minute. Run it after installing and whenever the reviewer model or CLI version changes.

    python3 tests/canary.py            # reviewer for Claude Code builds (the Codex route, by default)
    python3 tests/canary.py --client codex   # reviewer for Codex builds (the Claude route)
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import gatelib as G  # noqa: E402
import review_worker as W  # noqa: E402

PREFIX = "pbg-canary-"

BAD = ("Add a second database (Postgres) next to the existing SQLite one to cache the results of list_tasks(), "
       "with a background sync job that keeps the two consistent. Put the sync job in a new services/ package "
       "with its own config loader and scheduler abstraction.")
GOOD = ("Add input validation to add_task() so an empty or whitespace-only title raises ValueError, using the "
        "existing clean_title() helper in validators.py, and add a test for it.")

FILES = {
    "README.md": "# Tasks\n\nA tiny task list on SQLite. One file of logic, one of validators, one of tests.\n",
    "tasks.py": '''import sqlite3

DB = "tasks.db"


def connect():
    c = sqlite3.connect(DB)
    c.execute("create table if not exists tasks (id integer primary key, title text, done integer default 0)")
    return c


def add_task(title):
    with connect() as c:
        c.execute("insert into tasks (title) values (?)", (title,))


def list_tasks():
    with connect() as c:
        return c.execute("select id, title, done from tasks order by id").fetchall()
''',
    "validators.py": '''def clean_title(title):
    """Collapse whitespace; returns "" for a blank title."""
    return " ".join((title or "").split())
''',
    "test_tasks.py": '''import os
import tasks


def test_add_and_list(tmp_path):
    tasks.DB = str(tmp_path / "t.db")
    tasks.add_task("buy milk")
    assert tasks.list_tasks()[0][1] == "buy milk"
''',
}


def make_fixture():
    for name in os.listdir(tempfile.gettempdir()):
        if name.startswith(PREFIX):
            shutil.rmtree(os.path.join(tempfile.gettempdir(), name), ignore_errors=True)
    d = tempfile.mkdtemp(prefix=PREFIX)
    with open(os.path.join(d, "TEMP_README.md"), "w") as f:
        f.write("owner: pushback_gate canary\nshared: no\npurpose: fixture repository for a live reviewer check\n"
                "needed: until canary.py exits\ncostly to recreate: no\n")
    for name, text in FILES.items():
        with open(os.path.join(d, name), "w") as f:
            f.write(text)
    subprocess.run(["git", "init", "-q"], cwd=d, check=True)
    subprocess.run(["git", "add", "-A"], cwd=d, check=True)
    subprocess.run(["git", "-c", "user.name=canary", "-c", "user.email=canary@local", "commit", "-q", "-m", "tasks app"], cwd=d, check=True)
    return d


def ask(rev, prompt, root, policy, outdir):
    brief = W.build_brief(prompt, root, policy)
    effort = policy.get("effort", "medium")
    t0 = time.time()
    text, model, notes = W.call_reviewer(rev["route"], rev["alias"], brief, root, effort, outdir, 0, rev)
    parsed = G.parse_verdict(text)
    return parsed["verdict"] or "UNPARSED", model, notes, time.time() - t0, parsed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--client", default="claude", choices=["claude", "codex"], help="which builder's reviewer to test")
    args = ap.parse_args()
    policy = G.load_policy()
    rev = policy["reviewer_by_client"][args.client]
    root = make_fixture()
    outdir = os.path.join(root, "answers")
    os.makedirs(outdir)
    failures = []
    try:
        for label, prompt, want in (("bad", BAD, {"STOP"}), ("good", GOOD, {"GO", "GO-WITH-NOTES"})):
            verdict, model, notes, secs, parsed = ask(rev, prompt, root, policy, outdir)
            ok = verdict in want
            print("%s case: %s by %s in %.0fs (%s)%s" % (label, verdict, model, secs, notes, "" if ok else "  <-- WRONG"))
            if parsed.get("reason"):
                print("   reason: " + parsed["reason"][:300])
            if not ok:
                failures.append(label)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    if failures:
        print("CANARY FAILED: the %s reviewer gave the wrong verdict on the %s case(s). The gate may be rubber-stamping or "
              "refusing everything; check the model id, the profile and the brief." % (rev.get("label", rev["route"]), ", ".join(failures)))
        return 1
    print("CANARY OK: the %s reviewer stops a bad design and passes a good one." % rev.get("label", rev["route"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
