# claude-code-strip-injections

Disable a few of the things Claude Code pushes into your context — and, more importantly: **how to find and disable a new injection yourself when the next version adds one.**

[中文](README.md)

The examples were first taken from Claude Code `2.1.175`, and the script has been verified on `2.1.280`. By the time you read it the version has probably changed again, so the point is not to copy the offsets, it is to copy the method.

> **If you have been using the old script on 2.1.258 or later, note this**: from 258 on, the old version errors out as soon as you run it with `currentDate gate is ambiguous (0, 0)`, and even when it does run, on 280 **the patch does not take effect** — see [Patched does not mean effective: bytecode](#patched-does-not-mean-effective-bytecode) for why. This version fixes both.

---

## Contents

- [What is being disabled](#what-is-being-disabled)
- [Read this first: no decompilation involved](#read-this-first-no-decompilation-involved)
- [Patched does not mean effective: bytecode](#patched-does-not-mean-effective-bytecode)
- [Risks and boundaries](#risks-and-boundaries)
- [How to search this file safely](#how-to-search-this-file-safely)
- [Six examples](#six-examples)
- [The general method: what to do when a new version adds a new injection](#the-general-method-what-to-do-when-a-new-version-adds-a-new-injection)
- [How to verify it actually took effect](#how-to-verify-it-actually-took-effect)
- [Script usage](#script-usage)
- [Limits and known issues](#limits-and-known-issues)

---

## What is being disabled

Claude Code inserts content you never asked for into the conversation and sends it to the model, mostly wrapped in `<system-reminder>`. Five kinds:

**1. Task reminders**

Go a certain number of turns without using the TodoWrite / Task tools and it injects a paragraph reminding you to set up a task list. The three pieces of text are 384, 128 and 250 bytes.

**2. File change notifications (`changed_files`)**

After a file you have read is modified externally, it automatically re-reads the file, computes a diff, and splices a snapshot of the file into context. One large file can produce a few thousand tokens.

**3. Current date (`currentDate`)**

Generates `Today's date is X.` and splices it, together with `claudeMd` and `userEmail`, into the **system-reminder of the first user message** — the very front of the whole prompt. It is computed once per process, at startup.

The cost of the third one is not on the same scale as the first two, but you only hit it under a specific architecture:

If your setup **starts a new process every turn** (the scripted `claude -p --resume` style), the first turn after midnight recomputes that front block with the new date, and **the entire prompt cache is invalidated from the start**. We measured this once: `read=0`, 215,407 tokens rewritten in full, once a day.

Anyone who just keeps a terminal open and chats will not hit this, because the process stays alive. Claude Code's own day-rollover fallback appends a `The date has changed...` line at the **end**, which leaves the prefix untouched — it is the restart-every-turn style of running it that bypasses that fallback.

**If you do not start a new process every turn, you can leave this one alone.** Note that `--only` is a whitelist, not an exclude switch — to skip this one, list everything else you want instead, e.g. `--only task-text,task-logic,changed-files,user-email`.

⚠️ **From 2.1.269 on, the date also has a separate `date` attachment of its own** (`Today's date is …`), sent out as an entry by itself. This patch only handles the field described above and cannot reach that one — measured on 2.1.280: with this patch applied, and with `--source-fallback` making the module it lives in run from source, the date is still in every request, just as in the original. **On newer versions, this one on its own can no longer turn the date off**; we keep it for older versions and for teaching (see [Example 4](#example-4-delete-an-object-property-22-bytes-changed)).

**4. Account email (`userEmail`)**

The context block in the first user message carries the line `The user's email address is …`. According to the source, it is only spliced in when you log in with a claude.ai account (OAuth); with an API key or `--bare` it is not there to begin with.

**5. The pronouns section of the system prompt**

The system prompt has a section that teaches the model how to use pronouns (`When you use a pronoun for someone …`). It is part of the system prompt itself, not a reminder. **This one is not disabled by default**; to disable it you have to name `pronouns` with `--only` — see [Risks and boundaries](#risks-and-boundaries) for why.

---

## Read this first: no decompilation involved

Claude Code is a **Bun-bundled ELF**, not a Node SEA. The test is to search for marker strings: `/$bunfs/`, `process.versions.bun` and `bun-build` all hit, while `NODE_SEA_BLOB` / `NODE_SEA_FUSE` are all zero.

That means the **JavaScript source is embedded in the executable as plaintext**. Identifier names are minified down to two or three letters (`BJ`, `_W`, `Qv`), but the structure is intact and the string literals sit there verbatim. You can search for something like this directly:

```
case"task_reminder":{if(!BJ())return[];
```

So every operation described here is: **search for a string in plaintext, and replace some of its bytes with the same number of different bytes.** No decompilation, no disassembly, because none is needed.

---

## Patched does not mean effective: bytecode

The previous section says the source is plaintext — that is true, but it is only half the story.

Bun lets a module be stored in the executable **as both source and precompiled bytecode**. When bytecode is present, the loader runs the bytecode, and the source is only kept around for things like error stack traces. **What you edited is a copy that never runs**: the bytes changed, the size did not, `--version` runs, the script's two-state check goes green — every one of these holds, and the behavior has not changed at all.

This is not a rare case. In 2.1.280, of the 2196 modules that carry source, 1973 also carry bytecode. **On 280, every code-changing patch in this repository (Examples 2–6) lands in the same bytecode-carrying module.** We measured it: with all of these patches applied and nothing else done, the email, the pronouns section and the file change notification appear in the requests the CLI sends exactly as many times as with the original (see [How to verify it actually took effect](#how-to-verify-it-actually-took-effect)).

**How to tell**: look at whether the bytecode length in this module's record in Bun's module directory table is 0. Do not go by whether there is a `// @bun @bytecode` marker near the source, or how far away it is — that is the module header, and the distance tells you nothing. The table's structure, what its fields mean, and a read-only parser are covered in full in the sister repository [claude-code-ephemeral-reminders](https://github.com/lllq-123/claude-code-ephemeral-reminders/blob/main/README.en.md#buns-module-graph-why-the-source-edit-does-nothing), so we will not repeat them here.

**What the script does now**:

- After each hit it marks which module the hit is in and whether that module runs from bytecode, e.g. `@ module #333 (bytecode)`
- When a selected patch lands in a bytecode module, it says outright that the change will not take effect
- The optional flag `--source-fallback` (**off by default**): zeroes 16 bytes in that module's directory record (the offsets and lengths of the bytecode and module_info). With no bytecode to find, the loader falls back to running the source from the same record — that is, the copy that has already been patched. Not a single byte of the bytecode itself is touched, and the file size does not change

`--source-fallback` is a switch for the **whole module**, so it only acts when it can say exactly "what will start taking effect next", and refuses otherwise:

- The selected code-changing patches must all be in the same module
- If this module has a patch that was **applied before but not selected this time**, it refuses — falling back would make that earlier change, which never took effect, suddenly take effect
- If the module has no bytecode but those 16 bytes are not 0, it refuses — it cannot read that

For the cost, and what it does not cover, see [Limits and known issues](#limits-and-known-issues).

---

## Risks and boundaries

Section 3, "Use of our Services", of Anthropic's [Consumer Terms](https://www.anthropic.com/legal/consumer-terms) lists a string of prohibited actions; the lead-in sentence is:

> "You may not access or use, or help another person to access or use, our Services in the following ways:"

One of them:

> "To decompile, reverse engineer, disassemble, or otherwise reduce our Services to human-readable form, except when these restrictions are prohibited by applicable law."

**Our reading** is that this targets *reducing* the Services to human-readable form, and this binary's JavaScript is already plaintext — there is no "reducing" involved.

**But that is our reading, not legal advice.** Terms change; what is quoted above is the August 2026 version. The link is right there — go read the original yourself and judge for yourself.

The technical boundaries of these changes:

- **Signature verification is not touched**, nor is any authentication logic
- **Network requests are not touched.** Of what gets sent to the server, only things spliced in locally are removed; nothing is added or rewritten
- What's changed is mainly **what the local process puts into context** (Example 3 has one exception, see below)
- The model itself is not changed, and no tools are removed

### The six examples are not all the same kind of change

(The example numbers mentioned here are explained in the [Six examples](#six-examples) section below.)

- **Example 1** is a pure text replacement: a block of prompt text becomes the same number of spaces, with no code logic touched.
- **Examples 2, 3, 4 and 5** change the code itself — a conditional, a function call, an object property, a value expression. They are still equal-length byte replacements, but they change the program's execution path, which is one level deeper than blanking out a piece of text.
- **Example 6** removes **a section of the system prompt**. It is not a reminder; it is behavioral guidance written for the model. That goes one level deeper again than removing a reminder, so the script does not apply it by default — you have to name it with `--only pronouns`.
- **`--source-fallback`** changes not JS code but **metadata the loader reads** (the module directory table) — what it changes is not what the program does, but how the program gets loaded. That is a separate tier, so it is also off by default.

If you only want the most conservative tier, use `--only task-text`.

Example 3 also has a side effect you should know about: it simultaneously stops the "refresh read state after a file is modified externally" action. Edit / Write / Notebook each have their own stale-file protection — three separate pieces of logic — and all of them still work (see "Limits and known issues" at the end). But this one does change behavior around the tools, not just the display layer.

Rules you have to follow in practice:

- **Always keep a backup.** The script automatically creates a timestamped backup before every write
- **Replacements must be equal-length.** Changing the file size breaks ELF section offsets and the binary will not run at all
- **Always verify it starts.** The script runs `--version` once before the replacement takes effect
- **If you cannot read it, do not touch it.** The script runs a two-state check on structural targets and refuses to act when the state is ambiguous (see below)

---

## How to search this file safely

This is a file of over two hundred MB (249MB on 2.1.175, 234MB on 2.1.280). Three hard rules, every one of them learned the hard way:

**1. Never read the whole thing into memory**

`fs.readFileSync(p,'utf8')`, `open(p).read()` and `cat` are all off-limits.

**2. Check whether your `grep` is the one you think it is**

Claude Code injects a shell function into every Bash session it opens, hijacking `grep` to the bundled ugrep and `find` to bfs. The function lives in no rc file and cannot be edited away. `type grep` will tell you.

ugrep is designed for searching files in a code repository. **All of those optimizations fall away when it eats a large stream from stdin** — hit a multi-KB single line inside the binary, add a wide backtracking match like `.{0,80}xxx.{0,150}` on top, and memory takes off.

If you are going to use it, write `command grep` or `/usr/bin/grep`.

**3. `| head -N` is not a safeguard**

It only truncates the output; it does nothing about what the upstream process has already pulled into memory.

**Recommended approach: chunked streaming in Python.** Peak memory = one chunk + overlap; 4MB is enough, and a full pass over 249MB takes about 0.7 seconds:

```python
CHUNK = 4 << 20
M = max(len(p) for p in pats)
found = {p: [] for p in pats}
win, win_off = b'', 0
with open(BIN, 'rb') as f:
    while (c := f.read(CHUNK)):
        win += c
        for p in pats:
            i = 0
            while (j := win.find(p, i)) >= 0:
                found[p].append(win_off + j); i = j + 1
        keep = min(len(win), M - 1)
        win_off += len(win) - keep
        win = win[len(win) - keep:]
```

For comparison: one time we ran `strings <binary> | grep` 50 times, which took 27 minutes and pushed swap to 4.4G; the same locating task takes 0.7 seconds with the code above.

If you really do need to search repeatedly, run `strings` **once, out to a text file on disk**, and search that smaller file from then on. Do not keep feeding the raw binary in.

### Not every hit is code

Searching for any single line of UI text hits **2 places**. Searching for a minified identifier hits, on top of the real code references, **one extra place: a symbol name table** (the offsets in the table below are from 2.1.175 and change with every version; how to recognize each one does not):

| Location | What it is | How to recognize it |
|---|---|---|
| High offset (~235–242 million) | **the JS source itself** | readable JS on both sides; you can see `case"..."` and function bodies |
| Low offset (~139 million) | JSC string constant table | tabs and high bytes mixed in; template interpolation points are control characters |
| Low offset (~105 million) | symbol name table | a long run of short identifiers packed side by side, separated by tabs, with no JS syntax at all |

**To change behavior, touch only the JS source hit** — provided that source actually runs; see [Patched does not mean effective](#patched-does-not-mean-effective-bytecode). If all you want is to blank out the text, both of the first two have to be blanked.

The symbol name table looks like this; do not count it as a reference:

```
	vKq  	cI5  	C_6  	FR5  	Rv7  	d5q  	tR7  ...
```

A concrete case: searching for the minified name `Rv7` hits 3 places, one of which is this table, so there are only 2 real code references (the definition plus one default parameter). The reference count varies by symbol — **3 is not a rule** — what you count is whether the surrounding bytes look like JS, not the total.

---

## Six examples

Ordered by increasing difficulty. Examples 1–4 are changes measured to take effect on 2.1.175; Examples 5 and 6 are new on 2.1.280.

**On 2.1.280, the module that Examples 2–6 live in runs from bytecode, so they only take effect together with `--source-fallback`** (see [Patched does not mean effective](#patched-does-not-mean-effective-bytecode)). The minified names in the code snippets below each belong only to their own version.

### Example 1: blank out a piece of text (easiest)

Replace the whole reminder text with the same number of spaces.

| Target | Length | Hits |
|---|---|---|
| `The TodoWrite tool hasn't been used recently...applicable.` | 384 bytes | 2 places |
| `The task tools haven't been used recently...consider using ` | 128 bytes | 2 places |
| ` to update task status (set to in_progress when starting...applicable.` | 250 bytes | 2 places |

The task one is not a single continuous literal but a template interleaved in three pieces (with `${...}` interpolations and a 16-byte header in between), so it is handled piece by piece.

**This approach has a trap worth calling out on its own**: the middle segment ` to add new tasks and ` (22 bytes) does not get blanked, so what is left behind is this:

```
<128 spaces>TaskCreate to add new tasks and TaskUpdate<250 spaces>
```

We assumed at first that the model would not make sense of this leftover fragment, so blanking it was as good as disabling it. **The model does in fact read it**, and it **legitimately blocks it as a prompt injection** — the fragment has no tag, no source, and belongs to no file that was ever read. On one job with seven subagents dispatched, two of them proactively reported this suspicious content.

**It is not harmless noise. It continuously consumes attention and generates false alarms.**

Conclusion: blanking the text is not enough. Either the whole thing disappears, or you disable it where it is generated. The examples below are all the latter.

⚠️ On 2.1.280, each of these three pieces of text hits 2 places: one in the source of a module that runs from bytecode, the other not in any module's source (the bytecode's data area). The script blanks both, but **we have not verified on 280 what effect blanking them has on behavior** — the task reminder only fires after more than ten turns of tool calls in a row, and this round of verification did not cover it.

### Example 2: disable a conditional (4 bytes changed)

```
old: case"task_reminder":{if(!BJ())return[];
new: case"task_reminder":{if(!0   )return[];
                              ^^^^  BJ() these 4 bytes → 0 plus 3 spaces
```

`!0` is always true, so it takes the `return []` path that was already there. **The whole file differs by only 4 bytes**, and spaces inside parentheses are legal in JS.

After this step not even the spaces are produced — there is no leftover fragment, and therefore none of the Example 1 problem.

### Example 3: disable a generator (6 bytes changed)

```
old: y3("changed_files",()=>Xc4(w))
new: y3("changed_files",()=>[]    )
```

The collector returns an empty array. This disables both `edited_text_file` and `edited_image_file` at the generation end: no more silently re-reading files, computing diffs, refreshing read state, or generating file snapshots worth thousands of tokens.

**Why not just blank the text**: the renderer goes on to concatenate `${H.snippet}`, so blanking the fixed sentence would leave the entire file diff behind; and dropping only the text attachment inside the generator would still run all of those hidden re-reads and side effects. Returning an empty array is the narrowest complete shutoff.

### Example 4: delete an object property (22 bytes changed)

```
old: ..._&&{attachedProject:_},currentDate:T07(yPH())}
new: ..._&&{attachedProject:_},/*date-disabled*/     }
```

JS object literals allow a trailing comma, so this property simply disappears with no syntactic residue.

`/*date-disabled*/` is a 17-byte comment, and the remaining 5 spaces pad it out to 22 bytes. **The marker is deliberate** — the script uses it to recognize that this location has already been handled, so a repeat run leaves it alone.

**Why not write in a fixed fake date**: it would replay the Example 1 trap. The crippled string left in the binary gets legitimately blocked by the model as a prompt injection, and generates false alarms on top of that. Either the whole thing disappears, or leave it alone.

⚠️ **On newer versions this example is no longer enough; it stays here as history and teaching material**:

- **From 2.1.258 on, the code is written differently**: the outer rewriting function is gone, replaced by a template literal that builds the sentence directly: `` currentDate:`Today's date is ${Zfe()}.` ``. The old script's regex does not recognize it, the two-state check gets `(0, 0)`, and so it refuses to act — that is the check working correctly. This version of the script recognizes both forms.
- **From 2.1.269 on, the date also comes in a separate `date` attachment.** Measured on 2.1.280: with this patch applied and the module also falling back to source, every request still contains `Today's date is …`. **This patch alone can no longer turn the date off**, and this repository does not handle that attachment.

### Example 5: make a value always empty (email, 19 bytes changed)

2.1.280:

```
old: ANTHROPIC_UNIX_SOCKET?void 0:xn()?.emailAddress,
new: ANTHROPIC_UNIX_SOCKET?void 0:void 0,            
```

This assigns a value to a variable: `undefined` when the `ANTHROPIC_UNIX_SOCKET` environment variable is set, otherwise the account's email is read. Replace the email-reading half with `void 0,` plus spaces and the variable is always `undefined`, so the branch further on that builds the `The user's email address is …` line is never entered.

The anchors are the environment variable name `ANTHROPIC_UNIX_SOCKET` and the property name `.emailAddress` — neither of them gets minified.

### Example 6: make a system-prompt section return nothing (pronouns, 10 bytes changed)

2.1.280:

```
old: qd("pronouns",()=>tVn)
new: qd("pronoun",()=>null)
```

The system prompt is registered section by section: a section name, plus a function that returns that section's content. A section whose function returns `null` is filtered out when the system prompt is assembled.

The hard part is keeping it equal-length. Replacing `()=>tVn` with `()=>null` adds 1 byte, so the section name `"pronouns"` is shortened by 1 byte to `"pronoun"` to make up for it. **The variable name's length differs from version to version**, so how many bytes to cut is computed from the variable name actually matched; when the variable name is longer than 4 characters, shortening the section name cannot make up the difference, and the script refuses to act.

**Why it is not applied by default**: what it removes is a section of the system prompt itself, not a reminder. This section is behavioral guidance Anthropic wrote for the model, and turning it off changes how the model talks. Whether to turn it off is your call; the script only touches it when `--only` names `pronouns`.

---

## The general method: what to do when a new version adds a new injection

The examples above are all tied to specific versions. This section is what this repository is really here to give you.

### Step 1: confirm how it gets in

Claude Code pushes things into the prompt through an attachment mechanism. **What the session log (jsonl) stores is a structured event, not text**:

```json
{"type":"attachment","attachment":{"type":"task_reminder","content":[],"itemCount":0}}
```

The actual text is **assembled at the moment the request is sent**: the `case"<type>":` branch → build a user message → wrap it in `<system-reminder>` → into the API request body, and nothing is written back to disk afterwards.

**Actually observed at runtime**: `task_reminder`, `command_permissions`, `deferred_tools_delta`, `mcp_instructions_delta`, `skill_listing`, `edited_text_file`.

**Present in the code, but not observed by us**: `relevant_memories`, `diagnostics`, `queued_command`, `plan_mode_reentry`.

**This is key for judging whether you are misremembering something**: not finding a sentence's text in the jsonl does not prove it never appeared — only that it was never stored as text. If you want to check, check for the corresponding **attachment record**.

### Step 2: find an anchor that minification will not touch

**Minified names change with every version.** The same function at the same location is `nw()` in one version and `BJ()` in the next. Copying a name out of old notes will patch the wrong bytes.

Two kinds of things do not change:

- **String literals**: `case"task_reminder":`, `"changed_files"`
- **Object property names**: `currentDate:`

Use those as anchors, and write the minified name as the wildcard part of a regex:

```python
# don't hardcode BJ
TASK_LOGIC_LIVE = re.compile(
    rb'case"task_reminder":\{if\(!(?P<target>[A-Za-z_$][\w$]*\(\))\)return\[\];'
)
```

What `(?P<target>...)` captures is exactly the span to replace, and its length comes from the match result, so you never count by hand.

### Step 3: confirm that source actually runs

After you have found the location and before you act, check whether the module it is in runs from bytecode (see [Patched does not mean effective](#patched-does-not-mean-effective-bytecode)). If it does, editing the source has no effect whatsoever: either give up, or make the whole module fall back to running from source and accept the cost described in [Limits and known issues](#limits-and-known-issues).

**When "I patched the right place" and "the behavior did not change" are both true, suspect the execution path first, not that you found the wrong place.**

### Step 4: establish the scope, do not change one number and break three features

A constant name is globally unique, but one constant may feed several features. Enumerate its references before you change anything:

Search for the constant name → rule out the symbol-name-table hit → what remains are the real references → for each reference, look at who calls it → confirm they are all on the path you intend to change → only then act.

Skip this step and you may change one number and break three features.

### Step 5: pick an equal-length editing technique

After the edit the file size has to match to the byte. Three techniques:

| Situation | Method | Example |
|---|---|---|
| Blank out text | replace with the same number of spaces | 384-byte string → 384 spaces |
| Disable a conditional | `!X()` → `!0` plus space padding | `if(!BJ())` → `if(!0   )` |
| Change an order of magnitude | scientific notation moves a single byte | `Rv7=1e4` → `Rv7=1e5` |

The third one is free: changing `1e4` to `1e5` turns 10000 into 100000, and both sides are 3 bytes.

For a decimal form like `50000`, you can switch to `5e4` and pad with spaces — `50000` is 5 bytes, `5e4` is 3 bytes, so you **pad 2 spaces**. Pad exactly the difference, count it again every single time before you write, and do not copy someone else's numbers.

### Step 6: two-state check, refuse to act when you cannot read it

This is the one part of the whole method most worth copying.

Define two regexes for each change: **live** (the original form) and **disabled** (the patched form). After the scan, require the two hit counts to be exactly `(1, 0)` or `(0, 1)`.

- `(1, 0)` = not patched yet, safe to patch
- `(0, 1)` = already patched, skip
- **Anything else = I cannot read this binary, refuse to act**

This is not theoretical caution. While verifying the script for this write-up, we tested it against a backup we believed to be unpatched, and the script errored on the spot:

```
RuntimeError: 2.1.175: currentDate gate is ambiguous (0, 0); inspect this version manually
```

Going to look at that location showed this (the outer one of the two `}}` at the end belongs to a larger enclosing structure — it is not a typo):

```
..._&&{attachedProject:_},                      }}
```

That backup had in fact **already been patched by a much earlier version** — one that blanked the span straight to spaces and left no marker. So it was neither the original form nor a form the current script recognizes. The script correctly refused.

Without this check, the script would read "no live form found" as "nothing to change" — or worse, write bytes at the wrong location.

### Step 7: patch a copy, replace atomically

At any moment there may be dozens of processes on the machine holding this file open. And Linux does not allow writing to a file that is executing (`ETXTBSY`).

```bash
cd ~/.local/share/claude/versions
cp -p <ver> <ver>.bak-orig      # back it up
cp -p <ver> <ver>.tmp           # patch a copy, never in place
python3 your_patch_script.py    # patches .tmp, with byte-exact checks before and after
./<ver>.tmp --version           # confirm the patched copy starts first
mv <ver>.tmp <ver>              # atomic inode swap
```

Ending the intermediate file in `.tmp` is not an arbitrary choice: `strip_injections.py` skips that suffix. If you use a name like `.new`, give up partway, and leave the leftover file behind, the next run of the script will treat it as a real binary and patch it too.

On the same filesystem, `mv` is a rename: **processes already running keep using the old inode and are completely unaffected; only newly started processes get the new one**. Not a single window that is in the middle of work gets interrupted.

The cost is that they need a restart before it takes effect — **a reminder still showing up in your current window after patching is normal, not a sign the patch failed**.

---

## How to verify it actually took effect

### You cannot test it from the session log

Attachments are stored in the jsonl only as structured objects; the rendered text never hits disk. The jsonl looks exactly the same before and after.

### It starts ≠ it is not broken, the check goes green ≠ it took effect

`--version` only proves the ELF still loads. The bytes changed, the size did not, the two-state check went green — in a module that runs from bytecode every one of these holds, and yet the behavior has not changed at all (see [Patched does not mean effective](#patched-does-not-mean-effective-bytecode)). **Only a difference in the requests the CLI actually sends counts as evidence that it took effect.**

### Capture the request bodies and count: a local fake-server comparison

`examples/mockprobe.py` starts a fake API server on this machine, points the CLI at it, saves every request exactly as received, and counts how many times the email, the pronouns section, the file change notification and the date appear in them. **Run it once on the original and once on the patched version, and compare the numbers.**

```bash
python3 examples/mockprobe.py /path/to/original-claude original
python3 examples/mockprobe.py /path/to/patched-claude  patched
```

Nothing leaves this machine and nothing costs money: the CLI only connects to `127.0.0.1`; the outbound proxy points at a port nobody is listening on; HOME and the config directory are freshly created temporary ones with none of your real credentials in them; the token is a fake string, and the fake server never even looks at it.

Measured on 2.1.280 (3 conversation turns and 7 requests per side; each number is the total count of occurrences across the 7 requests):

| | Original | Patched, without `--source-fallback` | Patched, with it |
|---|---|---|---|
| Account email | 7 | 7 | 0 |
| Pronouns section | 7 | 7 | 0 |
| File change notification (`edited_text_file`) | 3 | 3 | 0 |
| Date (`Today's date is`) | 7 | 7 | 7 |
| Time until the first request is sent | 0.37 s | 0.35 s | 0.58 s |

The middle column is "patched does not mean effective" in action: every patch in place, every check green, and what gets sent is the same as the original. The date is there in all three columns; see [Example 4](#example-4-delete-an-object-property-22-bytes-changed) for why. With the fallback on, comparing the first request character by character against the original shows only two differences: the system prompt is missing the pronouns section, and the first message is missing the email block.

A few pitfalls:

- **Do not use `--bare` or an API key when checking the email**: according to the source, the email is never spliced in in those two cases, so the original is 0 too and the comparison means nothing. mockprobe uses a fake OAuth token plus a fake account (the email domain is `example.invalid`).
- **Do not use `--system-prompt` when checking the pronouns section**: it replaces the default system prompt wholesale, so the pronouns section is not in the original either.
- **To verify the file change notification on 280, the positive control has to use Write, not Read**: 280's collector skips read records that carry an offset/limit, and Read records an offset, so "Read, then modify the file externally" produces no notification on either side. mockprobe's tool plan includes both, and you can see that only the file written with Write gets a notification.
- **The outbound proxy points at a port nobody is listening on**: if some request does not go through `ANTHROPIC_BASE_URL`, it fails outright instead of quietly going out. Requests that hit other paths are recorded in `other_endpoints`, which was empty this time.

Earlier, our method was to ask the session running the test "what do you see"; that requires an A/B comparison, and it depends on how the model answers — it may see something and still not mention it. Capturing the request bodies is more direct.

### Mind the trigger conditions

The task reminder only appears after 10 turns of tool calls (the thresholds are "10 turns since the last write" plus "10 turns between two reminders"). The test prompt has to be designed to guarantee more than 12 independent tool calls, otherwise what you are testing is a case where it should not have appeared in the first place.

### Constructing an observable difference is less work than A/B

If what you changed is a behavioral threshold rather than a piece of text, you can just construct input that exceeds the threshold and look at the result. For example, when changing the hook output limit, plant a marker string at the head and at the tail of the output, then ask the session "which one did you see": before the patch only the head one is left (the tail is truncated), after the patch both are there.

---

## Script usage

`strip_injections.py` turns the six examples above into a script you can run repeatedly.

**Requires Python 3.10 or newer** (it uses `X | None` type annotations, so 3.9 and below fail outright on import). Standard library only, no third-party dependencies.

```bash
# See what would change, without writing anything
python3 strip_injections.py --dry-run

# Apply the default set (everything except pronouns)
python3 strip_injections.py

# Disable only the date, leave the rest alone
python3 strip_injections.py --only current-date

# Comma-separated to select more than one; pronouns is only applied when named like this
python3 strip_injections.py --only user-email,pronouns

# Make the patches actually take effect on versions like 2.1.280 (see "Patched does not mean effective")
python3 strip_injections.py --source-fallback

# Installed somewhere else
python3 strip_injections.py --versions-dir /path/to/claude/versions
```

The six names: `task-text`, `task-logic`, `changed-files`, `current-date`, `user-email`, `pronouns`. Without `--only`, the first five are applied.

In the output, each item is followed by which module it lands in and whether that module runs from bytecode:

```
  user email:    1 live / 0 disabled  @ module #333 (bytecode)
  ! bytecode: task-logic, current-date, changed-files, user-email
    sit in a module that runs from bytecode, so editing their source text
    has no effect at runtime. See --source-fallback.
```

The full sequence:

4MB streaming scan (never reads the whole file) → two-state check (refuse on ambiguity) → read the Bun module table and mark which module each site is in (read-only) → with `--source-fallback` on, check whether falling back is possible (refuse if not) → create a timestamped backup → copy to a temporary file → assert equal length at each write, **then compare the original bytes at that site** → check that the total size is unchanged → run `--version` → atomically replace the inode → **re-scan the entire file afterwards to re-verify**.

Two of those steps deserve a separate word:

**"Compare the original bytes at each site before writing" is the most important gate.** Equal length only guarantees the file size stays the same; comparing the original bytes is what guarantees you wrote in the right place — when an offset is computed wrong it aborts on the spot, instead of writing correctly sized garbage somewhere wrong.

By the way: the equal-length check uses Python's `assert`, and `assert` gets stripped out entirely under `python3 -O`. **Do not run this script with `-O`.**

**That final re-verification happens after the replacement.** If it does not pass, the script raises an exception, but the new file is already in place. Rolling back in that case depends on the backup it just made.

Skip rules: files whose name contains `.bak` or `.original`, ends in `.tmp`, or starts with a dot are not processed — **the backup is your rollback point, and it must not get patched along with everything else.** Note that `.tmp` is a suffix match, so a name like `foo.tmp.bin` will not be skipped.

(The temporary file the script itself produces is named `.<original filename>.patch-<pid>`, which starts with a dot and is skipped by the third rule above. The `.tmp` rule is there for when you do this by hand.)

`--dry-run` writes nothing, but the two-state check still runs, so it can also report `gate is ambiguous` and exit right there. That is expected behavior, not a broken dry run — it is telling you that it cannot read this binary's state.

Safe to re-run: an already-patched binary reports `all selected patches already applied`.

**Run it again after every Claude Code version change (upgrade, downgrade, reinstall).** We once forgot to after a downgrade, and were pestered by task reminders for three weeks before noticing.

If you have auto-update turned off like we do, you can otherwise leave it alone.

---

## Limits and known issues

**The stale-file guard is not modified.** What Example 3 disables is the change **notification**. Edit / Write / Notebook each carry their own "this file was modified after you read it" check — three separate pieces of logic — and all of them still work: read a file, modify it externally, then call Edit, and you will still be refused and told to re-read.

**Residual risk**: when writing files through Bash / sed / Python, the model will not be told about external changes on its own. But shell writes bypass the stale guard anyway, independently of this change — reading the current file before you write it is a habit worth having under any circumstances.

**Do not turn on `--skip-version-check` in normal use.** Its only reason to exist is testing the script itself against fake fixtures (a fake file cannot execute, so `--version` is guaranteed to fail). Turning it on means giving up the "does the patched binary still start" check.

**When `--only task-text` is used on its own**, the two-state check still runs against the original three structural targets (task-logic, current-date, changed-files). That is deliberate: that check answers "can I read this binary", not "do I want to patch here". The later additions `user-email` and `pronouns` are only checked when selected, so that older versions without these two sites do not get refused along with them.

**The cost of `--source-fallback`**: the whole module switches to parsing its source. Measured on 2.1.280, a full startup up to sending the first request is about 0.21 seconds slower (0.37 → 0.58 s); fast paths like `--version` are not affected.

**Patches that never took effect will come back to life.** The fallback is a switch for the whole module: any change in that module that was **applied earlier and never took effect** will take effect along with the rest once it falls back. The script checks the patches it knows (see the refusal conditions in [Patched does not mean effective](#patched-does-not-mean-effective-bytecode)), but **it cannot recognize places changed by other tools or by hand**. If you are not sure, start over from the official original: download a clean copy and patch it only with this script.

**The date cannot be fully turned off.** See [Example 4](#example-4-delete-an-object-property-22-bytes-changed): from 2.1.269 on, the date also comes in a separate attachment, which this repository does not handle.

**The version number will go stale.** Every offset and minified name in this document (175's `BJ`, `Xc4`, `T07`, `yPH`; 280's `xn`, `qd`, `tVn` and module number `#333`) belongs only to its own version, and is there for understanding. The script locates by structure and does not depend on them.

---

## License

MIT
