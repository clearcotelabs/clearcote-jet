// Read the rows of a saved list: {item: <selector for one row>, fields: {name: "selector" or "selector@attr"}}.
// Runs in the isolated world and only reads. Must never throw.
(spec => {
  try {
    const text = e => (e.innerText || e.textContent || '').replace(/\s+/g, ' ').trim();
    const pick = (row, f) => {
      const at = f.lastIndexOf('@'), sel = at >= 0 ? f.slice(0, at) : f, attr = at >= 0 ? f.slice(at + 1) : null;
      const el = sel ? row.querySelector(sel) : row;
      if (!el) return null;
      if (attr === 'href' || attr === 'src') {
        const v = attr === 'src' ? (el.currentSrc || el.getAttribute('src')) : el.getAttribute('href');
        return v ? new URL(v, location.href).href : null;
      }
      return attr ? el.getAttribute(attr) : text(el) || null;
    };
    return [...document.querySelectorAll(spec.item)].slice(0, spec.max || 50).map(row => {
      const out = {};
      for (const [k, f] of Object.entries(spec.fields || {})) {
        try { const v = pick(row, f); if (v) out[k] = String(v).slice(0, 300); } catch (e) {}
      }
      return out;
    });
  } catch (e) {
    return [];
  }
})(%s)
