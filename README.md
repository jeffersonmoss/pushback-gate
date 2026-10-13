# Pushback Gate

A partial answer to "my AI never pushes back on my bad ideas."

When you tell a coding agent *how* to change a codebase ("add a second database", "rewrite this in a
new framework", "pull in library X", "make it a microservice"), the agent usually just does it. It is
trained to be helpful, it is working inside your framing, and its own second-guessing is worth very
little because it is the same model grading its own homework.

Pushback Gate is a set of hooks for **Claude Code** and **Codex CLI** that gets a *different vendor's
model* to review your request in parallel with the build, with read-only access to the repository,
and lets that reviewer stop the build before anything hard to undo happens. You never have to remember
to ask for it. It runs on every prompt in every code folder and decides for itself whether a prompt
embodies a design decision.

Built 12 October 2026 by Jefferson Moss with Claude (Fable 5.1), reviewed by GPT (Sol) before and after
the build, and in daily use on one machine since that night. It is early and it is partial. Read
"What it does not do" before trusting it.

## The idea in one paragraph

Asking the builder to "be critical" is nothing: it is one model grading itself, inside your framing.
What works better is a second model from a different vendor, who did not hear the conversation that
led to your request, reading the request as if a junior developer had proposed it, with the actual
repository in front of it. It must restate the **goal** separately from the **means** you proposed,
cite files or concrete consequences for every objection, and propose the shape it would build instead.
It runs *in parallel* with the build so the fast path stays fast, and the build is only held at the
points where undoing would be expensive. If the reviewer says STOP, the builder presents both sides and
you decide. Your explicit choice always outranks the reviewer.

## How it is shaped

Three hooks, one detached worker, one policy file. Everything is Python 3 standard library, no
dependencies.

1. **UserPromptSubmit** (`gate.py <client> UserPromptSubmit`). Classifies the prompt in under a
   millisecond with rules from `policy.json`: SKIP (questions, investigation, typos, config, tests,
   acknowledgements, machine-generated prompts), CONSULT (rewrite, refactor, new dependency, schema,
   service, layer, module, abstraction, install, "replace X with Y"), or AMBIGUOUS, which goes to a cheap
   model (Haiku) in the background. Bias is toward consulting: a needless consult costs seconds of
   subscription usage, a missed one costs a wrong thousand-line diff. On CONSULT it snapshots the
   working tree, launches the review worker, and injects a short note telling the builder what the gate
   will do.
2. **PreToolUse** (`gate.py <client> PreToolUse`). While a consult is pending (first 3 minutes), denies
   the irreversible: `git push`, raw `rm`, package installs, migrations, deploys, outward sends. Shell
   commands are classified per statement by executable and subcommand, so `git commit -m "DROP TABLE"`
   passes and `ls && rm -rf dist` does not. Ordinary edits, reads, local commits and trash-style deletes
   pass. When the verdict lands it is handed to the builder on its next tool call: GO-WITH-NOTES as
   context (the builder must say per note whether it adopted it), STOP as a denial of everything except
   reads, with the full verdict.
3. **Stop** (`gate.py <client> Stop`). At turn end, hands over a verdict that arrived after the last tool
   call, and runs a mechanical diff check against the reviewer's proposed SHAPE: files outside the
   expected set, size band exceeded, possible new dependencies in manifests. Departures get one block
   asking for a one-line disposition each. At most two blocks per prompt, then the turn ends regardless.
4. **review_worker.py** (detached). Builds a brief: the request framed as a junior developer's proposal,
   the project's instruction files (CLAUDE.md, AGENTS.md, ARCHITECTURE.md, README), recent commits, a file
   tree, any files the prompt names. Calls the other vendor's CLI with read-only access to the repository.
   Parses the verdict strictly (`VERDICT: GO | GO-WITH-NOTES | STOP`) and publishes it into the session
   state.
5. **Modes**, detected from the prompt. Fast (default): the build starts immediately. "Wait for review":
   edits are held until the verdict, capped at 4 minutes. "Just do it": no consult.
6. **Checkpoint.** Before a consult, the working tree is snapshotted as a commit on
   `refs/pushback-gate/consult-N` through a temporary index, so your branch, your staging area and your
   `git log` are untouched. Restore after a STOP with `git checkout <sha> -- .`. A folder without git gets
   `git init` and an ignore file for secret and generated files. Cloud-synced folders (Google Drive,
   iCloud, Dropbox, OneDrive) get no repository and no checkpoint, because a `.git` folder inside a
   sync client is a bad time.
7. **Deadline.** The gate is a 3-minute window, absolute from the moment the prompt arrived. After it,
   the gate releases permanently for that prompt and a late verdict is shown as advisory, never enforced.
   Nothing ever hangs: any hook error, worker crash or timeout releases the gate and says so.
8. **Steering.** Any genuine new prompt from you supersedes a pending consult. Machine prompts (task
   notifications, app-generated suggestions, the gate's own briefs) are ignored entirely.
9. **Recursion guard.** The reviewer runs with `PUSHBACK_GATE_INTERNAL=1` and every prompt the gate sends
   starts with a sentinel line, so the reviewer's own session does not get gated and start another
   reviewer.

## The reviewer's instructions

The full brief is in `reviewer-brief.md`. The parts that matter:

- The request is presented as coming from a junior developer, technically minded but not trained. This
  removes the deference the reviewer would show to "the user".
- It must restate the GOAL apart from the proposed means. "Make search faster" is a goal; "add a second
  database" is a means.
- Every objection must cite a file, a function, or a concrete maintenance consequence. "Seems
  over-engineered" is not evidence. Insufficient context is not an architectural finding.
- It must propose the SHAPE it would build: files, key decisions, things to avoid, expected size. The
  Stop hook checks the real diff against this mechanically, so the builder never grades itself.
- STOP only with cited evidence; the owner decides, and the owner's choice outranks the reviewer.

## What it does not do (honest table)

| Situation | Claude Code 2.1.289 | Codex CLI 0.160 |
|---|---|---|
| Deny push / rm / install / migrate / deploy / send while pending | yes, per shell statement | yes, for tool calls that pass through hooks |
| Hold edits in wait mode; deny all but reads after STOP | yes | yes for apply_patch and shell; hosted tools bypass hooks |
| Scripts whose effect the gate cannot see (`python3 x.py`, unknown MCP tools) | allowed while pending, denied after STOP | same |
| Public-interface changes in the diff check | not detected | not detected |
| Another session editing the same worktree during a consult | attributed to this consult | same |
| A command already running when the verdict lands | cannot be retracted | cannot be retracted |
| Input typed into an existing shell (`write_stdin`) | n/a | not re-checked by hooks |
| Worker killed with SIGKILL mid-consult | consult looks alive until the deadline | same |

It also costs something: every consult is a model call with the repository context, roughly 20 to 40
seconds and a few thousand tokens on the reviewing vendor's subscription. The classifier call for
ambiguous prompts is another small one. If you are on a small plan, set `effort` to `low` in
`policy.json`.

## Requirements

- macOS or Linux, Python 3.9+, git.
- Claude Code (tested on 2.1.289) and Codex CLI (tested on 0.160.0), both signed in. As built, Claude
  Code builds get reviewed by GPT through Codex, and Codex builds get reviewed by Claude Opus through
  Claude Code. **If you only have one vendor**, the reviewer call is one function
  (`call_reviewer` in `review_worker.py`, about 40 lines): point it at any model API you have a key
  for (OpenRouter, Gemini, a local model). Your coding agent can make that change for you in a few
  minutes; tell it which API and hand it this README.

## Install

1. Copy the code:
   ```bash
   git clone https://github.com/jeffersonmoss/pushback-gate.git
   mkdir -p ~/.claude/scripts
   cp -R pushback-gate/gate ~/.claude/scripts/pushback_gate
   ```
   The hook commands assume `~/.claude/scripts/pushback_gate/gate.py`. Any path works; change the
   commands in the install files to match.
2. Register the hooks. For Claude Code, merge `install/claude-settings-hooks.json` into
   `~/.claude/settings.json` (the `hooks` key). For Codex, merge `install/codex-hooks.json` into
   `~/.codex/hooks.json` (create it if missing). If you already have hooks for these events, add the
   gate entries alongside them; order does not matter.
3. Add the Codex review profile: copy `install/codex-review.config.toml` to
   `~/.codex/review.config.toml`. See the next section for why.
4. Edit `policy.json` if needed: `reviewer_by_client` holds the model ids tried in order for each
   direction. Defaults are `gpt-6.1-sol` then `gpt-6-sol` for the Codex side and `opus` for the Claude
   side. `trash_tool` and `reversible_delete_tools` are optional and refer to a trash-style delete
   command if you use one; delete those keys if you do not.
5. Run the tests: `python3 ~/.claude/scripts/pushback_gate/tests/test_gate.py`. They use canned verdicts
   and make no model calls. 28 tests, about a minute.
6. Open a new session in a code folder and type something like "rewrite the config loader as a plugin
   system". You should see the gate's note under your prompt, and within about 30 seconds a verdict on
   your next tool call. The log is `~/.local/state/pushback_gate/gate.log`.

Switch it off for one shell with `PUSHBACK_GATE_DISABLED=1`, or everywhere with `"enabled": false` in
`policy.json`.

## Calling one CLI from the other (the part that took a while to get right)

Both CLIs can run headless and be called from a script, but each has quirks that make the first few
attempts fail silently or hang. What works, as of the versions above:

**Codex, called from a script (the reviewer when Claude Code builds):**

```
codex exec -p review -m gpt-6.1-sol --skip-git-repo-check -C <repo> -o <outfile> -c model_reasoning_effort="medium" - < brief.md
```

- `-p review` loads `~/.codex/review.config.toml` on top of your base config. Codex profiles are
  **separate files named `<name>.config.toml`** in `~/.codex`, not sections of `config.toml`. The
  profile sets `sandbox_mode = "read-only"` (the reviewer can read the repository but not write or reach
  the network) and `approval_policy = "never"` (no human in the loop, so it cannot stall waiting for a
  click).
- `--skip-git-repo-check` lets it run in folders that are not git repositories.
- `-` as the last argument reads the prompt from stdin. `-o <outfile>` writes the final answer to a file;
  use an **absolute** path, because Codex resolves relative paths against the `-C` directory, which is
  how we lost a few answers.
- A rejected model id (plan does not include it, or the id was renamed) comes back as a "not supported"
  or "unknown model" error; the gate falls through to the next id in the policy list.

**Claude Code, called from a script (the reviewer when Codex builds):**

```
claude -p --model opus --tools "Read,Grep,Glob" --effort medium --no-session-persistence --output-format text --setting-sources "" --strict-mcp-config --mcp-config '{"mcpServers":{}}' < brief.md
```

- `-p` is headless (print) mode, prompt on stdin.
- `--tools "Read,Grep,Glob"` is the read-only tool set. Without it the reviewer could edit.
- `--setting-sources ""` ignores your user and project settings, so **your own hooks (including this
  gate) do not run inside the reviewer**. `--strict-mcp-config --mcp-config '{"mcpServers":{}}'`
  disables every MCP server for the same reason. Without these two, the reviewer inherits your whole
  environment and can act through it.
- `--no-session-persistence` keeps the reviewer's run out of your session list.
- **Keychain gotcha (macOS):** headless `claude -p` over plain `ssh` fails with "Not logged in", because
  the credentials live in the keychain of the GUI login session. Run it inside that session (a
  `launchctl bootstrap gui/$(id -u) <plist>` launcher works) or run it locally.
- Claude's Stop hook accepts only `{"decision":"block","reason":"..."}`. Returning `additionalContext`
  from a Stop hook continues the conversation, which is almost never what you want.

**Both directions:**

- Set `PUSHBACK_GATE_INTERNAL=1` in the environment *and* put the sentinel line
  `PUSHBACK-GATE-INTERNAL-RUN` at the top of the prompt. Codex hooks were observed not inheriting the
  environment, so the sentinel is what actually stops the reviewer's session from being gated.
- Give both calls a long timeout (the gate uses 600 seconds) and a per-call output file. Reviews that
  take 20 to 40 seconds are normal; 2 minutes happens on big repositories.
- Hook JSON contracts differ by client and version. Claude's PreToolUse uses
  `hookSpecificOutput.permissionDecision: "deny"` plus `permissionDecisionReason`; Codex 0.160's Stop
  schema rejects `hookSpecificOutput` entirely. `gate.py` has one emitter per client; if a new version
  changes a schema, that is the place to look.

If you need more than this, hand this section to your coding agent and ask it to verify each flag
against the installed versions. That is how it got written.

## Files

```
gate/
  gate.py              hook entry: gate.py <claude|codex> <UserPromptSubmit|PreToolUse|Stop>
  gatelib.py           policy, locked atomic session state, git checkpoint, rules, verdict parsing, diff check
  review_worker.py     detached worker: classification, brief, reviewer call, publish
  policy.json          every rule, pattern and threshold
  reviewer-brief.md    the reviewer's instructions and output format
  tests/test_gate.py   28 tests with canned verdicts, no model calls
install/
  claude-settings-hooks.json   hook entries for ~/.claude/settings.json
  codex-hooks.json             hook entries for ~/.codex/hooks.json
  codex-review.config.toml     the read-only Codex profile
```

Session state lives in `~/.local/state/pushback_gate/sessions/<client>-<session>/` (state, prompt,
brief, verdict, worker log) and is pruned after 14 days.

## Design history

The design was reviewed before the build by GPT Sol (twice, both STOP, on separate grounds), GLM 5.3
Flash and MiMo (both GO-WITH-NOTES). The first build was reviewed by Sol (STOP, 13 findings, ten fixed
outright and three documented as partial in the table above). The gate reviewed its own README edits
while this file was being written. The method is the point: the thing that catches bad design decisions
was itself built under the thing that catches bad design decisions.

## Licence

MIT. Do what you like with it. If you make it work with a different reviewer, a pull request with the
`call_reviewer` variant would help the next person.
