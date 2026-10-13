#!/usr/bin/env python3
"""Pushback Gate shared library: policy, session state (locked, atomic), git checkpoints on a
dedicated ref, project detection, prompt rules, operation-aware tool classification, strict verdict
parsing and the mechanical diff check.

Python 3 standard library only. Nothing here calls a model; see review_worker.py.
"""
import fcntl
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
POLICY_PATH = os.path.join(HERE, "policy.json")
STATE_ROOT = os.environ.get("PUSHBACK_GATE_STATE_ROOT") or os.path.expanduser("~/.local/state/pushback_gate")
SESSIONS_DIR = os.path.join(STATE_ROOT, "sessions")
LOG_PATH = os.path.join(STATE_ROOT, "gate.log")
INTERNAL_ENV = "PUSHBACK_GATE_INTERNAL"
FAKE_VERDICT_ENV = "PUSHBACK_GATE_FAKE_VERDICT"   # tests: path to a canned reviewer answer
DISABLE_ENV = "PUSHBACK_GATE_DISABLED"
INTERNAL_SENTINEL = "PUSHBACK-GATE-INTERNAL-RUN"   # placed in every prompt the gate itself sends to a CLI
LOCK_TIMEOUT = 5.0


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_ts():
    return time.time()


def log(msg):
    try:
        os.makedirs(STATE_ROOT, exist_ok=True)
        with open(LOG_PATH, "a") as f:
            f.write("%s %s\n" % (now_iso(), msg))
    except Exception:
        pass


def load_policy():
    with open(POLICY_PATH) as f:
        return json.load(f)


def is_internal():
    return os.environ.get(INTERNAL_ENV) == "1" or os.environ.get(DISABLE_ENV) == "1"


class LockTimeout(Exception):
    pass


def _flock_timeout(fh, seconds):
    deadline = time.time() + seconds
    while True:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError:
            if time.time() > deadline:
                raise LockTimeout()
            time.sleep(0.05)


# ---------------------------------------------------------------- session state

class SessionState:
    """One JSON file per (client, session). Every transition runs inside `update(fn)`, which holds an
    exclusive lock (bounded wait), lets fn mutate the freshly read state, and renames atomically.
    fn receives the live state and returns whatever the caller needs; decisions are made inside the lock."""

    def __init__(self, client, session_id):
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id or "unknown")[:80]
        self.dir = os.path.join(SESSIONS_DIR, "%s-%s" % (client, safe))
        self.path = os.path.join(self.dir, "state.json")
        self.lock_path = os.path.join(self.dir, "state.lock")

    def _default(self):
        return {"schema": 2, "created": now_iso(), "generation": 0, "generations": {}}

    def read(self):
        try:
            with open(self.path) as f:
                return json.load(f)
        except Exception:
            return self._default()

    def update(self, fn):
        os.makedirs(self.dir, exist_ok=True)
        with open(self.lock_path, "a+") as lk:
            _flock_timeout(lk, LOCK_TIMEOUT)
            try:
                st = self.read()
                res = fn(st)
                st["updated"] = now_iso()
                tmp = self.path + ".tmp.%d" % os.getpid()
                with open(tmp, "w") as f:
                    json.dump(st, f, indent=1)
                os.replace(tmp, self.path)
                return res
            finally:
                fcntl.flock(lk, fcntl.LOCK_UN)

    def current(self, st=None):
        st = st or self.read()
        return st["generations"].get(str(st.get("generation", 0)))

    @staticmethod
    def gen(st, gen):
        st.setdefault("generations", {})
        return st["generations"].setdefault(str(gen), {})


def prune_old_sessions(days):
    """Remove the gate's own session state folders older than `days`.
    Uses the owner's trash-style delete tool when one is configured (policy "trash_tool", argv prefix),
    so the folders stay restorable; otherwise removes them directly (they hold only this gate's own records)."""
    try:
        cutoff = time.time() - days * 86400
        victims = [os.path.join(SESSIONS_DIR, n) for n in os.listdir(SESSIONS_DIR)
                   if os.path.isdir(os.path.join(SESSIONS_DIR, n)) and os.path.getmtime(os.path.join(SESSIONS_DIR, n)) < cutoff]
        if not victims:
            return
        trash = [os.path.expanduser(a) for a in (load_policy().get("trash_tool") or [])]
        import shutil
        if trash and (os.path.exists(trash[-1]) or shutil.which(trash[0])):
            subprocess.run(trash + ["--by", "pushback_gate", "--reason", "session state older than %d days" % days]
                           + victims[:20], capture_output=True, timeout=60)
        else:
            for v in victims[:20]:
                if os.path.realpath(v).startswith(os.path.realpath(SESSIONS_DIR) + os.sep):
                    shutil.rmtree(v, ignore_errors=True)
    except Exception as e:
        log("prune failed: %s" % e)


# ---------------------------------------------------------------- project detection

def git(args, cwd, timeout=15, env=None):
    try:
        r = subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", "git %s timed out after %ss" % (args[0], timeout)
    except Exception as e:
        return 1, "", str(e)


def git_root(cwd):
    rc, out, _ = git(["rev-parse", "--show-toplevel"], cwd, timeout=10)
    return out if rc == 0 and out else None


def is_cloud_synced(path, policy):
    p = os.path.realpath(path)
    return any(marker in p for marker in policy.get("cloud_sync_markers", []))


def looks_like_code_project(cwd, policy):
    """True if cwd is inside a git repo, or its top two levels hold source files or a manifest."""
    home = os.path.expanduser("~")
    if os.path.realpath(cwd) in (os.path.realpath(home), "/"):
        return False
    if git_root(cwd):
        return True
    exts = set(policy["code_extensions"])
    manifests = set(policy["manifest_files"])
    try:
        for root, dirs, files in os.walk(cwd):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("node_modules", "venv", ".venv")][:40]
            for f in files:
                if f in manifests or os.path.splitext(f)[1].lower() in exts:
                    return True
            if root.count(os.sep) - cwd.count(os.sep) >= 1:
                break
    except Exception:
        pass
    return False


# ---------------------------------------------------------------- git checkpoint

SECRET_IGNORE = """# Added by Pushback Gate on first checkpoint: files that usually hold secrets or are generated.
.env
.env.*
*.pem
*.key
*.p12
*.pfx
*_rsa
*_ed25519
keys/
secrets/
credentials*.json
node_modules/
venv/
.venv/
__pycache__/
dist/
build/
.DS_Store
"""


def _repo_lock_path(root):
    os.makedirs(os.path.join(STATE_ROOT, "repo-locks"), exist_ok=True)
    return os.path.join(STATE_ROOT, "repo-locks", hashlib.sha1(root.encode()).hexdigest()[:16] + ".lock")


def checkpoint(cwd, label, policy):
    """Snapshot the working tree (tracked, modified and untracked-but-not-ignored files) as a commit on a
    dedicated ref, without touching the user's index or branch. Returns (sha_or_None, ref_or_None, note).

    Restoring is one step: `git checkout <sha> -- .` (plus `git clean -fd` for files that did not exist).
    A folder without git gets `git init` unless it is cloud-synced (Drive mishandles .git)."""
    root = git_root(cwd)
    note = ""
    if not root:
        if is_cloud_synced(cwd, policy):
            return None, None, "no checkpoint: folder is cloud-synced and has no git repository"
        rc, _, err = git(["init", "-q"], cwd)
        if rc != 0:
            return None, None, "git init failed: %s" % err[:200]
        root = cwd
        note = "initialized git; "
        gi = os.path.join(root, ".gitignore")
        if not os.path.exists(gi):
            with open(gi, "w") as f:
                f.write(SECRET_IGNORE)
            note += "wrote .gitignore; "
    budget = float(policy.get("checkpoint_seconds", 20))
    t0 = time.time()
    try:
        with open(_repo_lock_path(root), "a+") as lk:
            _flock_timeout(lk, min(budget, 5))
            try:
                return _checkpoint_locked(root, label, note, budget - (time.time() - t0))
            finally:
                fcntl.flock(lk, fcntl.LOCK_UN)
    except LockTimeout:
        return None, None, note + "another session is checkpointing this repository"


def _checkpoint_locked(root, label, note, budget):
    env = dict(os.environ)
    tmp_index = os.path.join(root, ".git", "pushback-gate-index-%d" % os.getpid())
    env["GIT_INDEX_FILE"] = tmp_index
    try:
        rc, head, _ = git(["rev-parse", "--verify", "-q", "HEAD"], root)
        parent = head if rc == 0 and head else None
        if parent:
            rc, _, err = git(["read-tree", parent], root, timeout=10, env=env)
            if rc != 0:
                return None, None, note + "read-tree failed: %s" % err[:120]
        rc, _, err = git(["add", "-A", "--", "."], root, timeout=max(3, int(budget)), env=env)
        if rc != 0:
            # Drive folders and odd filenames can defeat add -A; fall back to tracked files only.
            rc2, _, err2 = git(["add", "-u", "--", "."], root, timeout=max(3, int(budget / 2)), env=env)
            if rc2 != 0:
                return None, None, note + "git add failed: %s" % (err or err2)[:160]
            note += "untracked files not covered (%s); " % err.splitlines()[0][:80] if err else "untracked files not covered; "
        rc, tree, err = git(["write-tree"], root, timeout=10, env=env)
        if rc != 0:
            return None, None, note + "write-tree failed: %s" % err[:120]
        args = ["-c", "user.name=Pushback Gate", "-c", "user.email=pushback-gate@local", "commit-tree", tree,
                "-m", "Checkpoint before %s (Pushback Gate)" % label]
        if parent:
            args += ["-p", parent]
        rc, sha, err = git(args, root, timeout=10)
        if rc != 0 or not sha:
            return None, None, note + "commit-tree failed: %s" % err[:120]
        ref = "refs/pushback-gate/%s" % re.sub(r"[^A-Za-z0-9_-]", "-", label)
        git(["update-ref", ref, sha], root, timeout=10)
        return sha, ref, note + "checkpoint %s on %s" % (sha[:10], ref)
    finally:
        try:
            os.unlink(tmp_index)
        except OSError:
            pass


# ---------------------------------------------------------------- prompt rules

def _any(patterns, text):
    for p in patterns:
        if re.search(p, text, re.I | re.S):
            return p
    return None


def _strip_quotes(text):
    return re.sub(r'"[^"\n]*"|\'[^\'\n]*\'|“[^”\n]*”', '""', text)


def is_machine_prompt(prompt, policy):
    """System notifications, task notifications, app-generated suggestions and the gate's own briefs."""
    return INTERNAL_SENTINEL in prompt or bool(_any(policy["machine_prompt_patterns"], prompt[:400]))


def detect_mode(prompt, policy):
    """skip | wait | fast. Quoted phrases do not count; a negated phrase ("do not skip the review") is not a skip."""
    text = _strip_quotes(prompt)
    if _any(policy["mode_wait_patterns"], text):
        return "wait"
    for p in policy["mode_skip_patterns"]:
        for m in re.finditer(p, text, re.I):
            before = text[max(0, m.start() - 40):m.start()].lower()
            if re.search(r"\b(don'?t|do not|never|not|without)\b[^.]*$", before):
                continue
            return "skip"
    return "fast"


def classify_rules(prompt, policy):
    """Returns ('SKIP'|'CONSULT'|'AMBIGUOUS', reason). Questions and quoted text never consult."""
    text = prompt.strip()
    if len(text) < 12:
        return "SKIP", "very short prompt"
    hit = _any(policy["skip_patterns"], text)
    if hit:
        return "SKIP", "rule: %s" % hit[:60]
    stripped = _strip_quotes(text)
    first = stripped.split("\n", 1)[0].strip()
    if first.endswith("?") and len(first.split()) <= 40 and not _any(policy["imperative_patterns"], first):
        return "SKIP", "question"
    hit = _any(policy["consult_patterns"], stripped)
    if hit:
        return "CONSULT", "rule: %s" % hit[:60]
    if len(stripped.split()) <= 8 and "?" in stripped:
        return "SKIP", "short question"
    return "AMBIGUOUS", "no rule matched"


def prompt_fingerprint(prompt):
    return hashlib.sha256(prompt.strip().encode("utf-8", "replace")).hexdigest()[:16]


# ---------------------------------------------------------------- tool classification

def bash_command(tool_input):
    if not isinstance(tool_input, dict):
        return ""
    for k in ("command", "cmd"):
        v = tool_input.get(k)
        if isinstance(v, str):
            return v
        if isinstance(v, list):
            return " ".join(str(x) for x in v)
    return ""


def _statements(cmd):
    """Split a shell command line into statements on ; && || | and newlines, dropping comments."""
    out = []
    for raw in re.split(r"\n|;|&&|\|\||\|", cmd):
        s = raw.strip()
        if not s:
            continue
        s = re.sub(r"(^|\s)#.*$", "", s).strip()
        if s:
            out.append(s)
    return out


def _tokens(statement):
    try:
        toks = shlex.split(statement, posix=True)
    except ValueError:
        toks = statement.split()
    # drop leading env assignments, sudo, time, nohup, env
    while toks and (re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", toks[0]) or toks[0] in ("sudo", "time", "nohup", "env", "exec", "command")):
        toks = toks[1:]
    return toks


def _statement_kind(statement, policy):
    toks = _tokens(statement)
    if not toks:
        return "other"
    exe = os.path.basename(toks[0])
    if exe in ("xargs", "parallel") and len(toks) > 1:
        rest = [t for t in toks[1:] if not t.startswith("-")]
        return _statement_kind(" ".join(shlex.quote(t) for t in rest), policy) if rest else "other"
    if exe == "find" and ("-delete" in toks or any(t in ("-exec", "-execdir") and i + 1 < len(toks) and os.path.basename(toks[i + 1]) in ("rm", "rmdir", "unlink") for i, t in enumerate(toks))):
        return "irreversible"
    sub = toks[1] if len(toks) > 1 else ""
    sub2 = toks[2] if len(toks) > 2 else ""
    joined = " ".join(toks)
    # explicit allowances (per statement)
    reversible = set(policy.get("reversible_delete_tools", []))
    if exe in reversible or (exe in ("python3", "python") and len(toks) > 1 and os.path.basename(toks[1]) in reversible):
        return "other"
    rules = policy["irreversible_commands"]
    if exe in rules.get("always", []):
        # rm into the system temp folder is the script's own cleanup
        if exe in ("rm", "rmdir") and all(re.match(r"^(/private)?/tmp/|^/var/folders/|^\$TMPDIR", t) for t in toks[1:] if not t.startswith("-")) and any(not t.startswith("-") for t in toks[1:]):
            return "other"
        return "irreversible"
    subs = rules.get("subcommands", {}).get(exe)
    if subs:
        if sub in subs or ("%s %s" % (sub, sub2)) in subs:
            return "irreversible"
    if exe == "git":
        if sub in rules.get("git_subcommands", []):
            return "irreversible"
        if sub in ("checkout", "restore") and ("--" in toks or sub == "restore" and "--source" in joined):
            return "irreversible"
        if sub in ("commit", "add", "stash", "tag", "merge", "cherry-pick", "revert", "switch"):
            return "edit"
        return "read"
    if exe in rules.get("sql_clients", []) and re.search(r"\b(DROP\s+(TABLE|DATABASE)|TRUNCATE\s+TABLE|DELETE\s+FROM)\b", statement, re.I):
        return "irreversible"  # for a database client the SQL legitimately sits inside quotes
    if re.search(r"(^|\s)(>|>>)\s*\S", statement) or exe in ("tee", "mv", "cp", "mkdir", "touch", "patch", "ln", "chmod", "chown"):
        return "edit"
    if exe == "sed" and "-i" in toks:
        return "edit"
    for pre in policy["readonly_executables"]:
        if exe == pre:
            return "read"
    return "other"


def bash_kind(cmd, policy):
    """Worst kind across the statements of a command line: irreversible > edit > other > read."""
    order = {"read": 0, "other": 1, "edit": 2, "irreversible": 3}
    worst = "read"
    for st in _statements(cmd):
        k = _statement_kind(st, policy)
        if order[k] > order[worst]:
            worst = k
    return worst


def tool_kind(tool_name, tool_input, policy):
    """'read' | 'edit' | 'irreversible' | 'other'. Unknown tools are 'other' (never silently read)."""
    name = tool_name or ""
    if name in policy["edit_tools"]:
        return "edit"
    if name in policy["read_tools"]:
        return "read"
    cmd = bash_command(tool_input)
    if name in policy["shell_tools"] or (cmd and not name):
        return bash_kind(cmd, policy) if cmd else "other"
    if name.startswith("mcp__"):
        low = name.lower()
        if any(s in low for s in ("send", "post", "publish", "delete", "trash", "forward", "reply", "create_file", "update", "write", "purchase", "set_")):
            return "irreversible"
        if any(s in low for s in ("read", "get", "list", "search", "status", "query", "find", "fetch", "show")):
            return "read"
    return "other"


# ---------------------------------------------------------------- verdict parsing

VERDICT_RE = re.compile(r"^\s*VERDICT\s*:\s*(GO-WITH-NOTES|GO|STOP)\s*$", re.I | re.M)
SECTION_RE = re.compile(r"^(REASON|GOAL|NOTES|SHAPE)\s*:\s*", re.I | re.M)


def parse_verdict(text):
    """Strict: the VERDICT line must be exactly one token. Anything else is UNPARSED (advisory only)."""
    out = {"verdict": None, "reason": "", "goal": "", "notes": "", "shape": "", "raw": text or ""}
    if not text:
        return out
    m = VERDICT_RE.search(text)
    if m:
        out["verdict"] = m.group(1).upper().replace("GO-WITH-NOTES", "GO-WITH-NOTES")
    positions = [(mm.start(), mm.end(), mm.group(1).lower()) for mm in SECTION_RE.finditer(text)]
    for i, (start, end, name) in enumerate(positions):
        stop = positions[i + 1][0] if i + 1 < len(positions) else len(text)
        out[name] = text[end:stop].strip()
    return out


def shape_files(shape_text):
    files = set()
    for m in re.finditer(r"`([^`\n]+)`|(\b[\w./-]+\.(?:py|js|ts|tsx|jsx|swift|go|rs|rb|java|kt|c|cc|cpp|h|m|mm|cs|php|sh|sql|html|css|scss|vue|svelte|json|toml|yaml|yml|md))\b", shape_text or ""):
        p = (m.group(1) or m.group(2) or "").strip()
        if p and re.search(r"\.\w{1,5}$", p) and " " not in p:
            files.add(os.path.normpath(p).lstrip("./"))
    return sorted(files)


def shape_size(shape_text):
    t = (shape_text or "").lower()
    m = re.search(r"expected size[^a-z]*(small|medium|large)", t)
    return m.group(1) if m else None


# ---------------------------------------------------------------- mechanical diff check

def _path_matches(actual, expected_set):
    a = os.path.normpath(actual)
    for e in expected_set:
        if a == e:
            return True
        if "/" in e and a.endswith("/" + e):
            return True
        if "/" not in e and os.path.basename(a) == e and a.count("/") == 0:
            return True
    return False


def diff_check(root, checkpoint_sha, shape_text, policy):
    """Compare the actual working tree since the checkpoint against the reviewer's SHAPE.
    Limitations (documented): public-interface changes are not detected; version bumps in manifests can look like
    new dependencies; changes made by another session in the same worktree are attributed to this consult."""
    res = {"files": [], "lines": 0, "band": None, "expected_band": shape_size(shape_text),
           "unexpected_files": [], "new_deps": [], "departures": []}
    if not root or not checkpoint_sha:
        return res
    rc, out, _ = git(["diff", "--numstat", checkpoint_sha], root)
    rc2, untracked, _ = git(["ls-files", "--others", "--exclude-standard"], root)
    files, lines = [], 0
    if rc == 0:
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) == 3:
                a, d, f = parts
                files.append(f)
                lines += (int(a) if a.isdigit() else 0) + (int(d) if d.isdigit() else 0)
    if rc2 == 0:
        for f in untracked.splitlines():
            if f and f not in files:
                files.append(f)
                try:
                    with open(os.path.join(root, f), errors="replace") as fh:
                        lines += sum(1 for _ in fh)
                except Exception:
                    pass
    res["files"], res["lines"] = files, lines
    bands = policy["size_bands_lines"]
    res["band"] = "small" if lines <= bands["small"] else "medium" if lines <= bands["medium"] else "large"
    expected = set(shape_files(shape_text))
    if expected:
        res["unexpected_files"] = [f for f in files if not _path_matches(f, expected)]
    manifests = set(policy["manifest_files"])
    for f in files:
        if os.path.basename(f) in manifests:
            rc, d, _ = git(["diff", checkpoint_sha, "--", f], root)
            if rc != 0 or not d:
                try:
                    with open(os.path.join(root, f), errors="replace") as fh:
                        d = "".join("+" + l for l in fh)
                except Exception:
                    d = ""
            added = [l[1:].strip() for l in d.splitlines() if l.startswith("+") and not l.startswith("+++")]
            deps = [a for a in added if re.search(r'^"[^"]+"\s*:\s*"[\^~>=<\d*]|^[\w.\[\]-]+\s*(==|>=|~=|\^|@)|^\s*[\w-]+\s*=\s*"[\^~>=<\d]', a)]
            if deps:
                res["new_deps"].append((f, deps[:8]))
    order = {"small": 0, "medium": 1, "large": 2}
    if res["expected_band"] and order.get(res["band"], 0) - order.get(res["expected_band"], 0) >= 1 and lines > bands["small"]:
        res["departures"].append("size: reviewer expected %s, actual %s (%d changed lines)" % (res["expected_band"], res["band"], lines))
    if res["unexpected_files"] and expected:
        res["departures"].append("files outside the reviewer's expected set: %s" % ", ".join(res["unexpected_files"][:12]))
    if res["new_deps"]:
        res["departures"].append("possible new dependencies (version bumps can look the same): " +
                                 "; ".join("%s: %s" % (f, ", ".join(d)) for f, d in res["new_deps"]))
    return res


# ---------------------------------------------------------------- misc

def read_json_stdin():
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except Exception:
        return {}


def emit(obj):
    sys.stdout.write(json.dumps(obj))
    sys.stdout.flush()
