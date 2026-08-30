#!/usr/bin/env python3
"""strip_injections.py -- disable unwanted context injections in the Claude Code CLI binary.

Claude Code ships as a large ELF executable (built with Bun) with its
JavaScript source embedded as plaintext inside the binary. This script scans
that binary for a handful of specific, stable patterns (see VALID_PATCH_NAMES
below) and disables them with equal-length in-place byte patches. Equal
length is mandatory: changing the file size would break ELF section offsets
and produce a binary that fails to load.

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
import subprocess
import sys

VALID_PATCH_NAMES = ("task-text", "task-logic", "changed-files", "current-date")

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
CURRENT_DATE_DISABLED = re.compile(rb'/\*date-disabled\*/ +')
CHANGED_FILES_LIVE = re.compile(
    rb'"changed_files",\(\)=>(?P<target>[A-Za-z_$][\w$]*\([A-Za-z_$][\w$]*\))\)'
)
CHANGED_FILES_DISABLED = re.compile(rb'"changed_files",\(\)=>\[\] +\)')

# Maps each --only name to the internal state/operation name(s) it covers.
CATEGORY_NAMES = {
    "task-text": {name for _, name in PATCHES},
    "task-logic": {"task logic"},
    "changed-files": {"changed files"},
    "current-date": {"currentDate"},
}


def _replacement(kind: str, original: bytes) -> bytes:
    if kind == "task logic":
        return b"0" + b" " * (len(original) - 1)
    if kind == "changed files":
        return b"[]" + b" " * (len(original) - 2)
    if kind == "currentDate":
        marker = b"/*date-disabled*/"
        if len(original) < len(marker):
            raise RuntimeError("currentDate target is too short for disabled marker")
        return marker + b" " * (len(original) - len(marker))
    return b" " * len(original)


def scan_binary(path: pathlib.Path) -> tuple[list[tuple[int, bytes, bytes, str]], dict[str, int]]:
    """Stream the file in fixed-size chunks and return exact equal-length
    writes plus structural hit counts, without ever reading the whole
    file into memory."""
    literal_hits = {name: set() for _, name in PATCHES}
    regex_specs = [
        ("task logic", TASK_LOGIC_LIVE),
        ("currentDate", CURRENT_DATE_LIVE),
        ("currentDate disabled", CURRENT_DATE_DISABLED),
        ("changed files", CHANGED_FILES_LIVE),
        ("task logic disabled", TASK_LOGIC_DISABLED),
        ("changed files disabled", CHANGED_FILES_DISABLED),
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

    state = {name: len(hits) for name, hits in literal_hits.items()}
    state.update({name: len(hits) for name, hits in regex_hits.items()})
    return sorted(operations.values()), state


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


def validate_gate_state(binary_name: str, state: dict[str, int]) -> None:
    """Fail closed: this is a "do I understand this binary" check, so it
    always runs against all three live/disabled pairs, regardless of
    which patches --only selected for writing."""
    for live_key, disabled_key in [
        ("task logic", "task logic disabled"),
        ("currentDate", "currentDate disabled"),
        ("changed files", "changed files disabled"),
    ]:
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
    return names or list(VALID_PATCH_NAMES)


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
            "                 across a day boundary"
        ),
        epilog="""examples:
  strip_injections.py --dry-run
      Preview what would change, using the default versions directory
      (~/.local/share/claude/versions).

  strip_injections.py --versions-dir /path/to/claude/versions
      Apply all four patches to every binary found in a custom directory.

  strip_injections.py --only task-logic,current-date --dry-run
      Preview applying just two of the four available patches.
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
        help=f"comma-separated subset of patches to apply, from: {', '.join(VALID_PATCH_NAMES)} (default: all four)",
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

    selected = parse_only(args.only) if args.only else list(VALID_PATCH_NAMES)
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

    for b in sorted(binaries):
        print(f"-> {b.name}:")
        operations, state = scan_binary(b)
        validate_gate_state(b.name, state)

        for _, name in PATCHES:
            note = "" if "task-text" in selected else "  (not selected)"
            print(f"  {name}: {state[name]} live{note}")
        note = "" if "task-logic" in selected else "  (not selected)"
        print(f"  task logic:    {state['task logic']} live / {state['task logic disabled']} disabled{note}")
        note = "" if "current-date" in selected else "  (not selected)"
        print(f"  current date:  {state['currentDate']} live / {state['currentDate disabled']} disabled{note}")
        note = "" if "changed-files" in selected else "  (not selected)"
        print(f"  changed files: {state['changed files']} live / {state['changed files disabled']} disabled{note}")

        operations = filter_operations(operations, selected)
        if not operations:
            print("  all selected patches already applied")
            continue
        if args.dry_run:
            print(f"  DRY RUN: would apply {len(operations)} equal-length writes")
            continue

        backup = apply_operations(b, operations, dry_run=False, skip_version_check=args.skip_version_check)
        _, after = scan_binary(b)
        validate_gate_state(b.name, after)

        if "changed-files" in selected and (after["changed files"] != 0 or after["changed files disabled"] != 1):
            raise RuntimeError(f"{b.name}: changed-files patch did not land exactly once")
        if "task-logic" in selected and after["task logic"] != 0:
            raise RuntimeError(f"{b.name}: an approved live task-logic pattern remains")
        if "current-date" in selected and (after["currentDate"] != 0 or after["currentDate disabled"] != 1):
            raise RuntimeError(f"{b.name}: an approved live current-date pattern remains")
        if "task-text" in selected and sum(after[name] for _, name in PATCHES) != 0:
            raise RuntimeError(f"{b.name}: a task-text reminder string is still present")

        version_note = "--version check skipped" if args.skip_version_check else "--version passed"
        print(f"  applied {len(operations)} equal-length writes")
        print(f"  backup: {backup}")
        print(f"  size unchanged; {version_note}; inode replaced atomically")


if __name__ == "__main__":
    main()
