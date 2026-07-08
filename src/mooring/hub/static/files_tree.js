"use strict";

// Pure, DOM-free grouping of the hub's flat file list into folder sections, so the
// folder STRUCTURE is visible — including an adopted/declared folder that is still
// empty ("here's where notebooks go", even before the first file lands). Loaded BEFORE
// app.js (exposes `FilesTree` as a bare global + on `window`); under Node it is
// require()d by tests/js. Nothing here touches the DOM, network, or storage.

const FilesTree = (function () {
  // Normalize a declared folder to a POSIX path with no surrounding slashes.
  function norm(folder) {
    return String(folder == null ? "" : folder)
      .replace(/\\/g, "/")
      .replace(/^\/+|\/+$/g, "");
  }

  // The folder a path belongs to: the LONGEST declared-folder prefix it falls under,
  // else its own top-level segment (so a file outside the declared scope still groups
  // under its real folder rather than the root), else "" (a loose root-level file).
  function folderOf(path, declared) {
    let best = "";
    for (const raw of declared) {
      const f = norm(raw);
      if (f && (path === f || path.startsWith(f + "/")) && f.length > best.length) best = f;
    }
    if (best) return best;
    const slash = path.indexOf("/");
    return slash === -1 ? "" : path.slice(0, slash);
  }

  // Group `files` into ordered folder sections. Every DECLARED folder appears (empty
  // when it holds no files); folders are sorted, with loose root-level files (e.g.
  // mooring.toml) in a trailing "" section. Each file gets `rel` — its path within the
  // section — for a compact display. Pure: inputs are never mutated.
  function group(files, declared) {
    const decl = Array.from(new Set((declared || []).map(norm).filter(Boolean)));
    const buckets = new Map();
    decl.forEach((f) => buckets.set(f, [])); // seed declared folders so empties show
    let root = null;
    for (const file of files || []) {
      const folder = folderOf(file.path, decl);
      // A file whose path equals a declared folder name (a loose file literally named
      // e.g. "notebooks") would slice to "" — fall back to the full path so the row is
      // never nameless.
      const rel = folder && file.path !== folder ? file.path.slice(folder.length + 1) : file.path;
      const entry = Object.assign({}, file, { rel });
      if (folder === "") {
        root = root || [];
        root.push(entry);
      } else {
        if (!buckets.has(folder)) buckets.set(folder, []);
        buckets.get(folder).push(entry);
      }
    }
    const result = Array.from(buckets.keys())
      .sort()
      .map((folder) => ({
        folder,
        files: buckets.get(folder),
        empty: buckets.get(folder).length === 0,
      }));
    if (root) result.push({ folder: "", files: root, empty: false });
    return result;
  }

  // Whether a file row matches a free-text catalog query. Space-separated terms are
  // ANDed; each must appear (case-insensitively) in the file's path, its harvested
  // title, or any of its tags. An empty query matches everything. Pure — used to filter
  // the listing client-side so an analyst can find a notebook in a big repo.
  function matches(file, query) {
    const q = String(query == null ? "" : query).trim().toLowerCase();
    if (!q) return true;
    const hay = [file.path, file.title]
      .concat(Array.isArray(file.tags) ? file.tags : [])
      .filter(Boolean)
      .join(" ")
      .toLowerCase();
    return q.split(/\s+/).every((term) => hay.includes(term));
  }

  // -- focus one folder + declutter (display-only, client-side) ---------------
  // Everything below narrows or re-roots the SAME flat file list by path. No value
  // leaves the client — folder structure is a pure function of each row's `path`,
  // and paths are already visible in the hub. Pure: inputs are never mutated.

  // "Many folders" thresholds: at/above either, folder sections open COLLAPSED so a
  // crowded repo isn't a wall of rows. Below both, the listing stays fully expanded
  // (today's behaviour) so small/flat repos are never punished. See `crowded`.
  const CROWD_FOLDERS = 4;
  const CROWD_FILES = 20;

  // The files at or under `focus` (a folder prefix). focus "" → every file. Slash-
  // bounded, so focus "report" never captures "reports/…".
  function scope(files, focus) {
    const f = norm(focus);
    if (!f) return (files || []).slice();
    return (files || []).filter((x) => x.path === f || x.path.startsWith(f + "/"));
  }

  // The breadcrumb trail for a focus, outermost segment first:
  // "reports/2026" → [{label:"reports",prefix:"reports"},{label:"2026",prefix:"reports/2026"}].
  // The caller renders an "All folders" (prefix "") root ahead of these.
  function crumbs(focus) {
    const f = norm(focus);
    if (!f) return [];
    const out = [];
    let acc = "";
    for (const part of f.split("/")) {
      acc = acc ? acc + "/" + part : part;
      out.push({ label: part, prefix: acc });
    }
    return out;
  }

  // The folder sections ONE LEVEL below `focus`, re-rooted so deep structure stops
  // flattening: with focus "reports", reports/2026/q3/x.py groups under "reports/2026"
  // (not the single flattened "reports/"). Each section is shaped like `group`'s —
  // {folder (full prefix), label (segment below focus), files:[{…,rel}], empty} —
  // plus `here:true` for the leading section of files that live DIRECTLY in the focus.
  // Declared folders immediately under the focus seed empty sections (the "here's
  // where notebooks go" nudge, one level down). focus "" delegates to `group`.
  function subsections(files, declared, focus) {
    const base = norm(focus);
    if (!base) return group(files, declared);
    const prefix = base + "/";
    const buckets = new Map();
    const here = [];
    for (const raw of declared || []) {
      const d = norm(raw);
      if (d.startsWith(prefix)) {
        const child = prefix + d.slice(prefix.length).split("/")[0];
        if (!buckets.has(child)) buckets.set(child, []);
      }
    }
    for (const file of files || []) {
      if (file.path !== base && !file.path.startsWith(prefix)) continue;
      const rest = file.path === base ? file.path : file.path.slice(prefix.length);
      const slash = rest.indexOf("/");
      if (slash === -1) {
        here.push(Object.assign({}, file, { rel: rest }));
      } else {
        const full = prefix + rest.slice(0, slash);
        if (!buckets.has(full)) buckets.set(full, []);
        buckets.get(full).push(Object.assign({}, file, { rel: file.path.slice(full.length + 1) }));
      }
    }
    const result = Array.from(buckets.keys()).sort().map((full) => ({
      folder: full,
      label: full.slice(prefix.length),
      files: buckets.get(full),
      empty: buckets.get(full).length === 0,
      here: false,
    }));
    if (here.length) {
      // The "here" section shares `folder` with the aggregate `group` section for the
      // same path (both "reports"), so it needs its OWN collapse key or the two fight
      // over one remembered open/closed bit. A trailing slash can't collide — norm()
      // strips trailing slashes, so no group folder ever carries one.
      result.unshift({
        folder: base, label: base.split("/").pop(), files: here,
        empty: false, here: true, expandKey: base + "/",
      });
    }
    return result;
  }

  // Whether the sections about to render should open COLLAPSED by default — true only
  // for a "crowded" repo. A lone folder never auto-collapses (opening a repo to a
  // single mysterious collapsed row reads as "where did my files go?"). The caller
  // layers each folder's remembered choice on top of this default.
  function crowded(sections) {
    const real = (sections || []).filter((s) => s.folder !== "" && !s.here);
    if (real.length <= 1) return false;
    const files = (sections || []).reduce((n, s) => n + (s.files ? s.files.length : 0), 0);
    return real.length >= CROWD_FOLDERS || files > CROWD_FILES;
  }

  // Whether a stored focus still points at something real — a file lives under it, or
  // a declared folder is at/under/above it. Used to self-heal a focus whose folder a
  // teammate renamed or deleted (reset to "All folders" instead of a blank card).
  function focusLive(files, declared, focus) {
    const f = norm(focus);
    if (!f) return true;
    if (scope(files, f).length) return true;
    return (declared || []).some((raw) => {
      const d = norm(raw);
      return d === f || d.startsWith(f + "/") || f.startsWith(d + "/");
    });
  }

  return { group, folderOf, norm, matches, scope, crumbs, subsections, crowded, focusLive };
})();

if (typeof window !== "undefined") window.FilesTree = FilesTree;
if (typeof module !== "undefined" && module.exports) module.exports = FilesTree;
