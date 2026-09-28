#!/usr/bin/env python3
"""Check what a Claude Code binary actually sends, against a local mock API.

"The bytes changed", "the size is the same", "--version still works" and "the
script's gate turned green" all pass on a module that runs from bytecode, where
the edit does nothing. The only evidence that a patch took effect is a
difference in the requests the CLI sends. This script records them.

Adapted from probe.py in the sister repository:
https://github.com/lllq-123/claude-code-ephemeral-reminders/blob/main/examples/probe.py

Everything stays on your machine and nothing is billed:

  * ANTHROPIC_BASE_URL points at an HTTP server on 127.0.0.1 started by this
    script; HTTP(S)_PROXY points at a dead loopback port, so nothing else can
    leave the box either.
  * HOME and CLAUDE_CONFIG_DIR are fresh directories inside the run folder.
    They contain no real credentials, so your real login is never read, used
    or refreshed, and your real session history is untouched.
  * The credential is a fake string. The mock never checks it.

What is different from probe.py, and why:

  * No --system-prompt override, so the default system prompt (including the
    pronoun section) is actually sent.
  * No --bare and no API key: the account email is only rendered for OAuth
    logins, so this uses a fake CLAUDE_CODE_OAUTH_TOKEN plus a fake
    oauthAccount (with an example.invalid address) in the isolated config.
  * A scripted tool plan: turn 1 = Read, Read, Write; then both files are
    modified externally; turn 2 = plain; turn 3 = Read. On 2.1.280 the
    changed-files collector skips files whose read state has an offset/limit,
    and Read records one -- so Write is the real positive control.

Usage -- run the original and the patched binary, then compare the totals:

    python3 mockprobe.py /path/to/original-claude original
    python3 mockprobe.py /path/to/patched-claude  patched

Each run writes request-N.json (the exact body sent), stdout/stderr of the
CLI and summary.json into a new temporary directory (or --out DIR), and
prints the summary. Delete the directory when you are done.
"""
from __future__ import annotations

import argparse, http.server, json, os, queue, re, subprocess, tempfile, threading, time, uuid
from pathlib import Path

MARKERS = {
    "userEmail": "The user's email address is",
    "pronoun": "When you use a pronoun",
    "edited_text_file": "changed on disk since you last read it",
    "date": "Today's date is",
}
TOOL_PLAN = {1: ["read", "read", "write"], 2: [], 3: ["read"]}
FAKE_EMAIL = "probe-user@example.invalid"
FAKE_TOKEN = "local-fixture-not-a-real-token"


def turn_state(body):
    """Return (turn number, tool_results since that turn's prompt)."""
    turn, since = 0, 0
    for m in body.get("messages", []):
        content = m.get("content")
        blocks = content if isinstance(content, list) else [{"type": "text", "text": content or ""}]
        for b in blocks:
            if b.get("type") == "text" and "Local transport test turn " in b.get("text", ""):
                t = b["text"].split("Local transport test turn ", 1)[1].split(":", 1)[0]
                if t.isdigit():
                    turn, since = int(t), 0
            elif b.get("type") == "tool_result":
                since += 1
    return turn, since


def build_mock(run, calls, other, t0):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            other.append(("GET", self.path, round(time.monotonic() - t0[0], 3)))
            self.send_response(404); self.send_header("Content-Type", "application/json"); self.end_headers()
            self.wfile.write(b'{"error":"not found"}')

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if self.path.startswith("/v1/messages/count_tokens"):
                self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
                self.wfile.write(b'{"input_tokens":100}'); return
            if not self.path.startswith("/v1/messages"):
                other.append(("POST", self.path, round(time.monotonic() - t0[0], 3)))
                self.send_error(404); return
            body = json.loads(raw)
            calls.append((round(time.monotonic() - t0[0], 3), body))
            (run / f"request-{len(calls)}.json").write_text(json.dumps(body, indent=2))
            turn, since = turn_state(body)
            plan = TOOL_PLAN.get(turn, [])
            use_tool = bool(body.get("tools")) and since < len(plan)
            kind = plan[since] if use_tool else None
            name, tool_input = (("Read", {"file_path": str(run / "fixture.txt")}) if kind == "read" else
                                ("Write", {"file_path": str(run / "written.txt"),
                                           "content": "Written by the Write tool.\nsecond line\n"}))
            block = ({"type": "tool_use", "id": f"toolu_fixture_{len(calls)}", "name": name, "input": {}}
                     if use_tool else {"type": "text", "text": ""})
            delta = ({"type": "input_json_delta", "partial_json": json.dumps(tool_input)}
                     if use_tool else {"type": "text_delta", "text": "Local fixture response."})
            message = {"id": f"msg_fixture_{len(calls)}", "type": "message", "role": "assistant", "content": [],
                       "model": body["model"], "stop_reason": None, "stop_sequence": None,
                       "usage": {"input_tokens": 100, "output_tokens": 1}}
            events = [
                ("message_start", {"type": "message_start", "message": message}),
                ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": block}),
                ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": delta}),
                ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                ("message_delta", {"type": "message_delta",
                                   "delta": {"stop_reason": "tool_use" if use_tool else "end_turn", "stop_sequence": None},
                                   "usage": {"output_tokens": 5}}),
                ("message_stop", {"type": "message_stop"}),
            ]
            payload = "".join(f"event: {n}\ndata: {json.dumps(d)}\n\n" for n, d in events).encode()
            self.send_response(200); self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(payload))); self.end_headers()
            self.wfile.write(payload)

    return http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)


def main():
    ap = argparse.ArgumentParser(description="Record what a Claude Code binary sends, against a loopback mock API.")
    ap.add_argument("binary", help="path to the Claude Code binary to test")
    ap.add_argument("label", help="short name for this run, e.g. original / patched")
    ap.add_argument("--out", type=Path, help="run directory (default: a new temporary directory)")
    args = ap.parse_args()
    run = args.out or Path(tempfile.mkdtemp(prefix=f"mockprobe-{args.label}-"))
    cfg, home = run / "config", run / "home"
    cfg.mkdir(parents=True, exist_ok=True); home.mkdir(parents=True, exist_ok=True)
    (cfg / ".claude.json").write_text(json.dumps({
        "hasCompletedOnboarding": True, "numStartups": 3,
        "oauthAccount": {"accountUuid": "00000000-0000-4000-8000-000000000001",
                         "emailAddress": FAKE_EMAIL,
                         "organizationUuid": "00000000-0000-4000-8000-000000000002",
                         "displayName": "Probe"}}))
    fixture = run / "fixture.txt"
    fixture.write_text("Local read fixture.\nline two\n")

    sid = str(uuid.uuid4())
    calls, other, t0 = [], [], [0.0]
    server = build_mock(run, calls, other, t0)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    env = {k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "LC_ALL", "TMPDIR")}
    env.update({
        "HOME": str(home),
        "CLAUDE_CONFIG_DIR": str(cfg),
        "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{server.server_port}",
        "CLAUDE_CODE_OAUTH_TOKEN": FAKE_TOKEN,
        "HTTPS_PROXY": "http://127.0.0.1:9", "HTTP_PROXY": "http://127.0.0.1:9",
        "https_proxy": "http://127.0.0.1:9", "http_proxy": "http://127.0.0.1:9",
        "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_AUTOUPDATER": "1", "DISABLE_TELEMETRY": "1", "DISABLE_ERROR_REPORTING": "1",
    })
    cmd = [str(Path(args.binary).resolve()), "-p",
           "--tools", "Read,Write", "--allowedTools", "Read,Write",
           "--model", "claude-sonnet-5", "--session-id", sid,
           "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
           "--setting-sources", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}']

    output: queue.Queue = queue.Queue()
    stderr = (run / "stderr.txt").open("w")
    t0[0] = time.monotonic()
    proc = subprocess.Popen(cmd, cwd=run, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=stderr, text=True, bufsize=1)

    def pump():
        with (run / "stdout.jsonl").open("w") as out:
            for line in proc.stdout:
                out.write(line); out.flush()
                try:
                    output.put(json.loads(line))
                except ValueError:
                    pass
        output.put({"type": "eof"})

    reader = threading.Thread(target=pump, daemon=True); reader.start()
    results, error = [], None
    try:
        for turn in (1, 2, 3):
            if turn == 2:
                time.sleep(1.1)
                fixture.write_text("Local read fixture.\nline two EDITED EXTERNALLY\nline three\n")
                (run / "written.txt").write_text("Written by the Write tool.\nsecond line EDITED EXTERNALLY\nthird\n")
            event = {"type": "user", "message": {"role": "user",
                     "content": f"Local transport test turn {turn}: reply briefly."}}
            proc.stdin.write(json.dumps(event) + "\n"); proc.stdin.flush()
            deadline = time.monotonic() + 60
            while True:
                ev = output.get(timeout=max(0.01, deadline - time.monotonic()))
                if ev.get("type") == "result":
                    results.append({k: ev.get(k) for k in ("subtype", "is_error", "num_turns")})
                    break
                if ev.get("type") == "eof":
                    raise RuntimeError("CLI exited before returning a result")
        proc.stdin.close(); proc.wait(timeout=15)
    except Exception as exc:
        error = str(exc) or type(exc).__name__
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill(); proc.wait()
        reader.join(timeout=2); server.shutdown(); server.server_close(); stderr.close()

    per_req = []
    for ts, body in calls:
        blob = json.dumps(body.get("system", "")) + json.dumps(body.get("messages", []))
        turn, since = turn_state(body)
        per_req.append({"t": ts, "turn": turn, "tool_results": since, "has_tools": bool(body.get("tools")),
                        **{k: blob.count(v) for k, v in MARKERS.items()},
                        "fake_email": blob.count(FAKE_EMAIL),
                        "edited_fixture": len(re.findall(r"fixture\.txt changed on disk", blob)),
                        "edited_written": len(re.findall(r"written\.txt changed on disk", blob))})
    totals = {k: sum(r[k] for r in per_req) for k in list(MARKERS) + ["fake_email", "edited_fixture", "edited_written"]}
    tool_errors = 0
    for line in (run / "stdout.jsonl").read_text().splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        for b in (ev.get("message") or {}).get("content") or []:
            if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("is_error"):
                tool_errors += 1
    summary = {"label": args.label, "run_dir": str(run), "exit": proc.returncode, "error": error,
               "requests": len(calls), "first_request_s": calls[0][0] if calls else None,
               "results": results, "tool_result_errors": tool_errors,
               "other_endpoints": other, "totals": totals, "per_request": per_req}
    (run / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=1))
    if error:
        print("--- stderr tail ---"); print((run / "stderr.txt").read_text()[-2000:])
    return 1 if error else 0


if __name__ == "__main__":
    raise SystemExit(main())
