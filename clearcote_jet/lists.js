// Lists of results on the page, found from their shape: three or more alike siblings, most with a link, outside the
// page's own navigation. Each list comes with a selector for its rows and one per field (title, link, price, image),
// chosen to work for most rows, so the rows can be read again later with no model. Runs in the isolated world and
// only reads. Must never throw.
(() => {
  try {
    const CUR = '(?:[$€£¥₹₽₺₩]|\\b(?:eur|usd|gbp|chf|pln|zł|sek|nok|dkk|czk|huf|ron|lei|rub|uah|inr|jpy|aud|cad|kr)\\b)';
    const PRICE = new RegExp(CUR + '\\s?\\d|\\d[\\d.,\\s\\u00a0]*\\s?' + CUR, 'i');
    // a discount badge or a saving, not the price itself
    const NOT_PRICE = /%|\b(off|save|saving|was|before|discount|korting|bespaar|rabatt|statt|réduction|sconto|descuento)\b/i;
    const cls = e => String(e.getAttribute && e.getAttribute('class') || '');
    const OLD = e => !!e.closest('del,s,strike') ||
      /\b(old|was|compare|regular|strike|before|original|list-?price)\b/i.test(cls(e) + ' ' + cls(e.parentElement || e));
    const SALE = /\b(sale|current|now|final|special|actual|offer)\b/i;
    const chrome = e => e.closest('nav,header,footer,aside,[role=navigation],[role=banner],[role=contentinfo]');
    const steady = c => !/\d{3,}/.test(c) && !/^(is|js|has)-/.test(c) &&
      !/^(active|selected|current|odd|even|first|last|hover|focus|open|show|hidden|visible|clearfix)$/.test(c);
    const sig = (e, k = 2) => e.tagName.toLowerCase() +
      [...e.classList].filter(steady).slice(0, k).map(c => '.' + CSS.escape(c)).join('');
    const shown = e => { const r = e.getBoundingClientRect();
      return r.width > 20 && r.height > 8 && e.checkVisibility({checkOpacity: true, checkVisibilityCSS: true}); };
    const text = e => (e.innerText || e.textContent || '').replace(/\s+/g, ' ').trim();
    const pathTo = e => {
      const parts = [];
      for (let n = e; n && n !== document.body && n !== document.documentElement && parts.length < 6; n = n.parentElement) {
        if (n.id && !/\d{3,}/.test(n.id)) { parts.unshift('#' + CSS.escape(n.id)); break; }
        parts.unshift(sig(n));
      }
      return parts.join(' > ');
    };
    const chain = (row, el, k) => { const parts = [];
      for (let n = el; n && n !== row; n = n.parentElement) parts.unshift(sig(n, k));
      return parts.join(' > '); };
    // A selector for a field inside one row: of the ones that find exactly this element first, the one that finds a
    // field in the most rows (most specific first on a tie).
    const within = (row, el, peers) => {
      if (el === row) return '';
      const cands = [chain(row, el, 2), chain(row, el, 1), sig(el, 1), chain(row, el, 0), sig(el, 0)]
        .filter((c, i, all) => c && all.indexOf(c) === i && row.querySelector(c) === el);
      if (!cands.length) return null;
      let best = cands[0], most = -1;
      for (const c of cands) {
        const n = peers.filter(p => { try { return !!p.querySelector(c); } catch (e) { return false; } }).length;
        if (n > most) { best = c; most = n; }
      }
      return best;
    };
    const pick = (row, spec) => {
      const at = spec.lastIndexOf('@'), sel = at >= 0 ? spec.slice(0, at) : spec, attr = at >= 0 ? spec.slice(at + 1) : null;
      const el = sel ? row.querySelector(sel) : row;
      if (!el) return null;
      if (attr === 'href' || attr === 'src') {
        const v = attr === 'src' ? (el.currentSrc || el.getAttribute('src')) : el.getAttribute('href');
        return v ? new URL(v, location.href).href : null;
      }
      return attr ? el.getAttribute(attr) : text(el) || null;
    };
    const rowsOf = (item, fields, max) => [...document.querySelectorAll(item)].slice(0, max).map(row => {
      const out = {};
      for (const [k, f] of Object.entries(fields)) { try { const v = pick(row, f); if (v) out[k] = String(v).slice(0, 300); } catch (e) {} }
      return out;
    });

    const groups = [];
    for (const parent of document.body.querySelectorAll('*')) {
      if (parent.children.length < 3 || chrome(parent)) continue;
      const bySig = new Map();
      for (const c of parent.children) { const s = sig(c); if (!bySig.has(s)) bySig.set(s, []); bySig.get(s).push(c); }
      for (const [s, all] of bySig) {
        if (all.length < 3) continue;
        const rows = all.filter(shown);
        if (rows.length < 3) continue;
        if (rows.filter(r => r.matches('a[href]') || r.querySelector('a[href]')).length < 0.6 * rows.length) continue;
        const avg = rows.reduce((n, r) => n + Math.min(text(r).length, 300), 0) / rows.length;
        if (avg < 12) continue;
        const priced = rows.filter(r => PRICE.test(text(r))).length / rows.length;
        const pictured = rows.filter(r => r.querySelector('img')).length / rows.length;
        groups.push({parent, s, rows, priced,
                     score: rows.length * Math.log(5 + avg) * (1 + 2 * priced) * (1 + 0.5 * pictured)});
      }
    }
    groups.sort((a, b) => b.score - a.score);

    const out = [];
    for (const g of groups.slice(0, 12)) {
      const item = pathTo(g.parent) + ' > ' + g.s;
      let count = 0;
      try { count = document.querySelectorAll(item).length; } catch (e) { continue; }
      if (count < 3 || out.some(o => o.item === item)) continue;
      const first = g.rows[0], peers = g.rows.slice(0, 15), fields = {};
      const links = [...first.querySelectorAll('a[href]')].concat(first.matches('a[href]') ? [first] : []);
      const head = first.querySelector('h1,h2,h3,h4,h5,h6,[class*=title],[class*=name]');
      const titled = links.find(l => l.getAttribute('title'));
      const link = (head && (head.matches('a[href]') ? head : head.querySelector('a[href]'))) || titled ||
        [...links].sort((x, y) => text(y).length - text(x).length)[0];
      const t = head && text(head) ? head : (link && text(link) ? link : null);
      if (t) { const sel = within(first, t, peers); if (sel !== null) fields.title = sel; }
      else if (titled) { const sel = within(first, titled, peers); if (sel !== null) fields.title = sel + '@title'; }
      if (link) { const sel = within(first, link, peers); if (sel !== null) fields.link = sel + '@href'; }
      const leaves = [...first.querySelectorAll('*')].filter(e =>
        [...e.childNodes].some(c => c.nodeType === 3 && c.textContent.trim()));
      const prices = leaves.filter(e => { const v = text(e); return PRICE.test(v) && v.length < 40 && !NOT_PRICE.test(v) && !OLD(e); });
      const price = prices.find(e => SALE.test(cls(e) + ' ' + cls(e.parentElement || e))) || prices[0];
      if (price) { const sel = within(first, price, peers); if (sel !== null) fields.price = sel; }
      const img = first.querySelector('img');
      if (img) { const sel = within(first, img, peers); if (sel !== null) fields.image = sel + '@src'; }
      if (!fields.title && !fields.price) continue;
      out.push({item, fields, count, priced: g.priced > 0.5, rows: rowsOf(item, fields, 20)});
      if (out.length >= 5) break;
    }
    return out;
  } catch (e) {
    return [];
  }
})()
