# spotify-manager

A command-line tool for managing a personal Spotify library. The first feature under
construction is saved-album deduplication: finding albums saved twice under different
editions (a plain release alongside a Deluxe or Remastered one) and proposing a
cleanup that only ever runs after explicit approval.

**Current state:** the read-only walking skeleton. `dedupe` authenticates, downloads
the entire saved-album library, caches it locally, and prints what it found. Nothing
is analysed and nothing is modified yet.

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
spotify-manager dedupe [--refresh] [--verbose]
spotify-manager redact-snapshot [--source PATH] [--output PATH]
```

### `dedupe`

Fetches the saved-album library and prints a summary: total albums, pages fetched,
requests made, and elapsed time.

The first run opens a browser for the Spotify login. The token is then cached, so
later runs never prompt again -- it refreshes itself silently.

The library is written to a local JSON snapshot that stays usable for six hours. A
second run inside that window reads the snapshot and makes no API requests at all;
after six hours the snapshot expires on its own and is refetched.

* `--refresh` bypasses the snapshot and fetches from Spotify.
* `--verbose` prints every API request to stderr.

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

## Rate limiting

Every request goes through one shared session that spaces requests to stay under
Spotify's throttling threshold. When Spotify does throttle (HTTP 429) the session
waits exactly as long as the response's `Retry-After` header instructs, rather than
guessing. Transient server errors are retried with exponentially increasing delays.

The library listing carries everything the deduplication logic needs, so no
per-album request is ever made: a full run costs one request per fifty albums.

## Development

```
pytest
```

Tests cover the pure logic only -- cache expiry, retry timing, and fixture
redaction. The Spotify API is never mocked; authentication, the HTTP session and the
cache are verified by running the tool read-only against a real account.
