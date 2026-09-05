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

The document has two modes, and the difference is one argument. Rendered with no
`approve_url` it stays a read-only record -- the form the archived copy of every run
takes, because an archive that could still submit something would be a lie. Rendered
with an `approve_url` it grows the approval controls: a keep checkbox on every album,
pre-checked to match the plan's own proposal, a skip switch on every group, and one
button that posts the decisions back to the waiting process. Submitting without
touching anything therefore reproduces the plan exactly.

That URL is a path, never an origin, so the page can only ever post back to whatever
served it -- which is the loopback approval server and nothing else.
"""

from __future__ import annotations

from html import escape

from ..dedupe.execute import ExecutionResult
from ..dedupe.models import AlbumJudgement, DedupePlan, DuplicateGroup, SavedAlbum
from ..dedupe.resolve import Resolution

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

#: Everything the approval controls add. Appended to `_STYLE` only when the report is
#: rendered interactive, so the archived copy carries no styling for controls it has
#: deliberately not got.
_APPROVAL_STYLE = """
/* ---------- approval controls ---------- */
.approving .wrap { padding-bottom: 168px; }
.approving .controls { top: 0; }

.skip {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  margin-left: auto;
  padding: 5px 11px 5px 8px;
  border: 1px solid var(--line);
  border-radius: 999px;
  background: var(--surface-2);
  color: var(--muted);
  font-size: 12px;
  cursor: pointer;
  user-select: none;
  white-space: nowrap;
}
.skip:hover { border-color: #46465a; color: var(--text); }
.skip input { accent-color: var(--remove); width: 15px; height: 15px; margin: 0; }
.group-head .size { margin-left: 0; }
.group.skipped { border-color: rgba(224, 85, 95, 0.45); }
.group.skipped .albums, .group.skipped .meta { opacity: 0.4; }
.group.skipped .skip { border-color: var(--remove); color: var(--remove); }
.group.skipped .skip-state::after { content: "skipped — no change"; }
.skip-state::after { content: "Skip this group"; }

.pick {
  position: absolute;
  top: 8px;
  right: 8px;
  z-index: 2;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 5px 10px 5px 7px;
  border-radius: 999px;
  border: 1px solid var(--line);
  background: rgba(10, 10, 13, 0.86);
  backdrop-filter: blur(3px);
  font-size: 11.5px;
  font-weight: 600;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  color: var(--muted);
  cursor: pointer;
  user-select: none;
}
.pick input { accent-color: var(--keep); width: 16px; height: 16px; margin: 0; }
.pick:hover { border-color: #46465a; }
.album.kept .pick { border-color: rgba(29, 185, 84, 0.6); color: #7ee2a4; }
.album.dropped .pick { border-color: rgba(224, 85, 95, 0.45); color: var(--remove); }
.pick-state::after { content: "remove"; }
.album.kept .pick-state::after { content: "keep"; }
/* A skipped group changes nothing, so its ticks must not claim otherwise. */
.group.skipped .pick-state::after { content: "no change"; }

/* While approving, the ring follows the checkbox rather than the original plan. */
.approving .album.keeper { box-shadow: none; }
.approving .album { opacity: 1; }
.approving .album.kept {
  border-color: rgba(29, 185, 84, 0.6);
  background: linear-gradient(180deg, rgba(29, 185, 84, 0.09), var(--surface-2) 60%);
}
.approving .album.dropped {
  border-color: rgba(224, 85, 95, 0.35);
  background: var(--surface-2);
}
.approving .album.dropped .cover { filter: grayscale(0.5) brightness(0.8); }
.approving .album.kept .cover { filter: none; }
.approving .group.skipped .album .pick { pointer-events: none; opacity: 0.5; }

.warn-all {
  display: none;
  margin: 10px 0 0;
  color: var(--remove);
  font-size: 12.5px;
}
.group.empty-keep:not(.skipped) .warn-all { display: block; }

.approve-bar {
  position: fixed;
  left: 0;
  right: 0;
  bottom: 0;
  z-index: 20;
  display: flex;
  flex-wrap: wrap;
  gap: 14px;
  align-items: center;
  padding: 14px 24px;
  background: rgba(18, 18, 23, 0.96);
  border-top: 1px solid var(--line);
  backdrop-filter: blur(8px);
}
.approve-bar .inner {
  max-width: 1180px;
  margin: 0 auto;
  width: 100%;
  display: flex;
  flex-wrap: wrap;
  gap: 14px;
  align-items: center;
}
.tally { font-size: 13.5px; color: var(--muted); }
.tally strong { color: var(--text); font-size: 16px; }
.tally .rm { color: var(--remove); }
.approve-bar .spacer { flex: 1 1 auto; }
button.approve {
  font: inherit;
  font-size: 14.5px;
  font-weight: 650;
  border: 0;
  border-radius: 10px;
  padding: 12px 22px;
  background: var(--keep);
  color: #06210f;
  cursor: pointer;
}
button.approve:hover { filter: brightness(1.08); }
button.approve:disabled { background: var(--line); color: var(--faint); cursor: default; }
button.reset {
  font: inherit;
  font-size: 13px;
  background: transparent;
  border: 1px solid var(--line);
  color: var(--muted);
  border-radius: 10px;
  padding: 10px 14px;
  cursor: pointer;
}
button.reset:hover { color: var(--text); border-color: #46465a; }
.bar-note { flex-basis: 100%; color: var(--faint); font-size: 12px; margin: 0; }
.bar-note.error { color: var(--remove); }

.done {
  position: fixed;
  inset: 0;
  z-index: 50;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(8, 8, 11, 0.94);
  padding: 24px;
  text-align: center;
}
.done .panel {
  max-width: 520px;
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  padding: 34px;
}
.done h2 { margin: 0 0 10px; font-size: 20px; }
.done p { color: var(--muted); margin: 8px 0 0; font-size: 14px; }
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

#: The approval behaviour. `__APPROVE_URL__` is substituted with the path the page
#: posts to; it is a path and not an origin, so the page can only ever answer the
#: server that served it.
_APPROVAL_SCRIPT = """
(function () {
  var APPROVE_URL = "__APPROVE_URL__";
  // An empty plan has no #groups element at all (see _groups()) -- there is
  // nothing to list, sort or tick. But its approve bar still renders an
  // "Approve — remove nothing" button (see _approve_bar()), and that button must
  // still submit, or the run hangs forever waiting for a POST that can never
  // come. So `list` is optional here; only `bar` (and the button it contains)
  // is required for this script to do its job.
  var list = document.getElementById("groups");
  var bar = document.getElementById("approve-bar");
  if (!bar) return;

  var groups = list ? Array.prototype.slice.call(list.querySelectorAll(".group")) : [];
  var button = document.getElementById("approve");
  var reset = document.getElementById("reset");
  var tally = document.getElementById("tally");
  var note = document.getElementById("bar-note");
  var defaultNote = note.textContent;

  function boxes(group) {
    return Array.prototype.slice.call(group.querySelectorAll("input.keep-box"));
  }
  function skipBox(group) { return group.querySelector("input.skip-box"); }

  function update() {
    var removing = 0, skipped = 0, wipes = 0;
    groups.forEach(function (group) {
      var skip = skipBox(group).checked;
      group.classList.toggle("skipped", skip);
      var kept = 0;
      boxes(group).forEach(function (box) {
        var card = box.closest(".album");
        var keep = skip || box.checked;
        card.classList.toggle("kept", keep);
        card.classList.toggle("dropped", !keep);
        if (box.checked) kept++;
        if (!skip && !box.checked) removing++;
      });
      if (skip) skipped++;
      var wipe = !skip && kept === 0;
      group.classList.toggle("empty-keep", wipe);
      if (wipe) wipes++;
    });
    tally.innerHTML =
      "<strong class=\\"rm\\">" + removing + "</strong> album" +
      (removing === 1 ? "" : "s") + " will be removed &nbsp;·&nbsp; " +
      skipped + " group" + (skipped === 1 ? "" : "s") + " skipped";
    button.textContent = removing
      ? "Approve — remove " + removing + " album" + (removing === 1 ? "" : "s")
      : "Approve — remove nothing";
    button.dataset.removing = removing;
    button.dataset.wipes = wipes;
  }

  function payload() {
    var out = {};
    groups.forEach(function (group) {
      var index = group.dataset.groupIndex;
      if (skipBox(group).checked) {
        out[index] = { action: "skip", keep: [] };
        return;
      }
      var keep = [];
      boxes(group).forEach(function (box) {
        if (box.checked) keep.push(box.dataset.albumId);
      });
      out[index] = { action: "resolve", keep: keep };
    });
    return { groups: out };
  }

  function fail(message) {
    var veil = document.getElementById("working");
    if (veil) veil.remove();
    note.textContent = message;
    note.classList.add("error");
    button.disabled = false;
  }

  function working() {
    var panel = document.createElement("div");
    panel.className = "done";
    panel.id = "working";
    panel.innerHTML =
      "<div class=\\"panel\\"><h2>Applying your decisions…</h2>" +
      "<p>The restore file is written before anything is removed. This page " +
      "becomes the results view as soon as the run finishes.</p></div>";
    document.body.appendChild(panel);
  }

  function done(body) {
    // The run applies the decisions while this request is in flight, so by the time
    // we are here the results view already exists. Go to it: the reviewer submitted
    // from this tab and the outcome belongs in this tab.
    if (body && body.results_url) {
      window.location.href = body.results_url;
      return;
    }
    var panel = document.createElement("div");
    panel.className = "done";
    panel.innerHTML =
      "<div class=\\"panel\\"><h2>Decisions submitted</h2>" +
      "<p>You can close this tab. The result is printed in the terminal you " +
      "started the run from.</p></div>";
    document.body.appendChild(panel);
  }

  function submit() {
    if (Number(button.dataset.wipes) > 0 &&
        !window.confirm("Some groups have no album ticked to keep, so every " +
                        "edition in them would be removed. Submit anyway?")) {
      return;
    }
    button.disabled = true;
    note.classList.remove("error");
    note.textContent =
      "Approved. Removing the albums now — a restore file is written first. " +
      "Leave this tab open; the results appear here when it finishes.";
    working();
    fetch(APPROVE_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload())
    }).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (body) {
        if (!response.ok) throw new Error(body.error || ("HTTP " + response.status));
        done(body);
      });
    }).catch(function (error) {
      fail("Could not submit: " + error.message + ". The run is still waiting; " +
           "reload this page and try again.");
      note.textContent += " (" + defaultNote + ")";
    });
  }

  if (list) {
    list.addEventListener("change", function (event) {
      if (event.target.matches("input.keep-box, input.skip-box")) update();
    });
  }
  button.addEventListener("click", submit);
  reset.addEventListener("click", function () {
    groups.forEach(function (group) {
      skipBox(group).checked = false;
      boxes(group).forEach(function (box) {
        box.checked = box.dataset.proposedKeep === "true";
      });
    });
    update();
  });
  update();
})();
"""


def render_plan_html(plan: DedupePlan, *, approve_url: str | None = None) -> str:
    """Render a dedupe plan as one self-contained HTML document.

    Pure: no filesystem, no network, no clock.

    Args:
        plan: what to render.
        approve_url: the path the approval controls post decisions to. Omit it (the
            default) and the document is a read-only record with no way to submit
            anything -- which is what every archived copy must be. Give it a path,
            and the report grows per-album keep checkboxes pre-checked to match the
            plan, a per-group skip switch, and a single approve button.
    """
    interactive = approve_url is not None
    style = _STYLE + (_APPROVAL_STYLE if interactive else "")
    script = _SCRIPT
    if interactive:
        script += _APPROVAL_SCRIPT.replace("__APPROVE_URL__", escape(approve_url or "", quote=True))
    parts: list[str] = [
        "<!doctype html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        "<title>Duplicate saved albums</title>",
        f"<style>{style}</style>",
        "</head>",
        f'<body class="{"approving" if interactive else "reporting"}">',
        '<div class="wrap">',
        _header(plan, interactive),
        _controls(),
        _groups(plan, interactive),
        _footer(plan, interactive),
        "</div>",
        _approve_bar(plan) if interactive else "",
        f"<script>{script}</script>",
        "</body>",
        "</html>",
    ]
    return "\n".join(part for part in parts if part) + "\n"


# --------------------------------------------------------------------------- header


def _header(plan: DedupePlan, interactive: bool = False) -> str:
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
    subtitle = (
        "Every album is ticked as the plan proposes: the richest edition in each "
        "group is kept and the rest are removed. Change any tick, skip any group "
        "you disagree with, then approve at the bottom of the page."
        if interactive
        else "A proposal, not an action. Nothing has been deleted; the keeper in "
        "each group is the richest edition the ranking table found."
    )
    return (
        "<header>"
        "<h1>Duplicate saved albums</h1>"
        f'<p class="subtitle">{subtitle}</p>'
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


def _groups(plan: DedupePlan, interactive: bool = False) -> str:
    if not plan.groups:
        return (
            '<div class="empty"><strong>No duplicate editions found.</strong><br>'
            "Every saved album is the only edition of itself in this library.</div>"
        )
    cards = "\n".join(
        _group_card(index, group, interactive) for index, group in enumerate(plan.groups)
    )
    return (
        f'<main id="groups">{cards}</main>'
        '<div class="no-results" id="no-results" hidden>No group matches that search.</div>'
    )


def _group_card(index: int, group: DuplicateGroup, interactive: bool = False) -> str:
    keeper = group.keeper
    size = len(group.members)
    searchable = " ".join(
        [group.key.normalized_title, group.key.album_type]
        + [f"{m.album.name} {m.album.artist_names}" for m in group.members]
    ).lower()
    albums = "\n".join(_album_card(member, interactive) for member in group.members)
    skip = (
        f'<label class="skip" title="Skip this group entirely: nothing in it changes">'
        f'<input type="checkbox" class="skip-box" data-group-index="{index}">'
        f'<span class="skip-state"></span></label>'
        if interactive
        else ""
    )
    warning = (
        '<p class="warn-all">Nothing is ticked to keep in this group, so every '
        "edition of it would be removed.</p>"
        if interactive
        else ""
    )
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
        f"{skip}"
        "</div>"
        f'<div class="meta">{_group_meta(group)}</div>'
        f'<div class="albums">{albums}</div>'
        f"{warning}"
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


def _album_card(member: AlbumJudgement, interactive: bool = False) -> str:
    album = member.album
    # When the reviewer can change the outcome, the badge stops claiming what will
    # happen and states what the *plan* proposed; the checkbox says what will happen.
    keep_label, remove_label = ("proposed keeper", "duplicate") if interactive else ("keep", "remove")
    badge = (
        f'<span class="badge keep">{keep_label}</span>'
        if member.is_keeper
        else f'<span class="badge remove">{remove_label}</span>'
    )
    pick = (
        f'<label class="pick" title="Tick to keep this album">'
        f'<input type="checkbox" class="keep-box" data-album-id="{escape(album.id)}" '
        f'data-proposed-keep="{"true" if member.is_keeper else "false"}"'
        f'{" checked" if member.is_keeper else ""}>'
        f'<span class="pick-state"></span></label>'
        if interactive
        else ""
    )
    state = (" kept" if member.is_keeper else " dropped") if interactive else ""
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
        f'<article class="album {"keeper" if member.is_keeper else "removal"}{state}"'
        f' data-album-id="{escape(album.id)}"'
        f' data-keeper="{"true" if member.is_keeper else "false"}"'
        f' data-rank="{member.edition_rank}"'
        f' data-search="{escape(f"{album.name} {album.artist_names}".lower())}">'
        f"{pick}"
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


def _approve_bar(plan: DedupePlan) -> str:
    """The single action that approves the whole reviewed plan.

    Fixed to the bottom of the window rather than the end of the document: with
    dozens of groups, a button at the end of the page is a button nobody finds.
    """
    if not plan.groups:
        return (
            '<div class="approve-bar" id="approve-bar"><div class="inner">'
            '<span class="tally" id="tally">Nothing to review.</span>'
            '<span class="spacer"></span>'
            '<button class="reset" id="reset" type="button" hidden>Reset</button>'
            '<button class="approve" id="approve" type="button">Approve — remove nothing</button>'
            '<p class="bar-note" id="bar-note">Nothing has been deleted yet.</p>'
            "</div></div>"
        )
    return (
        '<div class="approve-bar" id="approve-bar"><div class="inner">'
        '<span class="tally" id="tally"></span>'
        '<span class="spacer"></span>'
        '<button class="reset" id="reset" type="button">Reset to the proposal</button>'
        '<button class="approve" id="approve" type="button"></button>'
        '<p class="bar-note" id="bar-note">Nothing has been deleted yet. Closing this '
        "tab does not cancel the run — the terminal keeps waiting until you approve, "
        "or until you interrupt it there.</p>"
        "</div></div>"
    )


def _footer(plan: DedupePlan, interactive: bool = False) -> str:
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
        + (
            "<p>Nothing has been deleted yet. Your decisions are submitted from the "
            "bar at the bottom of this page.</p>"
            if interactive
            else "<p>Nothing has been deleted. This report states a proposal only.</p>"
        )
        +
        '<p class="note">All styling and behaviour is embedded in this file; the only '
        "external references are the cover images, loaded from Spotify&rsquo;s CDN "
        "(i.scdn.co), which may not render offline or years from now.</p>"
        "</footer>"
    )


# --------------------------------------------------------------------------- results

#: Styling for the results view only. Appended to `_STYLE`, so the results page is the
#: same document in the same skin -- the reviewer submitted from this page and lands
#: back on it, and it should look like the place they were already standing.
_RESULTS_STYLE = """
/* ---------- results ---------- */
.banner {
  border-radius: var(--radius);
  padding: 22px 24px;
  margin: 28px 0 4px;
  border: 1px solid var(--line);
  background: var(--surface);
  border-left: 6px solid var(--muted);
}
.banner h2 { margin: 0 0 6px; font-size: 20px; letter-spacing: -0.01em; }
.banner p { margin: 6px 0 0; color: var(--muted); font-size: 14px; }
.banner.ok { border-left-color: var(--keep); background: rgba(29, 185, 84, 0.07); }
.banner.ok h2 { color: #7ee2a4; }
.banner.bad {
  border: 1px solid rgba(224, 85, 95, 0.55);
  border-left: 6px solid var(--remove);
  background: rgba(224, 85, 95, 0.1);
}
.banner.bad h2 { color: var(--remove); font-size: 23px; }
.banner.bad strong { color: var(--remove); }
.banner.none { border-left-color: var(--faint); }

.stat.bad .n { color: var(--remove); }
.stat.bad { border-color: rgba(224, 85, 95, 0.5); background: rgba(224, 85, 95, 0.07); }
.stat.good .n { color: #7ee2a4; }

.panel {
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  padding: 20px 22px;
  margin: 16px 0;
}
.panel.alarm { border-color: rgba(224, 85, 95, 0.55); background: rgba(224, 85, 95, 0.06); }
.panel > h3 {
  margin: 0 0 4px;
  font-size: 15px;
  letter-spacing: 0.02em;
}
.panel.alarm > h3 { color: var(--remove); }
.panel > p { margin: 6px 0 0; color: var(--muted); font-size: 13.5px; }
pre.cmd {
  margin: 12px 0 0;
  padding: 12px 14px;
  background: #0a0a0d;
  border: 1px solid var(--line);
  border-radius: 10px;
  color: #d7d7e2;
  font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
  font-size: 13px;
  overflow-x: auto;
  white-space: pre;
}
.path { color: #c9c9d4; word-break: break-all; }

table.albums-table { width: 100%; border-collapse: collapse; margin-top: 12px; font-size: 13px; }
table.albums-table th {
  text-align: left;
  color: var(--faint);
  font-weight: 600;
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.07em;
  padding: 0 10px 8px 0;
  border-bottom: 1px solid var(--line);
}
table.albums-table td {
  padding: 8px 10px 8px 0;
  border-bottom: 1px solid rgba(42, 42, 51, 0.55);
  vertical-align: top;
}
table.albums-table td.name { color: var(--text); }
table.albums-table td.artist { color: var(--muted); }
table.albums-table td.idcell {
  color: #4d4d59;
  font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
  font-size: 11px;
}
.batch {
  border: 1px solid var(--line);
  border-radius: 10px;
  padding: 12px 14px;
  margin: 10px 0 0;
  background: var(--surface-2);
}
.batch.failed { border-color: rgba(224, 85, 95, 0.5); }
.batch .head { font-weight: 600; font-size: 13.5px; }
.batch .err {
  margin: 6px 0 0;
  color: var(--remove);
  font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
  font-size: 12px;
  word-break: break-word;
}
.tag {
  display: inline-block;
  border-radius: 999px;
  padding: 1px 9px;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  margin-right: 8px;
}
.tag.succeeded { background: var(--keep); color: #06210f; }
.tag.failed { background: var(--remove); color: #2a0508; }
.tag.never_attempted { background: var(--line); color: var(--text); }
details.roll > summary {
  cursor: pointer;
  color: var(--muted);
  font-size: 13.5px;
  padding: 4px 0;
}
details.roll > summary:hover { color: var(--text); }
"""


def render_results_html(
    result: ExecutionResult,
    resolution: Resolution,
    *,
    restore_command: str,
) -> str:
    """Render what a run actually did, as one self-contained HTML document.

    Pure: no filesystem, no network, no clock. Everything it states comes from the
    `ExecutionResult` it is handed, so there is no path by which this page can be
    more optimistic than the run was.

    The page leads with a banner that states the outcome in the first line, and a
    partial failure turns the whole page red rather than hiding behind a count in a
    tile. A failure the reader has to look for is a failure that gets missed, and a
    missed failure here means believing an album is gone when it is not.
    """
    parts: list[str] = [
        "<!doctype html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{escape(_results_title(result))}</title>",
        f"<style>{_STYLE + _RESULTS_STYLE}</style>",
        "</head>",
        '<body class="reporting results">',
        '<div class="wrap">',
        _results_header(result),
        _results_restore(result, restore_command),
        _results_failures(result, resolution),
        _results_never_attempted(result, resolution),
        _results_removed(result, resolution),
        _results_batches(result),
        _results_footer(result, resolution),
        "</div>",
        "</body>",
        "</html>",
    ]
    return "\n".join(part for part in parts if part) + "\n"


def _results_title(result: ExecutionResult) -> str:
    """The tab title says the outcome, because a tab is sometimes all you can see."""
    if result.nothing_requested:
        return "Nothing removed — dedupe results"
    if result.is_clean:
        return f"{result.removed_count} albums removed — dedupe results"
    unfinished = result.failed_count + result.never_attempted_count
    return f"INCOMPLETE — {unfinished} album(s) not removed"


def _results_header(result: ExecutionResult) -> str:
    if result.nothing_requested:
        banner = (
            '<div class="banner none"><h2>Nothing was removed.</h2>'
            "<p>You approved no removals, so no restore file was written and no "
            "request was sent to Spotify. Your library is exactly as it was.</p></div>"
        )
    elif result.is_clean:
        banner = (
            f'<div class="banner ok"><h2>All {result.removed_count} approved '
            f"{_plural(result.removed_count, 'album')} removed.</h2>"
            "<p>Every batch was accepted by Spotify. Nothing failed and nothing was "
            "left unattempted. It is still undoable — see below.</p></div>"
        )
    else:
        banner = (
            '<div class="banner bad"><h2>This run did not finish. '
            "Your library is not in the state the plan described.</h2>"
            f"<p>Of the <strong>{result.requested_count}</strong> "
            f"{_plural(result.requested_count, 'album')} you approved for removal, "
            f"{_results_breakdown(result)}. The lists are below, in full.</p>"
            + (
                f"<p>The run stopped early: {escape(result.interrupted)}.</p>"
                if result.interrupted
                else ""
            )
            + "</div>"
        )

    tiles = [
        ("approved for removal", result.requested_count, ""),
        ("removed", result.removed_count, "good" if result.removed_count else ""),
        ("failed", result.failed_count, "bad" if result.failed_count else ""),
        (
            "never attempted",
            result.never_attempted_count,
            "bad" if result.never_attempted_count else "",
        ),
    ]
    stats = "\n".join(
        f'<div class="stat {css}"><span class="n">{value}</span>'
        f'<span class="l">{escape(label)}</span></div>'
        for label, value, css in tiles
    )
    return (
        "<header>"
        "<h1>Dedupe results</h1>"
        '<p class="subtitle">What this run actually did, batch by batch. '
        "Every album you approved appears in exactly one of the lists below.</p>"
        f'<div class="stats">{stats}</div>'
        "</header>"
        f"{banner}"
    )


def _results_breakdown(result: ExecutionResult) -> str:
    """The one sentence that says where every approved album actually ended up.

    A failure Spotify refused outright is stated as flatly as a never-attempted one,
    because it is exactly as certain: no album in a rejected batch was removed. Only
    the failures we genuinely cannot account for are hedged, so that the hedge keeps
    meaning something when the reader sees it.
    """
    clauses = [
        f"<strong>{result.removed_count}</strong> "
        f"{_plural(result.removed_count, 'was', 'were')} removed"
    ]
    if result.rejected_count:
        clauses.append(
            f"<strong>{result.rejected_count}</strong> "
            f"{_plural(result.rejected_count, 'was', 'were')} refused by Spotify "
            "before anything was changed (those are certainly still saved)"
        )
    if result.unknown_count:
        clauses.append(
            f"<strong>{result.unknown_count}</strong> failed after every retry "
            "(those requests were sent, so those albums may or may not still be "
            "saved)"
        )
    if result.never_attempted_count or not (result.rejected_count or result.unknown_count):
        clauses.append(
            f"<strong>{result.never_attempted_count}</strong> "
            f"{_plural(result.never_attempted_count, 'was', 'were')} never attempted "
            "at all (those are certainly still saved)"
        )
    return ", ".join(clauses[:-1]) + f", and {clauses[-1]}"


def _results_restore(result: ExecutionResult, restore_command: str) -> str:
    """Where the undo lives and exactly what to type. Never below the fold."""
    if result.restore_path is None:
        return (
            '<div class="panel"><h3>No restore file</h3>'
            "<p>None was needed: this run removed nothing.</p></div>"
        )
    return (
        '<div class="panel"><h3>Undo this run</h3>'
        "<p>Every album this run set out to remove was written to a restore file "
        "<em>before</em> the first deletion was sent:</p>"
        f'<p class="path mono">{escape(str(result.restore_path))}</p>'
        "<p>Re-save all of them with:</p>"
        f'<pre class="cmd">{escape(restore_command)}</pre>'
        "<p>The file lists what this run intended to remove, which may be more than "
        "it managed to remove. Re-saving an album that is still saved changes "
        "nothing, so running the restore is always safe.</p>"
        "</div>"
    )


def _album_rows(ids: tuple[str, ...], resolution: Resolution) -> str:
    known = {album.id: album for album in resolution.restore_albums}
    rows = []
    for album_id in ids:
        album = known.get(album_id)
        name = escape(album.name) if album else "<em>(unknown album)</em>"
        artists = escape(album.artists) if album else ""
        rows.append(
            f'<tr><td class="name">{name}</td>'
            f'<td class="artist">{artists}</td>'
            f'<td class="idcell">{escape(album_id)}</td></tr>'
        )
    return (
        '<table class="albums-table">'
        "<thead><tr><th>Album</th><th>Artist</th><th>Spotify id</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _results_failures(result: ExecutionResult, resolution: Resolution) -> str:
    """The failed albums, in two panels, because there are two kinds of failure.

    A batch Spotify rejected with a 4xx never got as far as changing anything, so
    those albums are as certainly still saved as a never-attempted one, and saying
    "state unknown" about them would send the reader off to check a library that is
    exactly as they left it. A batch that failed any other way -- a timeout, a
    connection dropped mid-request, a 5xx that outlived its retries -- really might
    have applied first, and keeps the unknown-state wording.
    """
    return _results_rejected(result, resolution) + _results_unknown(result, resolution)


def _results_rejected(result: ExecutionResult, resolution: Resolution) -> str:
    ids = result.rejected_ids
    if not ids:
        return ""
    reasons = "".join(
        f'<p class="err">Batch {batch.number}: {escape(batch.error or "")}</p>'
        for batch in result.batches
        if batch.rejected
    )
    return (
        '<div class="panel alarm"><h3>'
        f"{len(ids)} {_plural(len(ids), 'album')} refused by Spotify "
        "&mdash; still saved</h3>"
        "<p>Spotify rejected these requests outright, which means it did not remove "
        "any of the albums in them: they are still in your library, exactly as they "
        "were. Nothing here is half-done and nothing needs checking. Fix what the "
        "error below names, then re-run the dedupe.</p>"
        f"{reasons}"
        f"{_album_rows(ids, resolution)}"
        "</div>"
    )


def _results_unknown(result: ExecutionResult, resolution: Resolution) -> str:
    ids = result.unknown_ids
    if not ids:
        return ""
    return (
        '<div class="panel alarm"><h3>'
        f"{len(ids)} {_plural(len(ids), 'album')} failed &mdash; state unknown</h3>"
        "<p>These requests were sent to Spotify and did not come back successfully "
        "after every retry. Spotify may have applied some of them before failing, so "
        "these albums may or may not still be in your library. Check them, and "
        "re-run the dedupe if they are still there.</p>"
        f"{_album_rows(ids, resolution)}"
        "</div>"
    )


def _results_never_attempted(result: ExecutionResult, resolution: Resolution) -> str:
    ids = result.never_attempted_ids
    if not ids:
        return ""
    return (
        '<div class="panel alarm"><h3>'
        f"{len(ids)} {_plural(len(ids), 'album')} never attempted &mdash; still saved</h3>"
        "<p>The run stopped before these were sent to Spotify at all. No request was "
        "ever issued for them, so they are still in your library exactly as they "
        "were. Re-run the dedupe to finish the job.</p>"
        f"{_album_rows(ids, resolution)}"
        "</div>"
    )


def _results_removed(result: ExecutionResult, resolution: Resolution) -> str:
    ids = result.removed_ids
    if not ids:
        return ""
    return (
        '<div class="panel"><h3>'
        f"{len(ids)} {_plural(len(ids), 'album')} removed</h3>"
        '<details class="roll"><summary>Show every album removed</summary>'
        f"{_album_rows(ids, resolution)}</details></div>"
    )


def _results_batches(result: ExecutionResult) -> str:
    """Every batch, in the order it was planned, with its own verdict.

    Shown in full rather than only on failure: this is the audit trail, and an audit
    trail that only appears when something went wrong teaches nobody what normal
    looks like.
    """
    if not result.batches:
        return ""
    cards = []
    for batch in result.batches:
        label = batch.status.replace("_", " ")
        attempts = (
            f" after {batch.attempts} {_plural(batch.attempts, 'attempt')}"
            if batch.attempts > 1
            else ""
        )
        error = f'<p class="err">{escape(batch.error)}</p>' if batch.error else ""
        cards.append(
            f'<div class="batch {escape(batch.status)}">'
            f'<div class="head"><span class="tag {escape(batch.status)}">'
            f"{escape(label)}</span>"
            f"Batch {batch.number} &mdash; {batch.size} "
            f"{_plural(batch.size, 'album')}{attempts}</div>"
            f"{error}</div>"
        )
    return (
        '<div class="panel"><h3>Every batch</h3>'
        "<p>Removals are sent 50 ids at a time, the most the API accepts in one "
        "request. Each batch has exactly one verdict.</p>"
        f"{''.join(cards)}</div>"
    )


def _results_footer(result: ExecutionResult, resolution: Resolution) -> str:
    skipped = len(resolution.skipped_groups)
    return (
        "<footer>"
        f"<p><strong>{len(resolution.kept)}</strong> "
        f"{_plural(len(resolution.kept), 'album')} you chose to keep "
        f"{_plural(len(resolution.kept), 'was', 'were')} left untouched, across "
        f"<strong>{skipped}</strong> skipped {_plural(skipped, 'group')} and the "
        "keepers of the groups you resolved.</p>"
        f"<p>Run recorded at {escape(result.created_at)}.</p>"
        '<p class="note">This page is a permanent record of one run; an archived copy '
        "was written next to it on disk. All styling is embedded in this file.</p>"
        "</footer>"
    )
