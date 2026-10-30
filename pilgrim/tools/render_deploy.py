"""Render deploy templates: `@@NAME@@` -> value (SPLIT_PLAN §5.2).

Deliberately minimal: no conditionals, no loops, no shell. nginx's own
`$variables` are left untouched. Placeholders are `@@NAME@@` (never `${NAME}`)
because nginx uses `$name` itself.

Usage:
    python -m pilgrim.tools.render_deploy TEMPLATE OUTPUT --set NAME=VALUE ...

Fails (exit non-zero) and writes nothing when a value is missing, a `--set`
name does not appear in the template, or any `@@NAME@@` would remain in the
output.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

PLACEHOLDER = re.compile(r"@@([A-Za-z0-9_]+)@@")


def _fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Render @@NAME@@ placeholders in a deploy template.")
    ap.add_argument("template", type=Path)
    ap.add_argument("output", type=Path)
    ap.add_argument("--set", action="append", default=[], metavar="NAME=VALUE",
                    help="placeholder value; repeat for each name")
    args = ap.parse_args(argv)

    values: dict[str, str] = {}
    for item in args.set:
        name, sep, value = item.partition("=")
        if not sep or not name:
            return _fail(f"--set expects NAME=VALUE, got {item!r}")
        values[name] = value

    try:
        text = args.template.read_text()
    except OSError as e:
        return _fail(f"cannot read template {args.template}: {e}")

    used = set(PLACEHOLDER.findall(text))
    unknown = sorted(set(values) - used)
    if unknown:
        return _fail("--set name(s) not present in the template: "
                     + ", ".join(unknown))
    missing = sorted(used - set(values))
    if missing:
        return _fail("missing value(s) for: " + ", ".join(missing))

    out = PLACEHOLDER.sub(lambda m: values[m.group(1)], text)
    leftover = sorted(set(PLACEHOLDER.findall(out)))
    if leftover:
        return _fail("value(s) still contain placeholders: " + ", ".join(leftover))

    try:
        args.output.write_text(out)
    except OSError as e:
        return _fail(f"cannot write {args.output}: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
