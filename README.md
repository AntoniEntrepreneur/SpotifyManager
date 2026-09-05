# spotify-manager

A command-line tool for managing a personal Spotify library. The first feature under
construction is saved-album deduplication: finding albums saved twice under different
editions (a plain release alongside a Deluxe or Remastered one) and proposing a
cleanup that only ever runs after explicit approval.

**Current state:** `dedupe` reviews and, once approved in the browser, carries out the
cleanup, writing a restore file before it removes anything; `restore` puts back
everything a restore file lists. `like-album-tracks` likes every song on every saved
album that is not liked yet, writing a run record before it adds anything;
`unlike-tracks` spends that record to undo the run. Nothing is ever written to Spotify
without an explicit approval and a way back.

## Setup

### 1. Create a Spotify app

At <https://developer.spotify.com/dashboard>, create an app. Then open its
**Settings** and add a redirect URI:

```
http://127.0.0.1:8888/callback
```

It must match the value you put in `.env` character for character.

### 2. Configure credentials

Create a `.env` file in the repository root (it is gitignored, and must stay that
way):

```
SPOTIPY_CLIENT_ID=<the Client ID from your Spotify app>
SPOTIPY_CLIENT_SECRET=<the Client secret from your Spotify app>
SPOTIPY_REDIRECT_URI=http://127.0.0.1:8888/callback
```

If any of these is missing the tool prints exactly which one and where to set it --
it never shows a traceback for a configuration problem.

### 3. Install

```
pip install -e ".[dev]"
```

## Commands

```
spotify-manager dedupe [--refresh] [--no-browser] [--port PORT] [--clear-decisions] [--verbose]
spotify-manager like-album-tracks [--refresh] [--yes] [--dry-run] [--rate PER_SECOND] [--verbose]
spotify-manager restore RESTORE_FILE [--verbose]
spotify-manager unlike-tracks RUN_RECORD [--rate PER_SECOND] [--verbose]
spotify-manager redact-snapshot [--source PATH] [--output PATH]
```

### `dedupe`

Fetches the saved-album library and prints the dedupe proposal: how many albums were
scanned, how many were excluded and why, then every duplicate group with its members,
the proposed keeper, the normalized key that grouped them, and the decorations that
had to be ignored for the members to match. Nothing is deleted.

Grouping is deliberately timid. Two albums are the same record only when their
normalized titles, their primary artist identifiers and their release types all
match, and a title is only normalized by removing decorations on a closed list --
Deluxe, Super Deluxe, Expanded, Complete, Anniversary, Remaster(ed), Special, Bonus
Track, featured-artist clauses, and a trailing year. Anything else, from `(Live)` to
`(Vol. 2)` to `(Explicit Version)`, keeps two albums apart. The cost of that bias is
the occasional duplicate left in place; the cost of the opposite bias is an album
deleted that cannot be recovered.

The first run opens a browser for the Spotify login. The token is then cached, so
later runs never prompt again -- it refreshes itself silently.

The library is written to a local JSON snapshot that stays usable for six hours. A
second run inside that window reads the snapshot and makes no API requests at all;
after six hours the snapshot expires on its own and is refetched.

* `--refresh` bypasses the snapshot and fetches from Spotify.
* `--verbose` prints every API request to stderr.

### `like-album-tracks`

Likes every song on every saved album that is not already in Liked Songs.

Track lists come from the saved-album snapshot the tool already holds -- the listing
endpoint embeds each album's tracks -- so working out what to like costs no requests
at all. Only an album with more tracks than that listing carries is fetched
individually; on a 1,281-album library that is two albums.

The same recording saved on two editions of one record is liked **once**. A standard
edition and a deluxe, an original and its remaster, an album and a compilation that
reprints one of its songs -- each carries its own track id for what is audibly the
same performance, and Spotify will happily add both. On a real library that is around
1,237 duplicated songs. The surviving copy comes from the richer edition, using the
same edition ranking `dedupe` uses, and never from a compilation when a real album has
the song. Matching is on title, artists and duration to the nearest second, which is a
heuristic; see `docs/adr/0001-collapse-same-recording-by-name-artist-duration.md` for
what that trades away.

The plan is printed as counts -- albums scanned, tracks found, already liked, collapsed
as duplicates, unlikeable -- and nothing is written until you type `yes`. If stdin is
not a terminal, the command refuses rather than proceeding or hanging; pass `--yes` to
approve it in a script.

Before the first like is issued, a run record naming exactly the tracks the run set out
to like is written to `.spotifymanager/likes/` and fsynced to disk. If it cannot be
written, nothing is liked -- ten thousand likes you cannot identify are worse than a
run that did not happen. Likes then go out in batches of fifty, oldest saved album
first, and every batch is reported as succeeded, failed, or never attempted.

An interrupted run is finished by running the command again: it likes only what is
still missing.

* `--refresh` bypasses both snapshots and fetches from Spotify.
* `--yes` skips the confirmation. Required when stdin is not a terminal.
* `--dry-run` prints the plan and exits, writing nothing and liking nothing.
* `--rate PER_SECOND` lowers the client-side request ceiling for this run.
* `--verbose` prints every API request to stderr.

Note that every track a run likes is stamped by Spotify with the moment of the run, so
a first full run puts one large block at the top of Liked Songs. Its internal order --
oldest saved album first -- is the only ordering it will ever have.

### `restore`

Re-saves every album listed in a restore file written by `dedupe`. The file is fully
parsed and validated before a single request goes out, so a missing or malformed
restore file produces a clear message and restores nothing. Re-saving an album that is
already saved is harmless.

### `unlike-tracks`

Removes the like from every track listed in a run record written by
`like-album-tracks`. That record is the only thing that tells the tracks one run liked
apart from likes made by hand years ago, so this command takes a path and never a
guess. The file is fully parsed and validated before a single request goes out, so a
missing or malformed run record produces a clear message and unlikes nothing.

```
spotify-manager unlike-tracks .spotifymanager/likes/liked-2026-09-05T14-30-00.json
```

The record names what the run *set out* to like, which may be more than it managed to
like; that is harmless, because removing a like that is not there is a no-op on
Spotify's side. For the same reason a run that did not finish can simply be re-run
with the same record. The file is read, not consumed: re-running `like-album-tracks`
is how *this* run is undone in turn.

What it cannot undo is chronology. Liked Songs is ordered by when each track was
liked, and un-liking then re-liking a track stamps it with the moment of the re-like.
The original dates are gone either way.

### `redact-snapshot`

Writes a redacted copy of the cached snapshot to
`tests/fixtures/library_snapshot.redacted.json` for use as committed test fixture
data, so that the planner's tests are seeded from a real library rather than an
imagined one.

```
spotify-manager dedupe            # fetch and cache the real library first
spotify-manager redact-snapshot   # then write the fixture
```

It keeps everything grouping and ranking depend on -- album and artist identifiers,
album and artist names, release type, release date and precision, track count, cover
art URLs. It strips per-account detail: `added_at` is reduced to a date, and market,
`href`, `uri`, `external_urls` and `external_ids` fields are dropped, along with any
key not on the allowlist. The exact rules are documented in
`src/spotify_manager/tools/redact.py`.

## Local state

All runtime state lives in `.spotifymanager/` at the repository root, which is
gitignored:

* `token_cache.json` -- the OAuth token and refresh token. Delete it to force a fresh
  login.
* `library_snapshot.json` -- the cached saved-album listing.
* `liked_tracks_snapshot.json` -- the cached Liked Songs listing. `like-album-tracks`
  and `unlike-tracks` delete it when a run ends, so the next run refetches and does
  exactly the work that is still outstanding.
* `restores/` -- one file per `dedupe` run that deleted something, listing the albums
  it set out to remove. `spotify-manager restore <file>` undoes that run.
* `likes/` -- one file per `like-album-tracks` run, listing the tracks it set out to
  like. `spotify-manager unlike-tracks <file>` undoes that run. Deliberately not the
  same directory as `restores/`: one holds album ids and one holds track ids, and they
  are undone by different commands.
* `reports/` -- the archived HTML report of each `dedupe` run.

## Rate limiting

Every request goes through one shared session that spaces requests to stay under
Spotify's throttling threshold. When Spotify does throttle (HTTP 429) the session
waits exactly as long as the response's `Retry-After` header instructs, rather than
guessing. Transient server errors are retried with exponentially increasing delays.

The library listing carries everything the deduplication logic needs, so no
per-album request is ever made: a full run costs one request per fifty albums.

`like-album-tracks` is the most request-hungry command, and it is still small: at
worst about 320 requests on a 1,281-album library -- 26 to fetch the albums, one per
fifty liked songs, and up to 215 to like around 9,500 tracks. Fifty ids per request is
the API maximum, so that write count is a floor rather than a tuning knob. `--rate`
lowers the ceiling if your app's quota turns out to be tighter than the default
assumes.

## Development

```
pytest
```

Tests cover the pure logic only -- cache expiry, retry timing, and fixture
redaction. The Spotify API is never mocked; authentication, the HTTP session and the
cache are verified by running the tool read-only against a real account.
