# ADR-0001: Collapse duplicate recordings by name, artist and duration, not by ISRC

Status: accepted (2026-09-05)
Context: `like-album-tracks` (issue #10)

## Context

A saved-album library holds the same recording more than once. A standard edition and
a deluxe edition of one record, an original release and its remaster, an album and the
compilation that reprints one of its songs -- each carries its own track ids for what
is audibly the same performance. Measured on the real 1,281-album library: 10,710
listed tracks, of which **1,237 are a second copy of a recording that appears
elsewhere in the same library**.

Liking all of them puts those 1,237 songs into Liked Songs twice, from two editions.
Spotify does not deduplicate them; they are distinct ids and it has no reason to.

Two ways to decide that two tracks are the same recording were available:

1. **ISRC.** The International Standard Recording Code identifies a *recording* rather
   than a release, which is precisely the question being asked. It is exact.
2. **Name, artists and duration.** All three are already present in the saved-albums
   listing this tool caches, so the question costs nothing to ask.

## Decision

Match recordings on `(track name, set of artist names, duration rounded to the nearest
second)`, all compared case-insensitively.

Duration tolerance is one second. Measured on the real library, exact-millisecond
matching finds 1,076 duplicates, ±1s finds 1,237, ±2s finds 1,260, and name-and-artist
alone finds 1,346. The 161 rows between exact and ±1s are remasters whose master
differs by a fraction of a second -- for the purpose of "do I want this song in Liked
Songs twice", a remaster is not a different recording. ±2s buys 23 more rows and starts
risking a collision with a genuine radio edit.

Of each matched group, one track id is liked. The winner is chosen by the existing
edition ranking in `dedupe/ranking.py` -- there is one idea in this codebase of what a
good edition is, and this is not a second one -- with one addition and one tiebreak:

* an album whose `album_type` is `compilation` ranks below every non-compilation, so a
  song is taken from the record it belongs to rather than from a greatest-hits. Without
  this, the ranking's "more tracks wins" tiebreaker hands a 30-track compilation the
  win over the 11-track album the song came from.
* ties break on album id and then track id, so the ordering is total and two runs of
  the same plan like the same ids.

If any copy of a recording is already in Liked Songs, no copy is liked: the user
already has that song.

## Consequences

**ISRC is not used, and the reason is cost.** `external_ids.isrc` is on the full track
object but not on the simplified track objects the album listing embeds. Using it would
mean `GET /tracks` in batches of 50 across the whole library -- about 215 extra requests
per run, roughly doubling it -- to decide a 13% slice more precisely.

**This is the only heuristic in the feature, and it is the only part that fails
quietly.** Everything else in `like-album-tracks` announces its failures: a failed batch
is named, an unlikeable track is counted, an incomplete album is listed. A wrong collapse
produces no message at all -- a song is simply never liked, and nothing says so. Two
distinct recordings that happen to share a title, an artist and a duration to the second
will be treated as one.

**The upgrade path is known and small.** If quiet misses turn out to matter more than
215 requests, fetch ISRCs for the tracks that collide under the current key -- only
those -- and use the ISRC where one is present, falling back to the heuristic where it
is not. That is strictly cheaper than fetching ISRCs for the whole library, and it keeps
this ADR's rule as the fallback rather than replacing it.
