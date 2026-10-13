#!/usr/bin/env python3
"""Pushback Gate hook entry point.

Usage (from a hooks config):  gate.py <client> <event>
  client: claude | codex
  event:  UserPromptSubmit | PreToolUse | Stop

Reads the hook JSON on stdin, transitions the session state inside one lock, prints the client's JSON reply.
Every failure path falls open: a broken gate must never block a prompt or hang a turn.

State per generation (one owner prompt that needed a consult):
  status: classifying | pending | done | failed | skipped | superseded
  deadline: absolute time after which the verdict is advisory only (never enforced)
  delivered / stop_blocks / diff_checked: once-only latches, claimed inside the lock
"""
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gatelib as G  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
WORKER = os.path.join(HERE, "review_worker.py")
CLIENT = "claude"
EVENT = ""


def main():
    global CLIENT, EVENT
    if len(sys.argv) < 3:
        return
    CLIENT, EVENT = sys.argv[1], sys.argv[2]
    if G.is_internal():
        return
    data = G.read_json_stdin()
    try:
        policy = G.load_policy()
        if not policy.get("enabled", True) and os.environ.get("PUSHBACK_GATE_FORCE_ENABLED") != "1":
            return
        handler = {"UserPromptSubmit": on_prompt, "PreToolUse": on_pre_tool, "Stop": on_stop}.get(EVENT)
        if handler:
            handler(data, policy)
    except G.LockTimeout:
        G.log("%s %s: state lock busy, falling open" % (CLIENT, EVENT))
    except Exception as e:  # fail open
        G.log("%s %s hook error: %r" % (CLIENT, EVENT, e))


# ---------------------------------------------------------------- UserPromptSubmit

def on_prompt(data, policy):
    prompt = data.get("prompt") or ""
    cwd = data.get("cwd") or os.getcwd()
    sid = data.get("session_id") or "unknown"
    if not prompt.strip() or G.is_machine_prompt(prompt, policy):
        return  # not an owner prompt: no new generation, nothing superseded
    state = G.SessionState(CLIENT, sid)
    code_project = G.looks_like_code_project(cwd, policy)
    mode = G.detect_mode(prompt, policy)
    decision, why = ("SKIP", "mode skip ('just do it')") if mode == "skip" else G.classify_rules(prompt, policy)
    if not code_project:
        decision, why = "SKIP", "not a code folder"

    def start(st):
        """Allocate the generation, supersede a pending predecessor, collect an undelivered verdict: one lock."""
        carry = None
        prev = state.current(st)
        if prev:
            if prev.get("status") in ("pending", "classifying"):
                prev["status"] = "superseded"
            elif prev.get("status") == "done" and not prev.get("delivered"):
                prev["delivered"] = True
                carry = render_verdict(prev, late=True)
        gen_no = int(st.get("generation", 0)) + 1
        st["generation"] = gen_no
        g = G.SessionState.gen(st, gen_no)
        g.update({"gen": gen_no, "prompt_fp": G.prompt_fingerprint(prompt), "prompt_head": prompt.strip()[:160], "cwd": cwd,
                  "mode": mode, "decision": decision, "why": why,
                  "status": "skipped" if decision == "SKIP" else ("classifying" if decision == "AMBIGUOUS" else "pending"),
                  "started": G.now_iso(), "deadline": G.now_ts() + policy["verdict_gate_seconds"],
                  "delivered": False, "stop_blocks": 0, "restorable": False})
        return gen_no, carry

    gen_no, carry = state.update(start)
    extra = [carry] if carry else []
    if decision == "SKIP":
        return emit_prompt_context("\n".join(extra))

    try:
        with open(os.path.join(state.dir, "prompt-%d.txt" % gen_no), "w") as pf:
            pf.write(prompt)
    except Exception as e:
        G.log("prompt file write failed: %r" % e)

    note = "deferred until classification says consult"
    if decision == "CONSULT":
        sha, ref, note = G.checkpoint(cwd, "consult-%d" % gen_no, policy)
        root = G.git_root(cwd)
        state.update(lambda st: G.SessionState.gen(st, gen_no).update(
            {"checkpoint": sha, "checkpoint_ref": ref, "checkpoint_note": note, "root": root, "restorable": bool(sha)}))

    env = dict(os.environ)
    env[G.INTERNAL_ENV] = "1"
    try:
        with open(os.path.join(state.dir, "worker-%d.log" % gen_no), "ab") as lf:
            p = subprocess.Popen([sys.executable, WORKER, CLIENT, sid, str(gen_no)], stdin=subprocess.DEVNULL,
                                 stdout=lf, stderr=subprocess.STDOUT, env=env, start_new_session=True, cwd=cwd)
        state.update(lambda st: G.SessionState.gen(st, gen_no).update({"worker_pid": p.pid}))
    except Exception as e:
        G.log("worker launch failed: %r" % e)
        state.update(lambda st: G.SessionState.gen(st, gen_no).update({"status": "failed", "error": "launch: %r" % e}))
        return emit_prompt_context("\n".join(extra))

    rev = policy["reviewer_by_client"][CLIENT]["label"]
    head = ("Pushback Gate: this prompt is being classified; if it embodies a design decision, an independent consult (%s) "
            "runs in parallel." % rev if decision == "AMBIGUOUS" else
            "Pushback Gate: an independent consult (%s) is running in parallel on this request (%s)." % (rev, why))
    rules = [
        head,
        "Mode: %s. %s" % (mode, "Hold all edits until the verdict arrives." if mode == "wait" else "Start work now."),
        "Checkpoint: %s." % note,
        "While the verdict is pending (up to %d s): pushes, raw deletes, installs, migrations, deploys and outward sends are "
        "blocked; trash-style deletes, local commits, reads and ordinary edits pass. A verdict is handed to you with your next tool call. "
        "GO-WITH-NOTES: weigh the notes and say in one line per note whether you adopted it. STOP: stop implementing, give "
        "the owner both sides in a few lines (the request, the reviewer's objection and alternative, your own view) and end with "
        "STATUS: needs-you; reads and questions stay allowed. The reviewer saw the request plus read-only repository access: weigh "
        "its view, do not defer to it. The owner's explicit choice outranks the reviewer." % policy["verdict_gate_seconds"],
    ]
    emit_prompt_context("\n".join(extra + rules))


# ---------------------------------------------------------------- PreToolUse

def on_pre_tool(data, policy):
    sid = data.get("session_id") or "unknown"
    state = G.SessionState(CLIENT, sid)
    tool = data.get("tool_name") or ""
    tool_input = data.get("tool_input") or {}
    kind = G.tool_kind(tool, tool_input, policy)

    def decide(st):
        """Everything is decided on the live state inside the lock; returns (action, text)."""
        g = state.current(st)
        if not g or g.get("status") in ("skipped", "superseded", None):
            return None, None
        gen_no = st["generation"]
        status = g.get("status")
        now = G.now_ts()
        past_deadline = now > float(g.get("deadline") or 0)
        enforce = not past_deadline and not g.get("released")

        if status == "done" and not g.get("delivered"):
            g["delivered"] = True
            g["delivered_at"] = G.now_iso()
            text = render_verdict(g, late=not enforce or g.get("late"))
            if g.get("verdict") == "STOP" and enforce and kind != "read":
                return "deny", text + "\n(This %s call was blocked. Reads and questions are still allowed.)" % tool
            return "context", text

        if status == "done" and g.get("verdict") == "STOP" and enforce and not g.get("overridden"):
            if kind != "read":
                return "deny", ("Pushback Gate: STOP verdict stands for this request (consult %d). Do not implement; present both "
                                "sides to the owner and end with STATUS: needs-you. Reads are allowed." % gen_no)
            return None, None

        if status in ("pending", "classifying"):
            age = int(now - (float(g.get("deadline") or now) - policy["verdict_gate_seconds"]))
            if not enforce:
                if not g.get("released"):
                    g["released"] = True
                    return "context", ("Pushback Gate: no verdict after %d s; the gate is released. A late verdict will still be "
                                       "shown as advisory." % age)
                return None, None
            if g.get("mode") == "wait" and age < policy["wait_mode_seconds"] and kind != "read":
                return "deny", ("Pushback Gate: wait mode, review pending (%d s elapsed, cap %d s). Investigate and plan; edits "
                                "are held until the verdict." % (age, policy["wait_mode_seconds"]))
            if kind == "irreversible":
                return "deny", ("Pushback Gate: consult %d pending (%d s); this looks irreversible (push, delete, install, "
                                "migration, deploy or send). Do it after the verdict, or use a trash-style delete tool (policy: reversible_delete_tools)." % (gen_no, age))
            if kind == "edit" and status == "pending" and not g.get("restorable") and is_dirty_target(tool_input, g):
                return "deny", ("Pushback Gate: consult %d pending; this file has uncommitted changes and no checkpoint could be "
                                "taken, so edits to it wait for the verdict. Other files are fine." % gen_no)
        return None, None

    action, text = state.update(decide)
    if action == "deny":
        deny(text)
    elif action == "context":
        context(text)


def is_dirty_target(tool_input, g):
    path = tool_input.get("file_path") or tool_input.get("path") or ""
    root = g.get("root")
    if not path or not root:
        return False
    rc, out, _ = G.git(["status", "--porcelain", "--", path], root, timeout=5)
    return rc == 0 and bool(out.strip())


# ---------------------------------------------------------------- Stop

def on_stop(data, policy):
    sid = data.get("session_id") or "unknown"
    state = G.SessionState(CLIENT, sid)
    g0 = state.current()
    if not g0 or g0.get("status") in ("skipped", "superseded"):
        return
    # One bounded grace sleep for a consult that is seconds from done (no polling loop, no continuation).
    if g0.get("status") in ("pending", "classifying") and G.now_ts() < float(g0.get("deadline") or 0):
        time.sleep(min(policy["stop_grace_seconds"], 10))

    def decide(st):
        g = state.current(st)
        if not g or g.get("status") in ("skipped", "superseded"):
            return None, None
        gen_no = st["generation"]
        if g.get("stop_blocks", 0) >= policy["stop_blocks_max_per_generation"]:
            return None, None
        status = g.get("status")
        enforce = G.now_ts() <= float(g.get("deadline") or 0) and not g.get("released")
        if status in ("pending", "classifying", "failed"):
            return None, None  # let the turn end; whatever arrives is shown at the next prompt
        if status != "done":
            return None, None
        if not g.get("delivered"):
            g["delivered"] = True
            g["delivered_at"] = G.now_iso()
            g["stop_blocks"] = g.get("stop_blocks", 0) + 1
            text = render_verdict(g, late=not enforce or g.get("late"))
            ask = ("Address this before finishing: for STOP, present both sides and end with STATUS: needs-you; for notes, say in "
                   "one line each whether you adopted them.")
            return "block", text + "\n" + ask
        if not g.get("diff_checked") and g.get("verdict") in ("GO", "GO-WITH-NOTES"):
            g["diff_checked"] = True
            res = G.diff_check(g.get("root"), g.get("checkpoint"), g.get("shape", ""), policy)
            g["diff"] = {k: v for k, v in res.items() if k != "new_deps"}
            if res["departures"]:
                g["stop_blocks"] = g.get("stop_blocks", 0) + 1
                return "block", ("Pushback Gate diff check (consult %d): the finished change departs from the reviewer's proposed "
                                 "shape:\n- " % gen_no + "\n- ".join(res["departures"]) +
                                 "\nState a one-line disposition for each departure in your final message (kept, and why; or "
                                 "reverted). The shape is advisory, so a justified departure is fine. Then finish.")
        return None, None

    action, text = state.update(decide)
    if action == "block":
        block_stop(text)


# ---------------------------------------------------------------- rendering and replies

def render_verdict(g, late=False):
    v = g.get("verdict") or "UNPARSED"
    rev = g.get("reviewer") or "the reviewer"
    head = "Pushback Gate verdict (consult %s, %s%s): %s%s" % (
        g.get("gen", "?"), rev, ", late, advisory only" if late else "", v,
        "" if v != "UNPARSED" else " (reviewer answer did not follow the format; treat as advisory)")
    parts = [head]
    if g.get("reason"):
        parts.append("Reason: " + g["reason"])
    if g.get("goal"):
        parts.append("Goal as the reviewer read it: " + g["goal"])
    if g.get("notes"):
        parts.append("Notes:\n" + g["notes"])
    if g.get("shape"):
        parts.append("Proposed shape:\n" + g["shape"])
    if v == "UNPARSED" and g.get("raw"):
        parts.append("Raw reviewer text:\n" + g["raw"][:4000])
    return "\n".join(parts)


def emit_prompt_context(text):
    if text and text.strip():
        G.emit({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": text}})


def context(text):
    # PreToolUse: both clients accept additionalContext without a permission decision.
    G.emit({"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": text}})


def deny(reason):
    G.emit({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}})


def block_stop(reason):
    # Stop: only decision/reason is valid on both clients (Codex rejects hookSpecificOutput here).
    G.emit({"decision": "block", "reason": reason})


if __name__ == "__main__":
    main()
