#!/usr/bin/env python3
"""strip_injections.py -- disable unwanted context injections in the Claude Code CLI binary.

Claude Code ships as a large ELF executable (built with Bun) with its
JavaScript source embedded as plaintext inside the binary. This script scans
that binary for a handful of specific, stable patterns (see VALID_PATCH_NAMES
below) and disables them with equal-length in-place byte patches. Equal
length is mandatory: changing the file size would break ELF section offsets
and produce a binary that fails to load.

Patched is not the same as effective. Bun can also store a module as
precompiled bytecode, and then the loader runs the bytecode and the source
text is never executed. On 2.1.280 every code patch here sits in such a
module. The script reads Bun's module table and marks every hit with the
module it belongs to and whether that module runs from bytecode. With
--source-fallback (off by default) it also zeroes that module's bytecode
reference so the loader falls back to the patched source; see
plan_source_fallback() for when it refuses. Only a difference in the
requests the CLI actually sends proves a patch works -- examples/mockprobe.py
records them against a local mock API.

The binary's internal identifier names are minified and change with every
release, so this script never matches on them. It anchors only on
structural literals -- string literals and object property names -- that
survive minification and stay stable across versions, e.g.
case"task_reminder": or currentDate:.

Safety model:
  - Streams the binary in 4 MB chunks (with a small overlap so a pattern
    can't be missed across a chunk boundary); never loads the whole file
    into memory.
  - For every patch, requires the "live" pattern and its "disabled" marker
    to together be found in exactly one of two states: (1 live, 0 disabled)
    or (0 live, 1 disabled). Anything else means this script does not
    confidently understand the binary's structure, and it refuses to touch it.
  - Always makes a timestamped backup before writing, and writes to a
    temporary copy rather than in place (a running executable on Linux
    cannot be opened for writing -- ETXTBSY).
  - After writing: verifies every write was equal-length, verifies total
    file size is unchanged, and runs the patched copy with --version to
    confirm it still executes -- only then atomically swaps it into place
    with os.replace(). A process that already has the binary open keeps
    using the old inode until it restarts.

Run with --help for the full list of patches and usage examples.
"""
import argparse
import datetime as dt
import os
import pathlib
import re
import shutil
import struct
import subprocess
import sys
from typing import NamedTuple

VALID_PATCH_NAMES = ("task-text", "task-logic", "changed-files", "current-date", "user-email", "pronouns")
# pronouns deletes a section of the system prompt itself, so it is only
# applied when named explicitly with --only.
DEFAULT_PATCH_NAMES = tuple(n for n in VALID_PATCH_NAMES if n != "pronouns")

# The three literal reminder strings Claude Code injects into context.
# Blanking these to spaces removes the reminder text itself.
PATCHES = [
    (b"The TodoWrite tool hasn't been used recently. If you're working on tasks that would benefit from tracking progress, consider using the TodoWrite tool to track progress. Also consider cleaning up the todo list if has become stale and no longer matches what you are working on. Only use it if it's relevant to the current work. This is just a gentle reminder - ignore if not applicable.", "task reminder (full)"),
    (b"The task tools haven't been used recently. If you're working on tasks that would benefit from tracking progress, consider using ", "task reminder (prefix)"),
    (b" to update task status (set to in_progress when starting, completed when done). Also consider cleaning up the task list if it has become stale. Only use these if relevant to the current work. This is just a gentle reminder - ignore if not applicable.", "task reminder (suffix)"),
]

CHUNK = 4 << 20
OVERLAP = 1024

# Structural anchors only -- minified identifiers (nw(), BJ(), etc.) are
# not stable across releases, but string literals and property names like
# these are, because minifiers do not rename them.
TASK_LOGIC_LIVE = re.compile(
    rb'case"task_reminder":\{if\(!(?P<target>[A-Za-z_$][\w$]*\(\))\)return\[\];'
)
TASK_LOGIC_DISABLED = re.compile(rb'case"task_reminder":\{if\(!0 +\)return\[\];')
CURRENT_DATE_LIVE = re.compile(
    rb'(?P<target>currentDate:[A-Za-z_$][\w$]*\([A-Za-z_$][\w$]*\(\)\))'
)
# 2.1.258 replaced the wrapper call with a template literal that builds the
# sentence directly:  currentDate:`Today's date is ${Zfe()}.`
# 2.1.267/269 gave the date function one argument. Accept zero arguments or a
# single identifier -- never an arbitrary expression.
CURRENT_DATE_LIVE_TEMPLATE = re.compile(
    rb"(?P<target>currentDate:`Today's date is \$\{[A-Za-z_$][\w$]*\((?:[A-Za-z_$][\w$]*)?\)\}\.`)"
)
CURRENT_DATE_DISABLED = re.compile(rb'/\*date-disabled\*/ +')
CHANGED_FILES_LIVE = re.compile(
    rb'"changed_files",\(\)=>(?P<target>[A-Za-z_$][\w$]*\([A-Za-z_$][\w$]*\))\)'
)
CHANGED_FILES_DISABLED = re.compile(rb'"changed_files",\(\)=>\[\] +\)')
# The account email in the first user message's session context (only
# rendered for claude.ai OAuth logins):
#     let M=a.ANTHROPIC_UNIX_SOCKET?void 0:xn()?.emailAddress,
# becomes `void 0,` plus spaces, so M is always undefined and that line is
# never built.
USER_EMAIL_LIVE = re.compile(
    rb'ANTHROPIC_UNIX_SOCKET\?void 0:(?P<target>[A-Za-z_$][\w$]*\(\)\?\.emailAddress,)'
)
USER_EMAIL_DISABLED = re.compile(rb'ANTHROPIC_UNIX_SOCKET\?void 0:void 0, +')
# The pronoun section of the system prompt:
#     qd("pronouns",()=>tVn)  ->  qd("pronoun",()=>null)
# A section whose thunk returns null is filtered out when the prompt is
# assembled. The section name is shortened by exactly as many bytes as
# `null` is longer than the variable name, to keep the length equal.
PRONOUNS_LIVE = re.compile(
    rb'(?P<target>\("pronouns",\(\)=>(?P<v>[A-Za-z_$][\w$]*)\))'
)
PRONOUNS_DISABLED = re.compile(rb'\("prono(?:u|un|uns)?",\(\)=>null\)')

# Maps each --only name to the internal state/operation name(s) it covers.
CATEGORY_NAMES = {
    "task-text": {name for _, name in PATCHES},
    "task-logic": {"task logic"},
    "changed-files": {"changed files"},
    "current-date": {"currentDate"},
    "user-email": {"user email"},
    "pronouns": {"pronouns"},
}

# Structural (code) patches: (--only name, report label, live key, disabled key).
GATES = [
    ("task-logic", "task logic", "task logic", "task logic disabled"),
    ("current-date", "current date", "currentDate", "currentDate disabled"),
    ("changed-files", "changed files", "changed files", "changed files disabled"),
    ("user-email", "user email", "user email", "user email disabled"),
    ("pronouns", "pronouns", "pronouns", "pronouns disabled"),
]
# These three are checked on every run, whatever --only selects: that check
# answers "do I understand this binary at all". The newer anchors are only
# checked when selected, so older versions without them are not refused.
ALWAYS_CHECKED = ("task-logic", "current-date", "changed-files")


class Module(NamedTuple):
    index: int
    name: str
    src_start: int   # absolute file offsets of this module's JS source text
    src_end: int
    bytecode: bool   # True: the loader runs precompiled bytecode, not the source
    record: int      # absolute file offset of this module's directory record


BUN_TRAILER = b"\n---- Bun! ----\n"
RECORD_SIZE = 52


def read_module_table(path: pathlib.Path) -> list[Module]:
    """Read-only parse of the Bun standalone module directory.

    Bun can store a module as JS source *and* precompiled bytecode; when the
    bytecode is present the loader uses it and the source text is only kept
    around for stack traces. Editing the source of such a module changes
    nothing at runtime. The only reliable test is the module's own directory
    record: a non-zero bytecode length means its source is not what runs.

    Layout (verified on 2.1.258 - 2.1.280): ELF section ".bun" = u64 length,
    then the graph; a 32-byte offsets block and a fixed trailer at the end of
    the section locate a directory of 52-byte records. Each record is 12
    little-endian u32 (name, source, sourcemap, bytecode, module_info, aux --
    each an offset/length pair relative to the graph start) plus 4 flag bytes.
    Anything that does not look exactly like that raises instead of guessing.
    """
    with path.open("rb") as f:
        header = struct.unpack("<16sHHIQQQIHHHHHH", f.read(64))
        if header[0][:6] != b"\x7fELF\x02\x01":
            raise RuntimeError("unsupported executable: expected little-endian ELF64")
        shoff, entsize, count, names_index = header[6], header[11], header[12], header[13]
        if entsize != 64 or not 0 < count < 4096 or names_index >= count:
            raise RuntimeError("unsupported ELF section table")
        f.seek(shoff)
        sections = [struct.unpack("<IIQQQQIIQQ", f.read(entsize)) for _ in range(count)]
        names = sections[names_index]
        if names[5] > 1 << 20:
            raise RuntimeError("ELF section names exceed bounded read")
        f.seek(names[4])
        strings = f.read(names[5])
        matches = [s for s in sections if strings[s[0]:].split(b"\0", 1)[0] == b".bun"]
        if len(matches) != 1:
            raise RuntimeError("expected exactly one .bun section")
        section = matches[0]
        base, end = section[4] + 8, section[4] + section[5]
        f.seek(end - len(BUN_TRAILER) - 32)
        offsets = f.read(32)
        if f.read(len(BUN_TRAILER)) != BUN_TRAILER:
            raise RuntimeError("unsupported Bun trailer")
        byte_count, table, table_size, *_ = struct.unpack("<Q6I", offsets)
        if (table_size % RECORD_SIZE or table_size > 4 << 20 or
                table + table_size > byte_count or base + byte_count > end):
            raise RuntimeError("unsupported Bun module directory bounds/stride")
        f.seek(base + table)
        records = f.read(table_size)
        modules = []
        for off in range(0, table_size, RECORD_SIZE):
            fields = struct.unpack_from("<12I4B", records, off)
            if not 0 < fields[1] <= 512 or fields[0] + fields[1] > byte_count:
                raise RuntimeError("invalid Bun module name bounds")
            f.seek(base + fields[0])
            name = f.read(fields[1])
            if not name.startswith(b"/$bunfs/"):
                raise RuntimeError("invalid Bun module directory record")
            if fields[2] + fields[3] > byte_count:
                raise RuntimeError("invalid Bun source bounds")
            if fields[3]:
                modules.append(Module(
                    off // RECORD_SIZE, name.decode("utf-8", "replace"),
                    base + fields[2], base + fields[2] + fields[3],
                    bool(fields[7]), base + table + off,
                ))
        return modules


def module_of(modules: list[Module], pos: int) -> Module | None:
    """The module whose JS source contains `pos`, or None (e.g. a string
    constant stored inside bytecode data rather than in any source text)."""
    found = [m for m in modules if m.src_start <= pos < m.src_end]
    if len(found) > 1:
        raise RuntimeError(f"offset {pos} belongs to more than one Bun source module")
    return found[0] if found else None


def describe_location(modules: list[Module], positions) -> str:
    """Short 'where are these hits' note for the report, e.g.
    '@ module #333 (bytecode)' or '@ module #532 (bytecode) + 1 outside JS source'."""
    if not positions:
        return ""
    parts, outside = {}, 0
    for pos in sorted(positions):
        m = module_of(modules, pos)
        if m is None:
            outside += 1
        else:
            parts[m.index] = f"module #{m.index} ({'bytecode' if m.bytecode else 'source'})"
    notes = list(parts.values())
    if outside:
        notes.append(f"{outside} outside JS source")
    return "@ " + " + ".join(notes)


FALLBACK_FIELDS = 24   # record+24: bytecode offset/length, module_info offset/length (4 x u32)


def plan_source_fallback(path: pathlib.Path, modules: list[Module], hits, selected: list[str]):
    """--source-fallback: make the module holding the selected code patches
    run from its (patched) JS source instead of its precompiled bytecode, by
    zeroing the 16 bytes at record+24. The bytecode itself is left in place,
    just no longer referenced.

    This is a whole-module switch, so it refuses unless it can say exactly
    what will start taking effect:
      - every selected code anchor must sit in the JS source of one and the
        same module;
      - that module must not contain a patch from an earlier run that is not
        selected now (reverting would silently bring it to life);
      - a module without bytecode must already have all 16 bytes zeroed.
    Returns (operation or None, module, message)."""
    wanted = [(cli, live, disabled) for cli, _label, live, disabled in GATES if cli in selected]
    if not wanted:
        raise RuntimeError("--source-fallback needs at least one code patch selected "
                           f"({', '.join(g[0] for g in GATES)})")
    owners = {}
    for cli, live, disabled in wanted:
        for pos in hits[live] | hits[disabled]:
            m = module_of(modules, pos)
            if m is None:
                raise RuntimeError(f"{path.name}: {cli} anchor at {pos} is outside any module's JS source; "
                                   "refusing to guess which module to fall back")
            owners.setdefault(m, set()).add(cli)
    if len(owners) != 1:
        found = ", ".join(f"#{m.index}: {sorted(c)}" for m, c in owners.items())
        raise RuntimeError(f"{path.name}: selected code patches span several modules ({found}); "
                           "use --only to fall back one module at a time, or inspect manually")
    module = next(iter(owners))
    with path.open("rb") as f:
        f.seek(module.record + FALLBACK_FIELDS)
        raw = f.read(16)
    if len(raw) != 16:
        raise RuntimeError(f"{path.name}: short read at module #{module.index} record")
    if not module.bytecode:
        if raw != bytes(16):
            raise RuntimeError(f"{path.name}: module #{module.index} has no bytecode but its module-info "
                               f"fields are non-zero ({raw.hex()}); inspect this version manually")
        return None, module, "already runs from source"
    for cli, _label, _live, disabled in GATES:
        if cli in selected:
            continue
        if any(module_of(modules, pos) == module for pos in hits[disabled]):
            raise RuntimeError(
                f"{path.name}: module #{module.index} also contains the {cli} patch from an earlier run, "
                "which is not selected now. Falling back would make that edit take effect too. "
                f"Add {cli} to --only, or start again from an unpatched binary.")
    op = (module.record + FALLBACK_FIELDS, raw, bytes(16), "source fallback")
    return op, module, "runs from bytecode; will clear 16 bytes of its directory record"


def _replacement(kind: str, original: bytes) -> bytes:
    if kind == "task logic":
        return b"0" + b" " * (len(original) - 1)
    if kind == "changed files":
        return b"[]" + b" " * (len(original) - 2)
    if kind == "user email":
        return b"void 0," + b" " * (len(original) - len(b"void 0,"))
    if kind == "pronouns":
        m = PRONOUNS_LIVE.fullmatch(original)
        if m is None:
            raise RuntimeError(f"pronouns target has unexpected shape: {original!r}")
        # ("pronouns",()=>V) is 17+len(V) bytes; ("NAME",()=>null) is 13+len(NAME)
        keep = 4 + len(m.group("v"))
        if keep > len(b"pronouns"):
            raise RuntimeError(f"pronouns thunk name too long to shrink equal-length: {original!r}")
        return b'("' + b"pronouns"[:keep] + b'",()=>null)'
    if kind == "currentDate":
        marker = b"/*date-disabled*/"
        if len(original) < len(marker):
            raise RuntimeError("currentDate target is too short for disabled marker")
        return marker + b" " * (len(original) - len(marker))
    return b" " * len(original)


def scan_binary(path: pathlib.Path):
    """Stream the file in fixed-size chunks and return exact equal-length
    writes, structural hit counts, and the file offset of every hit --
    without ever reading the whole file into memory."""
    literal_hits = {name: set() for _, name in PATCHES}
    regex_specs = [
        ("task logic", TASK_LOGIC_LIVE),
        ("currentDate", CURRENT_DATE_LIVE),
        ("currentDate", CURRENT_DATE_LIVE_TEMPLATE),
        ("currentDate disabled", CURRENT_DATE_DISABLED),
        ("changed files", CHANGED_FILES_LIVE),
        ("task logic disabled", TASK_LOGIC_DISABLED),
        ("changed files disabled", CHANGED_FILES_DISABLED),
        ("user email", USER_EMAIL_LIVE),
        ("user email disabled", USER_EMAIL_DISABLED),
        ("pronouns", PRONOUNS_LIVE),
        ("pronouns disabled", PRONOUNS_DISABLED),
    ]
    regex_hits = {name: set() for name, _ in regex_specs}
    operations: dict[int, tuple[int, bytes, bytes, str]] = {}
    win, base = b"", 0

    with path.open("rb") as f:
        while c := f.read(CHUNK):
            win += c
            for needle, name in PATCHES:
                start = 0
                while (idx := win.find(needle, start)) >= 0:
                    pos = base + idx
                    literal_hits[name].add(pos)
                    operations[pos] = (pos, needle, b" " * len(needle), name)
                    start = idx + 1
            for name, pattern in regex_specs:
                for match in pattern.finditer(win):
                    pos = base + match.start()
                    regex_hits[name].add(pos)
                    if "disabled" in name:
                        continue
                    start, end = match.span("target")
                    target = match.group("target")
                    target_pos = base + start
                    operations[target_pos] = (
                        target_pos, target, _replacement(name, target), name
                    )
            keep = min(len(win), OVERLAP)
            base += len(win) - keep
            win = win[-keep:]

    hits = {**literal_hits, **regex_hits}
    state = {name: len(positions) for name, positions in hits.items()}
    return sorted(operations.values()), state, hits


def filter_operations(operations, selected: list[str]):
    """Keep only the writes that belong to a category named in `selected`."""
    allowed = set()
    for cli_name in selected:
        allowed |= CATEGORY_NAMES[cli_name]
    return [op for op in operations if op[3] in allowed]


def apply_operations(
    path: pathlib.Path, operations, *, dry_run: bool, skip_version_check: bool = False
) -> pathlib.Path | None:
    if dry_run or not operations:
        return None

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.bak-pre-patch-{stamp}")
    tmp = path.with_name(f".{path.name}.patch-{os.getpid()}")
    shutil.copy2(path, backup)
    shutil.copy2(path, tmp)
    original_size = path.stat().st_size

    try:
        with tmp.open("r+b") as f:
            for offset, expected, replacement, _name in operations:
                assert len(expected) == len(replacement)
                f.seek(offset)
                actual = f.read(len(expected))
                if actual != expected:
                    raise RuntimeError(
                        f"bytes changed at {offset}: expected {expected!r}, got {actual!r}"
                    )
                f.seek(offset)
                f.write(replacement)
            f.flush()
            os.fsync(f.fileno())
        if tmp.stat().st_size != original_size:
            raise RuntimeError("binary size changed")
        if not skip_version_check:
            subprocess.run(
                [str(tmp), "--version"], check=True, timeout=20,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
        os.replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    return backup


def validate_gate_state(binary_name: str, state: dict[str, int], selected: list[str]) -> None:
    """Fail closed: this is a "do I understand this binary" check. The three
    original live/disabled pairs are always checked, regardless of which
    patches --only selected for writing; the newer ones when selected."""
    for cli_name, _label, live_key, disabled_key in GATES:
        if cli_name not in ALWAYS_CHECKED and cli_name not in selected:
            continue
        pair = (state[live_key], state[disabled_key])
        if pair not in {(1, 0), (0, 1)}:
            raise RuntimeError(
                f"{binary_name}: {live_key} gate is ambiguous {pair}; inspect this version manually"
            )


def parse_only(value: str) -> list[str]:
    names = [n.strip() for n in value.split(",") if n.strip()]
    invalid = [n for n in names if n not in VALID_PATCH_NAMES]
    if invalid:
        print(
            f"invalid --only name(s): {', '.join(invalid)}\n"
            f"valid names: {', '.join(VALID_PATCH_NAMES)}",
            file=sys.stderr,
        )
        sys.exit(1)
    return names or list(DEFAULT_PATCH_NAMES)


def main():
    parser = argparse.ArgumentParser(
        prog="strip_injections.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Disable unwanted context injections baked into a Claude Code CLI\n"
            "binary, using equal-length byte patches so the file size (and\n"
            "therefore ELF layout) never changes. Backs up the original, patches\n"
            "a temp copy, and verifies it before installing.\n\n"
            "Patches (see --only):\n"
            "  task-text      the literal task/todo reminder strings\n"
            "  task-logic     the code path that decides whether to emit one\n"
            "  changed-files  the recently-edited-files context attachment\n"
            "  current-date   the currentDate field that breaks prompt caching\n"
            "                 across a day boundary (2.1.269+ also sends the date\n"
            "                 as a separate attachment, which this does not touch)\n"
            "  user-email     the account email line in the session context\n"
            "  pronouns       the pronoun section of the system prompt\n"
            "                 (NOT applied by default -- name it with --only)"
        ),
        epilog="""examples:
  strip_injections.py --dry-run
      Preview what would change, using the default versions directory
      (~/.local/share/claude/versions).

  strip_injections.py --versions-dir /path/to/claude/versions
      Apply the default set (everything except pronouns) to every binary
      found in a custom directory.

  strip_injections.py --only task-logic,current-date --dry-run
      Preview applying just two of the available patches.

  strip_injections.py --only user-email,pronouns --dry-run
      pronouns is only ever applied when named like this.
""",
    )
    parser.add_argument("--dry-run", action="store_true", help="report what would change without writing anything")
    parser.add_argument(
        "--versions-dir",
        type=pathlib.Path,
        default=pathlib.Path.home() / ".local/share/claude/versions",
        help="directory containing Claude Code binaries (default: %(default)s)",
    )
    parser.add_argument(
        "--only",
        metavar="NAME[,NAME...]",
        help=(f"comma-separated subset of patches to apply, from: {', '.join(VALID_PATCH_NAMES)} "
              f"(default: {', '.join(DEFAULT_PATCH_NAMES)})"),
    )
    parser.add_argument(
        "--source-fallback",
        action="store_true",
        help=(
            "also make the module holding the selected code patches run from its JS "
            "source instead of precompiled bytecode, so the patches actually take "
            "effect. Off by default: it switches a whole module, and slows startup a "
            "little. Refuses when it cannot tell exactly what would start taking effect."
        ),
    )
    parser.add_argument(
        "--skip-version-check",
        action="store_true",
        help=(
            "skip running the patched copy with --version before installing it. "
            "This removes a real safety check; it exists for testing this tool "
            "against non-executable fixtures, not for normal use."
        ),
    )
    args = parser.parse_args()

    selected = parse_only(args.only) if args.only else list(DEFAULT_PATCH_NAMES)
    print(f"selected patches: {', '.join(selected)}")

    versions_dir = args.versions_dir
    if not versions_dir.exists():
        print(f"versions dir not found: {versions_dir}", file=sys.stderr)
        sys.exit(1)

    # Never touch backups or temp files left by a previous run: a .bak-*
    # file is the rollback point, and patching it would destroy that.
    binaries = [p for p in versions_dir.iterdir()
                if p.is_file() and not p.name.startswith(".")
                and ".bak" not in p.name and ".original" not in p.name
                and not p.name.endswith(".tmp")]
    if not binaries:
        print("no binaries found")
        return

    print("note: live/disabled describe the JS source text only. '@ module #N (bytecode)'")
    print("      means that text is not what runs -- see the README section on bytecode.")

    for b in sorted(binaries):
        print(f"-> {b.name}:")
        operations, state, hits = scan_binary(b)
        validate_gate_state(b.name, state, selected)
        modules = read_module_table(b)

        for _, name in PATCHES:
            note = "" if "task-text" in selected else "  (not selected)"
            where = describe_location(modules, hits[name])
            print(f"  {name}: {state[name]} live{note}  {where}".rstrip())
        inert = []
        for cli_name, label, live, disabled in GATES:
            note = "" if cli_name in selected else "  (not selected)"
            positions = hits[live] | hits[disabled]
            where = describe_location(modules, positions)
            print(f"  {label + ':':<15}{state[live]} live / {state[disabled]} disabled{note}  {where}".rstrip())
            if cli_name in selected and any(
                    (m := module_of(modules, p)) is not None and m.bytecode for p in positions):
                inert.append(cli_name)

        operations = filter_operations(operations, selected)
        fallback = fb_module = None
        if args.source_fallback:
            fallback, fb_module, fb_note = plan_source_fallback(b, modules, hits, selected)
            print(f"  source fallback: module #{fb_module.index} {fb_note}")
            if fallback:
                operations = sorted(operations + [fallback])
            inert = []   # every selected code patch lives in that module, which will run from source
        if inert:
            print(f"  ! bytecode: {', '.join(inert)}")
            print("    sit in a module that runs from bytecode, so editing their source text")
            print("    has no effect at runtime. See --source-fallback.")

        if not operations:
            print("  all selected patches already applied")
            continue
        if args.dry_run:
            print(f"  DRY RUN: would apply {len(operations)} equal-length write{'s' * (len(operations) != 1)}")
            continue

        backup = apply_operations(b, operations, dry_run=False, skip_version_check=args.skip_version_check)
        _, after, _ = scan_binary(b)
        validate_gate_state(b.name, after, selected)

        for cli_name, _label, live, disabled in GATES:
            if cli_name in selected and (after[live] != 0 or after[disabled] != 1):
                raise RuntimeError(f"{b.name}: {cli_name} patch did not land exactly once")
        if "task-text" in selected and sum(after[name] for _, name in PATCHES) != 0:
            raise RuntimeError(f"{b.name}: a task-text reminder string is still present")
        if fallback:
            now = next((m for m in read_module_table(b) if m.index == fb_module.index), None)
            with b.open("rb") as f:
                f.seek(fb_module.record + FALLBACK_FIELDS)
                cleared = f.read(16) == bytes(16)
            if now is None or now.bytecode or not cleared:
                raise RuntimeError(f"{b.name}: source fallback for module #{fb_module.index} did not land")

        version_note = "--version check skipped" if args.skip_version_check else "--version passed"
        print(f"  applied {len(operations)} equal-length write{'s' * (len(operations) != 1)}")
        print(f"  backup: {backup}")
        print(f"  size unchanged; {version_note}; inode replaced atomically")


if __name__ == "__main__":
    main()
