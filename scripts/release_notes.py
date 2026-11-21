"""Print the CHANGELOG.md section of one version. The release workflow uses
it as the text of the GitHub release.

Usage:
    python scripts/release_notes.py 0.2.0
"""

import re
import sys
from collections.abc import Sequence
from pathlib import Path

CHANGELOG = Path(__file__).resolve().parent.parent / "CHANGELOG.md"
# The next "## " heading, or the link definitions at the bottom ("[0.1.0]: https://...").
SECTION_END = re.compile(r"^(## |\[[^\]]+\]: )")


def section(changelog: str, version: str) -> str:
    lines = changelog.splitlines()
    heading = f"## [{version}]"
    starts = [i for i, line in enumerate(lines) if line.startswith(heading)]
    if not starts:
        raise LookupError(f"CHANGELOG.md has no '{heading}' section")
    body: list[str] = []
    for line in lines[starts[0] + 1 :]:
        if SECTION_END.match(line):
            break
        body.append(line)
    text = "\n".join(body).strip()
    if not text:
        raise LookupError(f"the '{heading}' section of CHANGELOG.md is empty")
    return text


def main(argv: Sequence[str]) -> int:
    if len(argv) != 1:
        print("usage: release_notes.py VERSION", file=sys.stderr)
        return 2
    try:
        print(section(CHANGELOG.read_text("utf-8"), argv[0]))
    except LookupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
