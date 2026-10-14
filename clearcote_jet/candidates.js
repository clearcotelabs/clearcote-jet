// Every value on the page that something could be READ from, each with a selector that finds it
// again. The model picks among these; it never writes a selector itself.
//
// `(spec)`: {row: "<selector for one row>" or null, max: <how many to return>}
//   - without `row`: candidates are page-wide and their selectors are absolute.
//   - with `row`: candidates come from inside the FIRST matching row and their selectors are
//     relative to it, so they can be used to read every row (the shape lists.js produces).
//
// A candidate is kept only when its selector finds exactly it again — the check that matters, done
// here where the DOM is, so nothing downstream has to trust a string.
//
// Runs in the isolated world and only reads. Must never throw.
(spec => {
  try {
    const max = Math.max(1, Math.min(200, spec.max || 60));
    const text = e => (e.innerText || e.textContent || '').replace(/\s+/g, ' ').trim();
    // Class names a redesign keeps: no digit soup, no state classes. Same rule lists.js uses, so a
    // selector from either side ages the same way.
    const steady = c => !/\d{3,}/.test(c) && !/^(is|js|has)-/.test(c) &&
      !/^(active|selected|current|odd|even|first|last|hover|focus|open|show|hidden|visible|clearfix)$/.test(c);
    const sig = (e, k = 2) => e.tagName.toLowerCase() +
      [...e.classList].filter(steady).slice(0, k).map(c => '.' + CSS.escape(c)).join('');
    const shown = e => {
      const r = e.getBoundingClientRect();
      return r.width > 4 && r.height > 4 &&
        (!e.checkVisibility || e.checkVisibility({checkOpacity: true, checkVisibilityCSS: true}));
    };
    const chrome = e => e.closest('nav,header,footer,aside,script,style,noscript,[role=navigation],[role=banner],[role=contentinfo]');
    // Elements that hold text of their own, rather than the text of everything beneath them.
    const leaves = root => [...root.querySelectorAll('*')].filter(e =>
      [...e.childNodes].some(c => c.nodeType === 3 && c.textContent.trim()));

    const pathTo = e => {
      const parts = [];
      for (let n = e; n && n !== document.body && n !== document.documentElement && parts.length < 6; n = n.parentElement) {
        if (n.id && !/\d{3,}/.test(n.id)) { parts.unshift('#' + CSS.escape(n.id)); break; }
        parts.unshift(sig(n));
      }
      return parts.join(' > ');
    };
    const chain = (row, el, k) => {
      const parts = [];
      for (let n = el; n && n !== row; n = n.parentElement) parts.unshift(sig(n, k));
      return parts.join(' > ');
    };
    // The shortest selector inside a row that still finds this element and no other.
    const within = (row, el) => {
      const tries = [chain(row, el, 2), chain(row, el, 1), sig(el, 1), chain(row, el, 0), sig(el, 0)];
      for (const c of tries) {
        if (!c) continue;
        try { if (row.querySelector(c) === el) return c; } catch (err) { /* not a selector */ }
      }
      return null;
    };

    const out = [];
    const seen = new Set();
    const add = (el, sel, scope) => {
      if (sel === null || sel === undefined || seen.has(sel) || out.length >= max) return;
      // The selector has to find this very element again, from the scope it will be used in. The
      // empty selector is the row itself, which needs no finding.
      if (sel === '') { if (el !== scope) return; }
      else { try { if (scope.querySelector(sel) !== el) return; } catch (err) { return; } }
      const value = text(el);
      if (!value || value.length > 200) return;
      seen.add(sel);
      out.push({i: out.length, sel, text: value, tag: el.tagName.toLowerCase()});
    };

    if (spec.row) {
      let row;
      try { row = document.querySelector(spec.row); } catch (err) { row = null; }
      if (!row) return {row: null, candidates: []};
      for (const el of leaves(row)) if (shown(el)) add(el, within(row, el), row);
      // The row itself is a candidate: a one-line row has its value on the container.
      add(row, '', row);
      return {row: spec.row, candidates: out};
    }

    for (const el of leaves(document.body)) if (shown(el) && !chrome(el)) add(el, pathTo(el), document);
    return {row: null, candidates: out};
  } catch (e) {
    return {row: null, candidates: []};
  }
})(%s)
