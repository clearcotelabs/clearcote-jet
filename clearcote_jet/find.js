// Where `words` first appear on the page: the first heading that contains them, else the first visible text that
// does. Returns the place (document y) and an id to jump to, when the heading (or a section starting at the same
// place) has one. Runs in the isolated world and only reads. Must never throw.
(words => {
  try {
    const want = String(words || '').replace(/\s+/g, ' ').trim().toLowerCase();
    if (!want || !document.body) return null;
    const top = e => e.getBoundingClientRect().top + scrollY;
    // an id at this place: the element's own, an anchor inside it, or a section that starts where it starts
    const idAt = (el) => {
      if (el.id) return el.id;
      const inner = el.querySelector('[id],a[name]');
      if (inner) return inner.id || inner.getAttribute('name');
      for (let n = el.parentElement; n && n !== document.body; n = n.parentElement) {
        if (Math.abs(top(n) - top(el)) > 40) break;
        if (n.id) return n.id;
      }
      return null;
    };
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let node, seen = 0, first = null;
    while ((node = walker.nextNode()) && seen++ < 300000) {
      if (!node.textContent.replace(/\s+/g, ' ').toLowerCase().includes(want)) continue;
      const el = node.parentElement;
      if (!el || el.closest('script,style,noscript,template') || !el.checkVisibility()) continue;
      const heading = el.closest('h1,h2,h3,h4,h5,h6,[role=heading]');
      const hit = {y: top(heading || el), heading: !!heading, id: idAt(heading || el)};
      if (heading) return {...hit, scrollY, vh: innerHeight};
      first = first || hit;
    }
    // no heading: the text itself, but an ancestor's id only if it starts at the same place
    return first && {...first, scrollY, vh: innerHeight};
  } catch (e) {
    return null;
  }
})(%s)
