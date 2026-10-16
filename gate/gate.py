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
WAIVE_CMD = "python3 %s waive" % shlex.quote(os.path.join(HERE, "gate.py"))
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
    if sys.argv[1] == "waive":
        sys.exit(waive_command(sys.argv[2:]))
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
    G.flush_notices(policy)
    state = G.SessionState(CLIENT, sid)
    control = G.prompt_control(prompt)
    code_project = G.looks_like_code_project(cwd, policy)
    mode = G.detect_mode(prompt, policy)
    about_review = G.is_about_review(prompt, policy)
    if mode == "skip" or control == "override":
        # The owner said, in whatever words matched, not to review: that waives the running review too.
        def owner_waive(st):
            prev = state.current(st)
            if prev and (prev.get("status") in G.ACTIVE_STATUSES or G.stop_enforced(prev)):
                ok, msg = G.apply_waiver(prev, "owner", "owner prompt: " + prompt.strip()[:160])
                if ok:
                    G.waivers_log("%s %s consult %s waived by owner (prompt)" % (CLIENT, sid, prev.get("gen")))
        state.update(owner_waive)
    if about_review and mode != "skip" and control != "override":
        # Talking about the review never starts a new one or replaces the running one.
        return
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
        return emit_prompt_context("\n".join(extra + [failure_notice({"error": "the review could not start: %r" % e})]))

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
        "If the owner says in any words not to review this request, run `%s --by owner \"<his words, briefly>\"` and carry on; "
        "his waiver is final. If you are confident the change is minor (small, no new dependency, no interface or data change), "
        "you may run `%s --by agent \"<one-line reason>\"`; you may not waive a review the owner asked for or a delivered STOP. "
        "Agent waivers are logged and the finished change is measured against them. "
        % (WAIVE_CMD, WAIVE_CMD)
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
    waive = parse_waive(G.bash_command(tool_input)) if tool in policy["shell_tools"] else None
    if waive:
        by, reason = waive
        def do_waive(st):
            g = state.current(st)
            if not g:
                return False, "no review in this session"
            ok, msg = G.apply_waiver(g, by, reason)
            if ok and g.get("waived", {}).get("at"):
                G.waivers_log("%s %s consult %s waived by %s: %s" % (CLIENT, sid, g.get("gen"), by, reason[:200]))
            return ok, msg
        ok, msg = state.update(do_waive)
        context("Pushback Gate: %s." % msg if ok else "Pushback Gate: waiver refused: %s." % msg)
        return

    def decide(st):
        """Everything is decided on the live state inside the lock; returns (action, text)."""
        g = state.current(st)
        if not g or g.get("status") in ("skipped", "superseded", "waived", None):
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
                return "context", failure_notice(g)
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
                                "sides to the owner and end with STATUS: needs-you. Reads are allowed. If the owner then says to go "
                                "ahead, in any words, run `%s --by owner \"<his words>\"`." % (gen_no, WAIVE_CMD))
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
    if g0.get("status") == "waived":
        return audit_agent_waiver(state, policy)
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
            return "block", failure_notice(g) + " Do not restart the review."
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


def failure_notice(g):
    return ("Pushback Gate: the second-opinion review FAILED (%s). The build continues, unreviewed. Tell the owner in one line "
            "of your reply that this request was not reviewed and why; this is a notice, not a permission request."
            % (g.get("error") or "unknown error")[:200])


def audit_agent_waiver(state, policy):
    """A builder that waived review as minor is checked against the size of what it actually changed."""
    def decide(st):
        g = state.current(st)
        w = (g or {}).get("waived") or {}
        if not g or w.get("by") != "agent" or g.get("waiver_audited"):
            return None
        g["waiver_audited"] = True
        res = G.diff_check(g.get("root") or G.git_root(g.get("cwd") or "."), g.get("checkpoint") or "HEAD", "", policy)
        g["waiver_diff"] = {"lines": res["lines"], "files": res["files"][:40], "new_deps": res["new_deps"][:10]}
        big = res["lines"] > policy["size_bands_lines"]["small"]
        if not big and not res["new_deps"]:
            return None
        G.waivers_log("%s consult %s agent waiver exceeded: %d lines, new deps %s" % (CLIENT, g.get("gen"), res["lines"], res["new_deps"][:5]))
        why = []
        if big:
            why.append("%d changed lines (minor means under %d)" % (res["lines"], policy["size_bands_lines"]["small"]))
        if res["new_deps"]:
            why.append("possible new dependencies: %s" % "; ".join("%s: %s" % (f, ", ".join(d)) for f, d in res["new_deps"][:5]))
        return ("Pushback Gate: you waived review as minor (%s), but the finished change has %s. Tell the owner in one line "
                "and offer a review after the fact; do not run it unless he says so." % (w.get("reason", "")[:120], "; ".join(why)))
    text = state.update(decide)
    if text:
        block_stop(text)


def parse_waive(cmd):
    """Recognise `gate.py waive --by owner|agent "reason"` inside a shell command."""
    import re
    if not cmd or not re.search(r"gate\.py['\"]?\s+waive\b", cmd):
        return None
    # Only an actual invocation counts (python3 .../gate.py waive ... as its own statement), never the same words
    # inside an echo, a heredoc or a document the builder is writing.
    for statement in G._statements(cmd):
        toks = G._tokens(statement)
        if len(toks) >= 2 and os.path.basename(toks[0]) == "gate.py" and toks[1] == "waive":
            return _waive_args(toks[2:])[:2]
        if len(toks) >= 3 and os.path.basename(toks[0]) in ("python", "python3") and os.path.basename(toks[1]) == "gate.py" and toks[2] == "waive":
            return _waive_args(toks[3:])[:2]
    return None


def _waive_args(args):
    by, reason, session, client = "agent", [], None, None
    it = iter(args)
    for a in it:
        if a == "--by":
            by = next(it, "agent")
        elif a == "--session":
            session = next(it, None)
        elif a == "--client":
            client = next(it, None)
        else:
            reason.append(a)
    return by, " ".join(reason), session, client


def waive_command(args):
    """Standalone: gate.py waive --by owner|agent "reason" [--session ID --client claude|codex].
    Inside a hooked session the PreToolUse hook has already applied it to the right session; this finds the most
    recent review for the current folder otherwise."""
    by, reason, session, client = _waive_args(args)
    here = os.path.realpath(os.getcwd())
    candidates = []
    if session and client:
        candidates = [G.SessionState(client, session)]
    else:
        try:
            for name in os.listdir(G.SESSIONS_DIR):
                st = G.SessionState("x", "x")
                st.dir = os.path.join(G.SESSIONS_DIR, name)
                st.path = os.path.join(st.dir, "state.json")
                st.lock_path = os.path.join(st.dir, "state.lock")
                g = st.current()
                if not g:
                    continue
                root = os.path.realpath(g.get("root") or g.get("cwd") or "/nonexistent")
                if here == root or here.startswith(root + os.sep) or root.startswith(here + os.sep):
                    candidates.append((float(g.get("started_ts") or 0), st))
        except FileNotFoundError:
            pass
        # A hooked session's own PreToolUse hook applies the waiver before this command runs; report that one
        # rather than reaching for another session's review in the same folder.
        for _, s in candidates:
            w = (s.current() or {}).get("waived") or {}
            if w.get("at") and time.time() - time.mktime(time.strptime(w["at"], "%Y-%m-%dT%H:%M:%SZ")) + time.timezone < 60:
                print("review already waived by the %s" % w.get("by"))
                return 0
        candidates = [s for _, s in sorted(candidates, key=lambda x: x[0], reverse=True)[:1]]
    if not candidates:
        print("nothing to waive: no review found for %s" % here)
        return 0
    state = candidates[0]

    def do(st):
        g = state.current(st)
        if not g:
            return False, "no review found"
        if g.get("status") == "waived":
            return True, "review already waived by the %s" % g["waived"].get("by")
        ok, msg = G.apply_waiver(g, by, reason)
        if ok and g.get("waived"):
            G.waivers_log("standalone %s consult %s waived by %s: %s" % (os.path.basename(state.dir), g.get("gen"), by, reason[:200]))
        return ok, msg
    ok, msg = state.update(do)
    print(msg if ok else "waiver refused: " + msg)
    return 0 if ok else 1


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
