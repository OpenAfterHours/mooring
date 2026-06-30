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

  return { group, folderOf, norm };
})();

if (typeof window !== "undefined") window.FilesTree = FilesTree;
if (typeof module !== "undefined" && module.exports) module.exports = FilesTree;
