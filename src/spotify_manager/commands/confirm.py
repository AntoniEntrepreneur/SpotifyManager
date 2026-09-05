"""Asking the user to approve a bulk write, and refusing to guess when we cannot ask.

Both commands that write thousands of ids to Spotify need the same three rules, and
each rule exists to prevent a specific accident:

* **A non-terminal stdin without `--yes` refuses.** Not proceeds, and not blocks. A
  redirect must never be able to approve a ten-thousand-row mutation on the user's
  behalf, and a CI job must never hang on a prompt nobody can answer.
* **The stream we check is the stream we read.** Not `input()`'s idea of stdin: the
  two must not be able to disagree about which thing was asked and which answered.
* **EOF is not a yes.** A terminal that went away mid-question answered nothing.

The wording is passed in rather than hardcoded, because "Like 9,412 tracks?" and
"Unlike 9,412 tracks?" are not the same question and the count is the part the user
is actually being asked to check.
"""

from __future__ import annotations

import sys


def confirmed(
    prompt: str,
    *,
    refusal: str,
    assume_yes: bool,
    stream=None,
) -> bool:
    """Whether the user has actually agreed.

    Args:
        prompt: the question, count included, without the trailing instruction.
        refusal: what to print when stdin cannot be asked -- named for the command,
            so the message says which write is being declined.
        assume_yes: `--yes` was given, and the question is not asked at all.
        stream: injected for tests; defaults to real stdin.
    """
    if assume_yes:
        return True
    stream = stream if stream is not None else sys.stdin
    if not (hasattr(stream, "isatty") and stream.isatty()):
        print(
            f"{refusal}\n"
            "Re-run with --yes to approve this, or --dry-run to see the plan.",
            file=sys.stderr,
        )
        return False
    print(f"{prompt} Type 'yes' to confirm: ", end="", flush=True)
    answer = stream.readline()
    if not answer:  # EOF: the terminal went away mid-question. That is not a yes.
        print()
        return False
    return answer.strip().lower() == "yes"
