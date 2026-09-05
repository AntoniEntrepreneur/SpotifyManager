"""The `restore` subcommand: re-save every album a restore file listed.

The safety loop `dedupe` promises only holds if this command keeps its own half of
the bargain: the file is read and fully parsed and validated *before* a single
request goes out, so a missing or malformed restore file produces a clear message
and touches nothing -- never a partial restore. Past that point, the albums are
re-saved with the same batching, retries and honest three-way classification that
`dedupe` uses for deletion, because a restore run can fail in exactly the same ways.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..config import Config, load_config
from ..dedupe.execute import ExecutionResult, restore
from ..dedupe.restore import RestoreDocument, RestoreDocumentError
from ..errors import SpotifyManagerError
from .library import build_client


class RestoreFileNotFoundError(SpotifyManagerError):
    """The given path does not name a file this command can read."""


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "restore_file",
        type=Path,
        metavar="RESTORE_FILE",
        help="path to a restore file written by `dedupe`",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="print every API request to stderr",
    )


def run(args: argparse.Namespace) -> int:
    # The file is read and validated before anything else -- including credentials
    # -- so a bad restore file fails the same clear way whether or not Spotify
    # login is even configured, and never as a side effect of reaching the API.
    document = load_restore_file(args.restore_file)
    config: Config = load_config()

    if not document.album_ids:
        # A restore file with an empty list is not malformed -- `dedupe` never
        # writes one, but nothing rules it out -- it just names nothing to do.
        print(f"{args.restore_file} lists no albums. Nothing was restored.")
        return 0

    print(f"Restoring {len(document.album_ids)} album(s) from {args.restore_file}.")
    result = restore(
        document.album_ids,
        build_client(config, verbose=args.verbose),
        on_progress=lambda line: print(line, flush=True),
    )

    print()
    print(format_result(result))
    # A restore that did not finish did not succeed, and should not tell a shell
    # script that it did.
    return 0 if result.is_clean else 1


def load_restore_file(path: Path) -> RestoreDocument:
    """Read and completely validate the restore file before anything is sent.

    Reading, JSON-decoding and schema validation all happen here, in that order,
    before the first `save_albums` call -- so any of the three ways a restore file
    can be wrong (missing, not JSON, wrong shape) ends the run the same way: a clear
    message, nothing restored.

    Raises:
        RestoreFileNotFoundError: the path cannot be read.
        RestoreDocumentError: the contents are not valid JSON, or not a valid
            restore document.
    """
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RestoreFileNotFoundError(
            f"Could not read restore file {path}: {exc}. Nothing was restored."
        ) from exc
    try:
        raw = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise RestoreDocumentError(
            f"{path} is not valid JSON ({exc}). Nothing was restored."
        ) from exc
    return RestoreDocument.from_raw(raw)


def format_result(result: ExecutionResult) -> str:
    """What the run did, in the terminal, with nothing summarised away.

    The same shape as `dedupe_cmd.format_results`, because a reader who has already
    seen one recognises the other: requested, succeeded, failed, never attempted.
    """
    lines = [
        "Results",
        f"  requested to restore   {result.requested_count}",
        f"  restored               {result.removed_count}",
        f"  failed                 {result.failed_count}",
        f"  never attempted        {result.never_attempted_count}",
    ]
    if result.is_clean:
        lines += ["", "Every album was restored."]
        return "\n".join(lines)

    lines += ["", "THIS RUN DID NOT FINISH. Some albums may not have been restored."]
    lines += _listing(
        "Failed",
        result.failed_ids,
        "these requests were sent and errored; those albums may or may not be saved",
    )
    lines += _listing(
        "Never attempted",
        result.never_attempted_ids,
        "no request was ever issued for these; they were not restored",
    )
    for batch in result.failed_batches:
        lines += ["", f"Batch {batch.number} error: {batch.error}"]
    return "\n".join(lines)


def _listing(title: str, ids: tuple[str, ...], note: str) -> list[str]:
    if not ids:
        return []
    return ["", f"{title} ({len(ids)}) -- {note}"] + [f"  {album_id}" for album_id in ids]
