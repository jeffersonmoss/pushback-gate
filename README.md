# Pushback Gate

Pushback Gate gets an independent review when a coding request prescribes a consequential implementation choice. Its purpose is useful objections before costly work, with little interruption when the proposed approach is sound.

The builder compares the requested outcome, explicit constraints, and reviewer's interpretation internally. Routine success statements, review announcements, and agreement reports are unnecessary. A material mismatch or objection still belongs in the conversation. Casual background information is not a hard requirement, and a tradeoff the owner has knowingly accepted should not be raised again without new evidence.

## How it works

1. `UserPromptSubmit` classifies the request. Clear questions, routine fixes, acknowledgements, and cancellations skip review. Short or politely phrased implementation requests can still trigger it. A small model classifies ambiguous requests.
2. Before consequential edits, the gate records a working-tree checkpoint in an existing Git repository. Reads remain available during classification and checkpoint preparation. The gate does not initialize repositories by default.
3. A detached worker sends the request, recent owner context, and capped repository evidence to the other vendor's CLI. Claude Code builds use Codex; Codex builds use Claude Code. The reviewer has read-only tools.
4. `PreToolUse` delivers the verdict and enforces any applicable hold. `GO` requires no public ceremony. `GO-WITH-NOTES` carries material implementation advice. A timely, context-matched `STOP` pauses mutations so the owner can decide.
5. The Stop hook can deliver an outstanding material verdict or flag a mechanical departure from the proposed shape. Its continuation count is bounded. Shape estimates are advisory, and the comparison does not understand all semantic changes.

## Decisions and timing

- Fast mode allows reversible work once the checkpoint is ready, while holding recognized irreversible operations until the verdict. Unknown scripts may have effects that the classifier cannot determine.
- Explicit “review this first” requests use wait mode and hold non-read tools until a verdict or the 240-second deadline. The default arrival deadline is 180 seconds.
- A STOP received before its deadline persists until the request is cancelled, replaced, or explicitly overridden. “Thanks” does not erase it; after the objection is delivered, ordinary decision replies such as “yes” or “go ahead” resolve the hold. Questions about the objection do not authorize implementation.
- The builder interprets ordinary replies in the conversation; no special override phrase is required. An informed instruction such as “I accept the tradeoff; proceed” or “do it anyway” overrides the gate. A new substantive correction starts a new generation. “Just do it” and “skip the review” remain explicit review bypasses.
- A verdict arriving after release is advisory. Worker failure permits progress with one brief notice that there was no second opinion. Gate exceptions fail open and are logged.
- A missing goal, uncertain repository, or tool target outside the reviewed repository cannot receive an architectural veto from that review. Semantic goal alignment still depends on model judgment.

## Checkpoints and restore

Checkpoints use a temporary Git index and unique `refs/pushback-gate/...` references. They preserve the branch and staging area. The verdict's restore command names an immutable commit SHA, not a shared generation label.

Restore first creates and retains a recovery snapshot of the current covered working tree, including unstaged tracked content. If recovery fails, restore stops before changing files. Files absent from the requested snapshot are moved to the gate's state directory. The output includes the recovery SHA, so a restore can itself be undone with the same command.

Ignored files and submodule contents are outside snapshot coverage. Automatic restore refuses symlinks, submodules, unsafe paths, and uncovered collisions. It is not atomic against an unrelated process editing the same worktree. An interrupted checkout can be partial; retain the reported recovery snapshot and inspect before proceeding.

## Installation and local updates

Requires Python 3, Git, Claude Code, and Codex CLI with existing signed-in accounts. The source was exercised with Claude Code 2.1.289 and Codex CLI 0.160.0. The implementation uses CLI subscription routes; API integration is not required.

Copy `gate/` to `~/.claude/scripts/pushback_gate/`. Merge the matching entries from `install/claude-settings-hooks.json` and `install/codex-hooks.json` into the respective client hook files without replacing unrelated hooks. The commands point to that shared installed directory.

The Codex review profile is `install/codex-review.config.toml`, installed as `~/.codex/review.config.toml`. It sets read-only execution and no interactive approval. Verify the installed CLI's profile behavior before a fresh installation.

Before updating an existing installation, save copies and hashes of its source and hook configuration. Update only gate-owned files. Existing detached workers may finish using code already loaded; new worker processes use the updated source. No running work is automatically cancelled by a file update.

`policy.json` controls reviewers, classification rules, timing, and shape thresholds. Existing model aliases and the optional council roster can resolve to different versions; the worker records the model requested. The Codex route retains its configured fallback list. This is separate from an explicitly named model review, which must verify the actual response identity and must not silently substitute.

Disable with `PUSHBACK_GATE_DISABLED=1` for a shell or `"enabled": false` in the installed policy. State and worker logs live in `~/.local/state/pushback_gate/`.

## Validation and limits

The deterministic suites are `gate/tests/test_gate.py` and `gate/tests/test_regressions.py`. Run them in a disposable filesystem sandbox with network denied, a dedicated temporary directory, and writes restricted to that directory. They exercise real Git restores and clean up their own fixtures. They use fake model answers and cannot establish reviewer quality.

Live evaluation must assess both useful objections and false alarms. Include a justified change, a harmful proposal, an informed tradeoff, casual context, cancellation, and small requests. Keep fixture expectations out of the reviewer prompt. Test actual tool delivery separately from response-only experiments.

Known limits:

- Shell classification is heuristic, not a security boundary. Complex wrappers, arbitrary programs, aliases, and existing interactive shells can evade effect classification.
- Hosted tools or application surfaces that do not call these local hooks are not covered. Registering hooks does not prove that every client path invokes them.
- Checkpoint and pre-restore recovery references currently have no automatic expiry; they retain non-ignored snapshot contents until deliberately removed.
- Already running commands cannot be recalled. Concurrent sessions can change the same worktree; the gate's repository lock coordinates its own checkpoints/restores only.
- Goal and context checks do not prove semantic understanding. A model can still invent constraints, miss a defect, or raise a false alarm.
- The mechanical diff check cannot detect public-interface semantics. Dependency version changes may resemble new dependencies. Unrelated changes in a shared worktree may be attributed to the current review.
- Timing and reviewer accuracy vary. A small synthetic pilot is evidence about those cases, not an estimate of real-world false-positive rates or latency.
- This local installation does not distribute updates to offline machines or cloud execution environments.

## Files

- `gate.py`: prompt, tool, and Stop hooks; restore entry point.
- `gatelib.py`: policy, locked session state, checkpoints, restore, classification, verdict parsing, and diff checks.
- `review_worker.py`: classifier, review brief, subscription CLI invocation, and result publication.
- `reviewer-brief.md`: reviewer instructions and verdict format.
- `policy.json`: rules and thresholds.
- `gate/tests/`: deterministic regressions and the older live canary.
- `install/`: example hook entries and Codex review profile.

MIT license. The October 2026 repair followed independent Fable 5.1 and Opus 5.5 reviews, mock execution evidence, disposable Git tests, and limited live experiments. Full review and deployment receipts accompany the originating task.
