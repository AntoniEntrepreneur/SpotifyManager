"""The dedupe plan as a single self-contained HTML document.

`render_plan_html` is a pure function: a `DedupePlan` in, an HTML string out. It
opens no file, makes no request and reads no clock, so re-rendering after retuning
the ranking table costs nothing -- which is the whole point of caching the library.
The caller archives the string and opens a browser; this module never does.

"Self-contained" here means the document's own logic and styling: every rule of CSS
and every line of JavaScript is inline, there is no `<link>`, no `<script src>`, no
web font and no CDN. Cover art is the one deliberate exception -- the `<img>` tags
point at the Spotify CDN URLs the library listing already handed us, which costs no
Spotify API request and keeps the file small enough to archive on every run. The
footer says so, so a reader years later knows why the art may be missing.

The document is read-only by construction: nothing here submits anything, and the
only interactivity is client-side search and sorting. Per-album approval controls
arrive in later work and hang off the ids and `data-` attributes written here.
"""

from __future__ import annotations

from html import escape

from ..dedupe.models import AlbumJudgement, DedupePlan, DuplicateGroup, SavedAlbum

#: Preferred cover-art edge length. The listing offers roughly 640/300/64px; 300 is
#: the one that looks right at card size without downloading the largest asset.
PREFERRED_COVER_WIDTH = 300

#: Category identifiers are storage values; these are what a human should read.
CATEGORY_LABELS: dict[str, str] = {
    "super_deluxe": "Super Deluxe",
    "complete": "Complete",
    "anniversary": "Anniversary",
    "deluxe": "Deluxe",
    "expanded": "Expanded",
    "remastered": "Remastered",
    "special": "Special",
    "bonus_track": "Bonus Track",
    "plain": "Plain",
}

_STYLE = """
:root {
  --bg: #0c0c0f;
  --surface: #15151a;
  --surface-2: #1c1c23;
  --line: #2a2a33;
  --text: #ececf1;
  --muted: #9a9aa8;
  --faint: #6d6d7b;
  --keep: #1db954;
  --remove: #e0555f;
  --radius: 14px;
}
* { box-sizing: border-box; }
html { color-scheme: dark; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica,
    Arial, sans-serif;
  font-size: 15px;
  line-height: 1.5;
  -webkit-font-smoothing: antialiased;
}
.wrap { max-width: 1180px; margin: 0 auto; padding: 0 24px 72px; }
code, .mono {
  font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
}

/* ---------- header ---------- */
header { padding: 48px 0 28px; }
h1 { font-size: 28px; letter-spacing: -0.02em; margin: 0 0 6px; font-weight: 650; }
.subtitle { color: var(--muted); margin: 0 0 28px; font-size: 14px; }
.stats {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
  gap: 12px;
}
.stat {
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  padding: 16px 18px;
}
.stat .n { font-size: 30px; font-weight: 650; letter-spacing: -0.02em; display: block; }
.stat .l {
  color: var(--muted);
  font-size: 12px;
  text-transform: uppercase;
  letter-spacing: 0.07em;
}
.stat.warn .n { color: var(--remove); }

/* ---------- controls ---------- */
.controls {
  position: sticky;
  top: 0;
  z-index: 10;
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  align-items: center;
  padding: 14px 0;
  margin: 28px 0 8px;
  background: linear-gradient(var(--bg) 78%, rgba(12, 12, 15, 0));
}
input[type="search"], select {
  background: var(--surface-2);
  border: 1px solid var(--line);
  color: var(--text);
  border-radius: 10px;
  padding: 10px 12px;
  font: inherit;
  font-size: 14px;
}
input[type="search"] { flex: 1 1 260px; min-width: 200px; }
input[type="search"]::placeholder { color: var(--faint); }
input[type="search"]:focus, select:focus {
  outline: 2px solid var(--keep);
  outline-offset: 1px;
}
select { cursor: pointer; }
.count { color: var(--muted); font-size: 13px; margin-left: auto; }

/* ---------- group cards ---------- */
.group {
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  padding: 18px 18px 20px;
  margin: 14px 0;
}
.group-head {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 12px;
  align-items: baseline;
  margin-bottom: 4px;
}
.group-head h2 { font-size: 17px; margin: 0; font-weight: 600; letter-spacing: -0.01em; }
.group-head .artist { color: var(--muted); font-size: 14px; }
.group-head .size {
  margin-left: auto;
  color: var(--faint);
  font-size: 12px;
  text-transform: uppercase;
  letter-spacing: 0.07em;
}
.meta { color: var(--faint); font-size: 12.5px; margin: 8px 0 14px; }
.meta .k { color: var(--muted); }
.key {
  background: var(--surface-2);
  border: 1px solid var(--line);
  border-radius: 6px;
  padding: 1px 6px;
  color: #c9c9d4;
}
.chips { display: inline-flex; flex-wrap: wrap; gap: 6px; vertical-align: middle; }
.chip {
  background: rgba(29, 185, 84, 0.11);
  border: 1px solid rgba(29, 185, 84, 0.35);
  color: #7ee2a4;
  border-radius: 999px;
  padding: 1px 9px;
  font-size: 11.5px;
  white-space: nowrap;
}
.chip.none { background: transparent; border-color: var(--line); color: var(--faint); }

.albums {
  display: flex;
  flex-wrap: wrap;
  gap: 14px;
}
.album {
  flex: 1 1 190px;
  max-width: 240px;
  background: var(--surface-2);
  border: 1px solid var(--line);
  border-radius: 12px;
  padding: 12px;
  display: flex;
  flex-direction: column;
  transition: border-color 0.12s ease;
  gap: 8px;
  position: relative;
}
.album.keeper {
  border-color: rgba(29, 185, 84, 0.6);
  background: linear-gradient(180deg, rgba(29, 185, 84, 0.09), var(--surface-2) 60%);
  box-shadow: 0 0 0 1px rgba(29, 185, 84, 0.25);
}
.album.removal { opacity: 0.82; }
.album.removal:hover { opacity: 1; }
.cover {
  width: 100%;
  aspect-ratio: 1 / 1;
  border-radius: 8px;
  object-fit: cover;
  background: #101014;
  border: 1px solid var(--line);
  display: block;
}
.album.removal .cover { filter: grayscale(0.45) brightness(0.86); }
.badge {
  align-self: flex-start;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.09em;
  text-transform: uppercase;
  border-radius: 999px;
  padding: 2px 9px;
}
.badge.keep { background: var(--keep); color: #06210f; }
.badge.remove {
  background: rgba(224, 85, 95, 0.14);
  color: var(--remove);
  border: 1px solid rgba(224, 85, 95, 0.4);
}
.album .title { font-weight: 600; font-size: 14px; line-height: 1.35; }
.album .artist { color: var(--muted); font-size: 12.5px; }
.facts { color: var(--faint); font-size: 12px; display: flex; flex-wrap: wrap; gap: 6px; }
.facts span::after { content: "\\00b7"; margin-left: 6px; color: var(--line); }
.facts span:last-child::after { content: ""; margin: 0; }
.rank {
  font-size: 11.5px;
  color: #c9c9d4;
  background: rgba(255, 255, 255, 0.05);
  border: 1px solid var(--line);
  border-radius: 6px;
  padding: 1px 7px;
  align-self: flex-start;
}
.album.keeper .rank { color: #9fe8bb; border-color: rgba(29, 185, 84, 0.35); }
.album .dec { font-size: 11.5px; color: var(--faint); }
.id { font-size: 10.5px; color: #4d4d59; word-break: break-all; }

/* ---------- footer & empty ---------- */
.empty, .no-results {
  border: 1px dashed var(--line);
  border-radius: var(--radius);
  padding: 42px;
  text-align: center;
  color: var(--muted);
}
footer {
  margin-top: 40px;
  padding-top: 20px;
  border-top: 1px solid var(--line);
  color: var(--muted);
  font-size: 13px;
}
footer p { margin: 6px 0; }
footer .note { color: var(--faint); font-size: 12px; }
"""

_SCRIPT = """
(function () {
  var list = document.getElementById("groups");
  if (!list) return;
  var groups = Array.prototype.slice.call(list.querySelectorAll(".group"));
  var search = document.getElementById("search");
  var sort = document.getElementById("sort");
  var count = document.getElementById("visible-count");
  var empty = document.getElementById("no-results");
  var total = groups.length;

  function num(el, name) { return parseInt(el.dataset[name], 10) || 0; }

  var sorters = {
    "size-desc": function (a, b) {
      return num(b, "size") - num(a, "size") || cmp(a, b, "artist");
    },
    "size-asc": function (a, b) {
      return num(a, "size") - num(b, "size") || cmp(a, b, "artist");
    },
    "artist": function (a, b) { return cmp(a, b, "artist") || cmp(a, b, "title"); },
    "title": function (a, b) { return cmp(a, b, "title") || cmp(a, b, "artist"); },
    "year-desc": function (a, b) {
      return num(b, "year") - num(a, "year") || cmp(a, b, "artist");
    }
  };

  function cmp(a, b, name) {
    var x = a.dataset[name] || "", y = b.dataset[name] || "";
    return x < y ? -1 : x > y ? 1 : 0;
  }

  function apply() {
    var q = (search.value || "").trim().toLowerCase();
    var shown = 0;
    groups.forEach(function (g) {
      var hit = !q || g.dataset.search.indexOf(q) !== -1;
      g.hidden = !hit;
      if (hit) shown++;
    });
    var order = groups.slice().sort(sorters[sort.value] || sorters["size-desc"]);
    order.forEach(function (g) { list.appendChild(g); });
    count.textContent = shown === total
      ? total + " group" + (total === 1 ? "" : "s")
      : "Showing " + shown + " of " + total + " groups";
    empty.hidden = shown !== 0 || total === 0;
  }

  search.addEventListener("input", apply);
  sort.addEventListener("change", apply);
  apply();
})();
"""


def render_plan_html(plan: DedupePlan) -> str:
    """Render a dedupe plan as one self-contained HTML document.

    Pure: no filesystem, no network, no clock. Nothing about the returned document
    is interactive beyond client-side search and sorting -- it states a proposal.
    """
    parts: list[str] = [
        "<!doctype html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        "<title>Duplicate saved albums</title>",
        f"<style>{_STYLE}</style>",
        "</head>",
        "<body>",
        '<div class="wrap">',
        _header(plan),
        _controls(),
        _groups(plan),
        _footer(plan),
        "</div>",
        f"<script>{_SCRIPT}</script>",
        "</body>",
        "</html>",
    ]
    return "\n".join(parts) + "\n"


# --------------------------------------------------------------------------- header


def _header(plan: DedupePlan) -> str:
    stats = [
        ("albums scanned", plan.total_albums_scanned, ""),
        ("duplicate groups", len(plan.groups), ""),
        ("proposed for removal", plan.proposed_removal_count, "warn"),
        ("suppressed groups", plan.suppressed_group_count, ""),
    ]
    tiles = "\n".join(
        f'<div class="stat {css}"><span class="n">{value}</span>'
        f'<span class="l">{escape(label)}</span></div>'
        for label, value, css in stats
    )
    return (
        "<header>"
        "<h1>Duplicate saved albums</h1>"
        '<p class="subtitle">A proposal, not an action. Nothing has been deleted; '
        "the keeper in each group is the richest edition the ranking table found."
        "</p>"
        f'<div class="stats">{tiles}</div>'
        "</header>"
    )


def _controls() -> str:
    options = [
        ("size-desc", "Group size (largest first)"),
        ("size-asc", "Group size (smallest first)"),
        ("artist", "Artist A–Z"),
        ("title", "Album A–Z"),
        ("year-desc", "Release year (newest keeper)"),
    ]
    rendered = "".join(
        f'<option value="{escape(value)}">{escape(label)}</option>' for value, label in options
    )
    return (
        '<div class="controls">'
        '<input id="search" type="search" autocomplete="off" '
        'placeholder="Search albums, artists or normalized keys…" '
        'aria-label="Search albums">'
        f'<select id="sort" aria-label="Sort groups">{rendered}</select>'
        '<span class="count" id="visible-count"></span>'
        "</div>"
    )


# --------------------------------------------------------------------------- groups


def _groups(plan: DedupePlan) -> str:
    if not plan.groups:
        return (
            '<div class="empty"><strong>No duplicate editions found.</strong><br>'
            "Every saved album is the only edition of itself in this library.</div>"
        )
    cards = "\n".join(_group_card(index, group) for index, group in enumerate(plan.groups))
    return (
        f'<main id="groups">{cards}</main>'
        '<div class="no-results" id="no-results" hidden>No group matches that search.</div>'
    )


def _group_card(index: int, group: DuplicateGroup) -> str:
    keeper = group.keeper
    size = len(group.members)
    searchable = " ".join(
        [group.key.normalized_title, group.key.album_type]
        + [f"{m.album.name} {m.album.artist_names}" for m in group.members]
    ).lower()
    albums = "\n".join(_album_card(member) for member in group.members)
    return (
        f'<section class="group" id="group-{index}"'
        f' data-group-index="{index}"'
        f' data-size="{size}"'
        f' data-year="{_release_year(keeper.album) or 0}"'
        f' data-artist="{escape(keeper.album.artist_names.lower())}"'
        f' data-title="{escape(group.key.normalized_title)}"'
        f' data-key="{escape(group.key.normalized_title)}"'
        f' data-search="{escape(searchable)}">'
        '<div class="group-head">'
        f"<h2>{escape(keeper.album.name)}</h2>"
        f'<span class="artist">{escape(keeper.album.artist_names)}</span>'
        f'<span class="size">{size} {_plural(size, "edition")}</span>'
        "</div>"
        f'<div class="meta">{_group_meta(group)}</div>'
        f'<div class="albums">{albums}</div>'
        "</section>"
    )


def _group_meta(group: DuplicateGroup) -> str:
    key = escape(group.key.normalized_title) or "<em>(empty)</em>"
    bits = [
        f'<span class="k">matched on</span> <span class="key mono">{key}</span>',
        f'<span class="k">type</span> {escape(group.key.album_type or "unknown")}',
        f'<span class="k">ignored</span> {_chips(group.ignored_decorations)}',
    ]
    if group.suppressed_pair_count:
        count = group.suppressed_pair_count
        bits.append(
            f'<span class="k">{count} {_plural(count, "comparison", "comparisons")} '
            "already judged not duplicates</span>"
        )
    return " &nbsp;·&nbsp; ".join(bits)


def _chips(decorations: tuple[str, ...]) -> str:
    if not decorations:
        return '<span class="chip none">nothing</span>'
    return '<span class="chips">' + "".join(
        f'<span class="chip">{escape(text)}</span>' for text in decorations
    ) + "</span>"


def _album_card(member: AlbumJudgement) -> str:
    album = member.album
    badge = (
        '<span class="badge keep">keep</span>'
        if member.is_keeper
        else '<span class="badge remove">remove</span>'
    )
    cover = _cover(album)
    decorations = (
        f'<div class="dec">ignored here: '
        f'{escape(", ".join(member.ignored_decorations))}</div>'
        if member.ignored_decorations
        else ""
    )
    facts = (
        f'<span title="{escape(_release_detail(album))}">{escape(_release_display(album))}</span>'
        f"<span>{album.total_tracks} tracks</span>"
    )
    rank_label = CATEGORY_LABELS.get(member.edition_category, member.edition_category)
    return (
        f'<article class="album {"keeper" if member.is_keeper else "removal"}"'
        f' data-album-id="{escape(album.id)}"'
        f' data-keeper="{"true" if member.is_keeper else "false"}"'
        f' data-rank="{member.edition_rank}"'
        f' data-search="{escape(f"{album.name} {album.artist_names}".lower())}">'
        f"{cover}"
        f"{badge}"
        f'<div class="title">{escape(album.name)}</div>'
        f'<div class="artist">{escape(album.artist_names)}</div>'
        f'<div class="facts">{facts}</div>'
        f'<div class="rank" title="Edition rank from the ranking table">'
        f"{escape(rank_label)} · rank {member.edition_rank}</div>"
        f"{decorations}"
        f'<div class="id mono">{escape(album.id)}</div>'
        "</article>"
    )


def _cover(album: SavedAlbum) -> str:
    url = _cover_url(album)
    alt = escape(f"Cover art for {album.name}")
    if not url:
        return f'<div class="cover" role="img" aria-label="{alt}"></div>'
    return (
        f'<img class="cover" src="{escape(url)}" alt="{alt}" loading="lazy" '
        'referrerpolicy="no-referrer">'
    )


def _cover_url(album: SavedAlbum) -> str:
    """The cover closest to card size; the listing already gave us these URLs."""
    if not album.images:
        return ""
    best = min(
        album.images,
        key=lambda image: abs((image.width or image.height or 0) - PREFERRED_COVER_WIDTH),
    )
    return best.url or album.images[0].url


# --------------------------------------------------------------------------- release


def _release_year(album: SavedAlbum) -> int:
    year = album.release_sort_key[0]
    return year if year > 0 else 0


def _release_display(album: SavedAlbum) -> str:
    """The release year -- the one part of the date every precision agrees on."""
    year = _release_year(album)
    return str(year) if year else "year unknown"


def _release_detail(album: SavedAlbum) -> str:
    """The full date, said no more precisely than Spotify claims to know it."""
    if not album.release_date:
        return "Spotify gives no release date for this album"
    precision = album.release_date_precision or "unknown"
    return f"Released {album.release_date} ({precision} precision)"


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    return singular if count == 1 else (plural or singular + "s")


# --------------------------------------------------------------------------- footer


def _footer(plan: DedupePlan) -> str:
    if plan.excluded_reasons:
        reasons = ", ".join(
            f"{count} {escape(reason)}" for reason, count in sorted(plan.excluded_reasons.items())
        )
        excluded = (
            f"<strong>{plan.excluded_count}</strong> "
            f"{_plural(plan.excluded_count, 'album')} were excluded from analysis: "
            f"{reasons}."
        )
    else:
        excluded = "No albums were excluded from analysis."
    suppressed = (
        f"<strong>{plan.suppressed_group_count}</strong> "
        f"{_plural(plan.suppressed_group_count, 'group')} were suppressed by "
        "earlier &lsquo;not duplicates&rsquo; decisions."
        if plan.suppressed_group_count
        else "No groups were suppressed by earlier decisions."
    )
    return (
        "<footer>"
        f"<p>{excluded}</p>"
        f"<p>{suppressed}</p>"
        "<p>Nothing has been deleted. This report states a proposal only.</p>"
        '<p class="note">All styling and behaviour is embedded in this file; the only '
        "external references are the cover images, loaded from Spotify&rsquo;s CDN "
        "(i.scdn.co), which may not render offline or years from now.</p>"
        "</footer>"
    )
