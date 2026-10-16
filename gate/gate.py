#!/usr/bin/env python3
"""Pushback Gate hook entry point.

Usage (from a hooks config):  gate.py <client> <event>
  client: claude | codex
  event:  UserPromptSubmit | PreToolUse | Stop

Reads the hook JSON on stdin, transitions the session state inside one lock, prints the client's JSON reply.
Every failure path falls open: a broken gate must never block a prompt or hang a turn.

State per generation (one owner prompt that needed a consult):
  status: classifying | preparing | pending | done | failed | skipped | superseded
  deadline: last arrival time for an enforceable verdict; a timely STOP persists until resolved
  delivered / stop_blocks / diff_checked: once-only latches, claimed inside the lock
"""
import json
import os
import subprocess
import sys
import time
import shlex

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
    if sys.argv[1] == "restore":
        # gate.py restore <sha-or-ref> [folder]   (run by a person or the builder after a STOP)
        ok, note = G.restore(sys.argv[3] if len(sys.argv) > 3 else os.getcwd(), sys.argv[2])
        print(("restored: " if ok else "restore failed: ") + note)
        sys.exit(0 if ok else 1)
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
    control = G.prompt_control(prompt)
    code_project = G.looks_like_code_project(cwd, policy)
    mode = G.detect_mode(prompt, policy)
    decision, why = ("SKIP", "mode skip ('just do it')") if mode == "skip" else G.classify_rules(prompt, policy)
    if mode == "wait" and control not in ("cancel", "ack", "choice"):
        decision, why = "CONSULT", "explicit wait for review"
    if control in ("cancel", "override"):
        decision, why = "SKIP", control
    if not code_project:
        decision, why = "SKIP", "not a code folder"

    def start(st):
        """Allocate the generation, supersede a pending predecessor, collect an undelivered verdict: one lock."""
        carry = None
        prev = state.current(st)
        resolved_choice = bool(prev and G.stop_enforced(prev) and prev.get("delivered") and control == "choice")
        if prev and control in ("ack", "choice") and not resolved_choice and (prev.get("status") in ("pending", "classifying", "preparing") or G.stop_enforced(prev)):
            return None, None
        if prev and G.stop_enforced(prev) and decision == "SKIP" and control not in ("cancel", "override") and prompt.strip().endswith("?"):
            return None, None  # questions about an objection are not consent or replacement work
        if prev:
            if control == "override" or resolved_choice:
                prev["overridden"] = True
                prev["override_prompt"] = prompt[:500]
            if prev.get("status") in ("pending", "classifying", "preparing"):
                prev["status"] = "superseded"
            elif prev.get("status") == "done" and not prev.get("delivered") and control not in ("cancel", "override") and not resolved_choice and prev.get("verdict") != "GO":
                prev["delivered"] = True
                carry = render_verdict(prev, late=True)
        gen_no = int(st.get("generation", 0)) + 1
        st["generation"] = gen_no
        g = G.SessionState.gen(st, gen_no)
        g.update({"gen": gen_no, "prompt_fp": G.prompt_fingerprint(prompt), "prompt_head": prompt.strip()[:160], "cwd": cwd,
                  "mode": mode, "decision": decision, "why": why,
                  "status": "skipped" if decision == "SKIP" else ("classifying" if decision == "AMBIGUOUS" else "pending"),
                  "started": G.now_iso(), "started_ts": G.now_ts(),
                  "deadline": G.now_ts() + policy["wait_mode_seconds" if mode == "wait" else "verdict_gate_seconds"],
                  "delivered": False, "stop_blocks": 0, "restorable": False})
        return gen_no, carry

    gen_no, carry = state.update(start)
    if gen_no is None:
        return
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
        sha, ref, note = G.checkpoint(cwd, "consult-%s-%s-%d" % (CLIENT, G.prompt_fingerprint(sid), gen_no), policy)
        root = G.git_root(cwd)
        state.update(lambda st: G.SessionState.gen(st, gen_no).update(
            {"checkpoint": sha, "checkpoint_ref": ref, "checkpoint_note": note, "root": root or cwd, "restorable": bool(sha)}))

    env = dict(os.environ)
    env[G.INTERNAL_ENV] = "1"
    try:
        with open(os.path.join(state.dir, "worker-%d.log" % gen_no), "ab") as lf:
            p = subprocess.Popen([sys.executable, WORKER, CLIENT, sid, str(gen_no)], stdin=subprocess.DEVNULL,
                                 stdout=lf, stderr=subprocess.STDOUT, env=env, start_new_session=True, cwd=cwd)
        state.update(lambda st: G.SessionState.gen(st, gen_no).update({"worker_pid": p.pid}))
    except Exception as e:
        G.log("worker launch failed: %r" % e)
        state.update(lambda st: G.SessionState.gen(st, gen_no).update({"status": "failed", "failure_delivered": True, "error": "launch: %r" % e}))
        return emit_prompt_context("\n".join(extra + ["Pushback Gate: review could not start; proceeding without a second opinion. Mention this once if material to the work, without asking routine permission."]))

    rev = policy["reviewer_by_client"][CLIENT]["label"]
    head = ("Pushback Gate: this prompt is being classified; if it embodies a design decision, an independent consult (%s) "
            "runs in parallel." % rev if decision == "AMBIGUOUS" else
            "Pushback Gate: an independent consult (%s) is running in parallel on this request (%s)." % (rev, why))
    rules = [head,
        "Internal context only: do not announce routine review status, success statements or agreement. "
        "Read and investigate while classification or checkpoint preparation is pending. "
        "Compare the requested outcome and reviewer's goal internally; distinguish explicit constraints from casual context. "
        "Surface only a material mismatch, consequential objection, or a failure that affects the work. "
        "Platform permissions and the owner's explicit decisions still govern. "
        + ("Hold edits until the verdict or the documented wait deadline." if mode == "wait" else
           "Once preparation finishes, reversible work may proceed; consequential actions wait for review.")]
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
        call_cwd = os.path.realpath(data.get("cwd") or g.get("cwd") or os.getcwd())
        root = os.path.realpath(g.get("root") or g.get("cwd") or call_cwd)
        target = tool_input.get("file_path") or tool_input.get("path") if isinstance(tool_input, dict) else None
        wrong_target = target and os.path.isabs(target) and not (os.path.realpath(target) == root or os.path.realpath(target).startswith(root + os.sep))
        if wrong_target and kind != "read":
            if not g.get("context_warning_delivered"):
                g["context_warning_delivered"] = True
                return "context", "Pushback Gate: this operation targets a different repository from %s. This review does not govern that target; it still applies to work inside its reviewed repository." % root
            return None, None
        now = G.now_ts()
        past_deadline = now > float(g.get("deadline") or 0)
        enforce = G.stop_enforced(g) if status == "done" else not past_deadline and not g.get("released")
        if status == "failed":
            if not g.get("failure_delivered"):
                g["failure_delivered"] = True
                return "context", "Pushback Gate: no second opinion (%s); proceeding unreviewed. Mention once if material; this is not a permission request." % g.get("error", "review failed")[:200]
            return None, None

        if status == "done" and not g.get("delivered"):
            g["delivered"] = True
            g["delivered_at"] = G.now_iso()
            text = render_verdict(g, late=bool(g.get("late") or g.get("released")))
            if g.get("verdict") == "STOP" and enforce and kind != "read":
                return "deny", text + "\n(This %s call was blocked. Reads and questions are still allowed.)" % tool
            return "context", text

        if status == "done" and g.get("verdict") == "STOP" and enforce and not g.get("overridden"):
            if kind != "read":
                return "deny", ("Pushback Gate: STOP verdict stands for this request (consult %d). Do not implement; present both "
                                "sides to the owner and end with STATUS: needs-you. Reads are allowed." % gen_no)
            return None, None

        if status in ("pending", "classifying", "preparing"):
            cap = policy["wait_mode_seconds" if g.get("mode") == "wait" else "verdict_gate_seconds"]
            age = int(now - float(g.get("started_ts") or (float(g.get("deadline") or now) - cap)))
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
            if kind != "read" and (status in ("classifying", "preparing") or not g.get("restorable")):
                return "deny", ("Pushback Gate: classification/checkpoint preparation is pending; investigate with read-only tools." if status in ("classifying", "preparing") else "Pushback Gate: no restorable checkpoint is available (%s). Read-only work can continue; mutations wait for the verdict or deadline." % g.get("checkpoint_note", "checkpoint unavailable"))
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
    if g0.get("status") == "pending" and G.now_ts() < float(g0.get("deadline") or 0):
        time.sleep(min(policy["stop_grace_seconds"], 10))

    def decide(st):
        g = state.current(st)
        if not g or g.get("status") in ("skipped", "superseded"):
            return None, None
        gen_no = st["generation"]
        if g.get("stop_blocks", 0) >= policy["stop_blocks_max_per_generation"]:
            return None, None
        status = g.get("status")
        enforce = G.stop_enforced(g)
        if status == "failed" and not g.get("failure_delivered"):
            g["failure_delivered"] = True
            g["stop_blocks"] = g.get("stop_blocks", 0) + 1
            return "block", "Pushback Gate: review failed; finish with one brief unreviewed notice if material. Do not request routine permission or restart the review."
        if status in ("pending", "classifying", "preparing", "failed"):
            return None, None  # let the turn end; whatever arrives is shown at the next prompt
        if status != "done":
            return None, None
        if not g.get("delivered"):
            g["delivered"] = True
            g["delivered_at"] = G.now_iso()
            if g.get("verdict") != "GO":
                g["stop_blocks"] = g.get("stop_blocks", 0) + 1
                text = render_verdict(g, late=bool(g.get("late") or g.get("released")))
                return "block", text + "\nFinish with only material implications or disagreement; do not narrate routine agreement."
        if not g.get("diff_checked") and g.get("verdict") in ("GO", "GO-WITH-NOTES"):
            g["diff_checked"] = True
            res = G.diff_check(g.get("root"), g.get("checkpoint"), g.get("shape", ""), policy)
            g["diff"] = {k: v for k, v in res.items() if k != "new_deps"}
            if res["departures"] and (res["new_deps"] or res["lines"] > policy["size_bands_lines"]["small"]):
                g["stop_blocks"] = g.get("stop_blocks", 0) + 1
                return "block", ("Pushback Gate diff check (consult %d): the finished change departs from the reviewer's proposed "
                                 "shape:\n- " % gen_no + "\n- ".join(res["departures"]) +
                                 "\nAssess these internally. Mention only material unintended changes or consequential disagreement. The shape is advisory; do not add a disposition checklist. Then finish.")
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
    parts.append("Reviewed request: %s\nRepository: %s" % (g.get("prompt_head", "unknown"), g.get("root") or g.get("cwd", "unknown")))
    if v == "GO":
        return "\n".join(parts) + "\nInternal agreement only; no user-visible review announcement is needed."
    if v == "STOP" and (not g.get("goal") or g.get("context_status", "MATCH") != "MATCH"):
        parts.append("Advisory only: goal/repository context is missing or uncertain. Resolve the material mismatch before applying conclusions; do not treat it as an architectural veto.")
    elif v == "STOP" and not late:
        parts.append("Pause implementation and present the consequential objection, alternative and your assessment briefly. The owner's informed choice governs. Thanks-type acknowledgements are not overrides. Once the objection is delivered, an ordinary yes or go-ahead resolves the hold; follow the direction in the conversation without repeating the objection.")
    if g.get("reason"):
        parts.append("Reason: " + g["reason"])
    if g.get("goal"):
        parts.append("Goal as the reviewer read it: " + g["goal"] +
                     "\nCompare internally with the owner's actual goal and explicit constraints. Mention only a material mismatch; casual context is not a hard constraint.")
    if g.get("notes"):
        parts.append("Notes:\n" + g["notes"])
        parts.append("Discuss only notes that materially change the work or that you reject; no per-note adoption ritual.")
    if g.get("shape"):
        parts.append("Proposed shape:\n" + g["shape"])
    if v == "UNPARSED" and g.get("raw"):
        parts.append("Raw reviewer text:\n" + g["raw"][:4000])
    if v == "STOP" and g.get("checkpoint") and g.get("root"):
        parts.append("Snapshot of covered files: %s. If rollback is chosen: python3 %s restore %s %s. Restore first saves a recovery snapshot and refuses unsupported paths; ignored files/submodules are outside coverage." % (g["checkpoint"], shlex.quote(os.path.abspath(__file__)), shlex.quote(g["checkpoint"]), shlex.quote(g["root"])))
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
