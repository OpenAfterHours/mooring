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

  // A recursive folder tree rooted at `focus` ("" = the repo root). Unlike the two-level
  // group(), deep sub-folders nest as their own collapsible nodes instead of flattening:
  // reports/2026/q3/x.py becomes reports → 2026 → q3 → x.py. Returns the focus LEVEL as
  // `{ folders: node[], files: directFile[] }`, where each node recurses via node.children.
  // A node is `{ path (full prefix), name (own segment), files:[{…,rel}], children:[node],
  // count (files in the whole subtree), empty (a declared folder with nothing beneath) }`.
  // Declared folders under the focus are seeded so an empty one still shows. Files carry
  // `rel` = their basename. Pure — inputs are never mutated.
  function tree(files, declared, focus) {
    const base = norm(focus);
    const prefix = base ? base + "/" : "";
    const root = { path: base, name: base ? base.split("/").pop() : "", files: [], kids: new Map() };
    // Walk/create the chain of nodes for `segments` (already relative to the focus),
    // returning the leaf. Node paths are absolute (from the repo root).
    function ensure(segments) {
      let node = root;
      let acc = base;
      for (const seg of segments) {
        acc = acc ? acc + "/" + seg : seg;
        if (!node.kids.has(seg)) node.kids.set(seg, { path: acc, name: seg, files: [], kids: new Map() });
        node = node.kids.get(seg);
      }
      return node;
    }
    // A path relative to the focus (or null if it doesn't live under the focus).
    function under(p) {
      if (!base) return p;
      if (p === base) return "";
      return p.startsWith(prefix) ? p.slice(prefix.length) : null;
    }
    for (const raw of declared || []) {
      const rest = under(norm(raw));
      if (rest) ensure(rest.split("/")); // seed empty declared folders (the leaf ends up empty)
    }
    for (const file of files || []) {
      const rest = under(file.path);
      if (rest == null) continue;
      const segments = rest ? rest.split("/") : [file.path];
      const name = segments.pop();
      ensure(segments).files.push(Object.assign({}, file, { rel: name }));
    }
    function finalize(node) {
      node.files.sort((a, b) => (a.rel < b.rel ? -1 : a.rel > b.rel ? 1 : 0));
      node.children = Array.from(node.kids.values())
        .sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0))
        .map(finalize);
      delete node.kids;
      node.count = node.children.reduce((n, c) => n + c.count, node.files.length);
      // Only a true leaf (no files, no sub-folders) is "empty" — an intermediate declared
      // chain keeps a live caret so you can still drill to the declared leaf beneath it.
      node.empty = node.files.length === 0 && node.children.length === 0;
      return node;
    }
    finalize(root);
    return { folders: root.children, files: root.files };
  }

  // Every folder-node path in a tree, depth-first — the set Expand-all / Collapse-all pins.
  function allFolderPaths(t) {
    const out = [];
    (function walk(nodes) {
      for (const n of nodes || []) {
        out.push(n.path);
        walk(n.children);
      }
    })(t && t.folders);
    return out;
  }

  // How many folder nodes can actually be steered open/closed — an empty declared leaf has
  // a disabled caret and nothing beneath, so it doesn't count. Decides whether the
  // Expand/Collapse-all toggles are worth showing (they'd be inert no-ops otherwise).
  function expandableCount(t) {
    let n = 0;
    (function walk(nodes) {
      for (const node of nodes || []) {
        if (!node.empty) n += 1;
        walk(node.children);
      }
    })(t && t.folders);
    return n;
  }

  // Whether folders should open COLLAPSED by default — true only for a "crowded" level.
  // A lone folder never auto-collapses (opening a repo to a single mysterious collapsed
  // row reads as "where did my files go?"). The caller layers remembered choices on top.
  function crowdedCount(folderCount, fileCount) {
    if (folderCount <= 1) return false;
    return folderCount >= CROWD_FOLDERS || fileCount > CROWD_FILES;
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

  return { group, folderOf, norm, matches, scope, crumbs, tree, allFolderPaths, expandableCount, crowdedCount, focusLive };
})();

if (typeof window !== "undefined") window.FilesTree = FilesTree;
if (typeof module !== "undefined" && module.exports) module.exports = FilesTree;
