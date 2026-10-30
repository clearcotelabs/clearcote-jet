// Runs in a CDP isolated world: `window` here is the isolated world's own global, so the node cache
// below is invisible to page scripts. Must never throw: an exception makes the wrapper recreate the world.
(() => {
  if (!document.body) return null;
  const cache = window.__ca ||= {ids:new WeakMap(), nodes:new Map(), next:1, secret:new WeakSet()};
  const identity = e => {
    if (!cache.ids.has(e)) cache.ids.set(e,cache.next++);
    const id=cache.ids.get(e); cache.nodes.set(id,e); return id;
  };
  for (const [id,e] of cache.nodes) if (!e.isConnected) cache.nodes.delete(id);
  // Open shadow roots (consent CMPs, web components) are part of the page a user sees.
  const shadows=[];
  const findShadows=root=>{ for (const h of root.querySelectorAll('*')) if (h.shadowRoot) {
    shadows.push(h.shadowRoot); findShadows(h.shadowRoot); } };
  findShadows(document);
  const deepAll=sel=>[document,...shadows].flatMap(r=>[...r.querySelectorAll(sel)]);
  // elementFromPoint stops at a shadow host; descend to the element actually under the point.
  const deepPoint=(x,y)=>{ let t=document.elementFromPoint(x,y);
    while (t?.shadowRoot) { const i=t.shadowRoot.elementFromPoint(x,y); if (!i || i===t) break; t=i; }
    return t; };
  const byId=(e,id)=>e.getRootNode().getElementById?.(id) || document.getElementById(id);
  const safe = e => !['file','hidden'].includes(e.type);
  // Password fields can be typed into but are never read: all that leaves this world is whether one holds anything.
  // A field once seen as type=password stays secret after the page turns it into a text field ("show password").
  const secret = e => {
    if (e.tagName==='INPUT' && e.type==='password') cache.secret.add(e);
    return cache.secret.has(e);
  };
  const formValue = e => secret(e) ? e.value.length>0 : e.value??null;
  const visible = e => !e.closest('[aria-hidden="true"],[inert]') &&
    e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
  // Labels that name this field. Two labels pointing at one field (a copy-paste slip: for="username" twice) name
  // two fields: this one takes the last label before it, the other label is for the field after it.
  const ownLabels = e => {
    const all=[...(e.labels||[])];
    if (all.length<2) return all;
    const before=all.filter(l=>l.compareDocumentPosition(e)&Node.DOCUMENT_POSITION_FOLLOWING);
    return [before.length ? before[before.length-1] : all[0]];
  };
  // A typing field with no label of its own: the label just before it, beside it or in the cell before its
  // wrapper (a form row: "Subjects" | [field], a label for= another id by mistake). Nearest wrapper first.
  const typing = e => e.tagName==='TEXTAREA' || e.tagName==='SELECT' ||
    (e.tagName==='INPUT' && !['checkbox','radio','button','submit','reset','image','hidden','range'].includes(e.type));
  const nearbyLabel = (e,any=false) => {
    if (!(any || typing(e)) || (e.labels||[]).length) return '';
    for (let n=e, depth=0; n && n!==document.body && depth<6; n=n.parentElement, depth++) {
      for (let p=n.previousElementSibling, k=0; p && k<2; p=p.previousElementSibling, k++) {
        const l = p.tagName==='LABEL' ? p : (p.children.length<=3 ? p.querySelector('label') : null);
        const t = l && (l.textContent||'').trim();
        if (t && t.length<=60) return t;
        if (p.matches('input,select,textarea,button') || p.querySelector('input,select,textarea')) return '';
      }
    }
    return '';
  };
  const name = (e,seen=new Set()) => {
    if (!e || seen.has(e)) return '';
    seen.add(e);
    const referenced=(e.getAttribute('aria-labelledby')||'').split(/\s+/)
      .map(id=>name(byId(e,id),seen)).filter(Boolean).join(' ');
    return referenced || e.getAttribute('aria-label') ||
      ownLabels(e).map(l=>name(l,seen)).filter(Boolean).join(' ') || nearbyLabel(e) ||
      (['button','submit','reset'].includes(e.type) ? e.value : '') || e.getAttribute('alt') ||
      (e.tagName==='INPUT' ? '' : [...e.childNodes].map(n=>n.nodeType===3 ? n.textContent :
        n.nodeType===1 && n.getAttribute('aria-hidden')!=='true' ? name(n,seen) : '').join(' ').trim()) ||
      e.getAttribute('title') || e.getAttribute('placeholder') || '';
  };
  const roles=['button','link','checkbox','radio','switch','tab','menuitem','menuitemradio',
    'option','gridcell','combobox','textbox','searchbox','spinbutton'];
  const selector='a[href],button,input,textarea,select,summary,[contenteditable="true"],'+
    roles.map(role=>'[role="'+role+'"]').join(',');
  const clickable=new WeakSet(); // elements with no role that look clickable (see below): buttons to the model
  const role = e => {
    const explicit=e.getAttribute('role');
    if (roles.includes(explicit)) return explicit;
    if (clickable.has(e)) return 'button';
    if (e.tagName==='BUTTON' || e.tagName==='SUMMARY') return 'button';
    if (e.tagName==='A') return 'link';
    if (e.tagName==='SELECT') return 'combobox';
    if (e.tagName==='TEXTAREA' || e.isContentEditable) return 'textbox';
    if (e.tagName==='INPUT') {
      if (['checkbox','radio'].includes(e.type)) return e.type;
      if (['button','submit','reset','image'].includes(e.type)) return 'button';
      if (e.type==='search') return 'searchbox';
      if (e.type==='number') return 'spinbutton';
      if (e.type==='range') return 'slider';
      if (['text','email','url','tel','password',''].includes(e.type)) return 'textbox';
    }
    return null;
  };
  cache.pageKey=()=>[performance.timeOrigin,location.href,scrollX,scrollY,innerWidth,innerHeight,
    [...document.querySelectorAll('input,textarea,select')].filter(safe)
      .map(e=>[identity(e),formValue(e),e.checked,e.selectedIndex,e.disabled,e.readOnly])];
  cache.guard=e=>{
    if (!e?.isConnected || !visible(e)) return null;
    const scope=e.closest('form,dialog,[role="dialog"],article,li,tr,[role="row"]') || e.parentElement;
    return [identity(e),role(e),name(e),formValue(e),e.checked??null,e.selectedIndex??null,
      e.readOnly??null,e.matches(':disabled'),e.getAttribute('aria-disabled'),
      e.getAttribute('aria-expanded'),e.getAttribute('aria-checked'),e.getAttribute('aria-selected'),
      e.getAttribute('href'),scope?.innerText?.slice(0,6000)||''];
  };
  // Executor helpers. Geometry is resolved here, immediately before input, never from the snapshot.
  cache.box=(id,kind)=>{
    const e=cache.nodes.get(id);
    if (!e?.isConnected || e.matches(':disabled') || e.closest('[aria-disabled="true"],[inert]') ||
        !e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true})) return null;
    if (kind==='fill' && (e.readOnly || e.getAttribute('aria-readonly')==='true')) return null;
    const r=e.getBoundingClientRect(), x=r.x+r.width/2, y=r.y+r.height/2;
    if (!r.width || !r.height || x<0 || y<0 || x>=innerWidth || y>=innerHeight) return null;
    if (!e.contains(deepPoint(x,y))) return null;
    const input=['INPUT','TEXTAREA'].includes(e.tagName) || e.isContentEditable;
    return {x:r.x,y:r.y,width:r.width,height:r.height,input};
  };
  cache.hit=(id,x,y)=>{
    const e=cache.nodes.get(id);
    return !!e?.isConnected && e.contains(deepPoint(x,y));
  };
  cache.select=(id,value)=>{
    const e=cache.nodes.get(id);
    if (!e?.isConnected || e.tagName!=='SELECT' || ![...e.options].some(o=>o.value===value &&
        !o.disabled && !o.closest('optgroup[disabled]'))) return false;
    e.value=value;
    e.dispatchEvent(new Event('input',{bubbles:true}));
    e.dispatchEvent(new Event('change',{bubbles:true}));
    return true;
  };
  // A scroll box of its own (a phone menu's panel, a dialog's body, a side list, a carousel): the nearest ancestor
  // that scrolls, other than the page itself. Scrolling the page does not move what is inside it.
  const scroller=e=>{
    for (let p=e.parentElement; p && p!==document.body && p!==document.documentElement; p=p.parentElement) {
      const cs=getComputedStyle(p);
      if ((/(auto|scroll)/.test(cs.overflowY) && p.scrollHeight>p.clientHeight+2) ||
          (/(auto|scroll)/.test(cs.overflowX) && p.scrollWidth>p.clientWidth+2)) return p;
    }
    return null;
  };
  // The part of a scroll box a person sees and can wheel over: its inside, cut to the screen.
  const view=p=>{
    const r=p.getBoundingClientRect(), left=Math.max(r.left+p.clientLeft,0), top=Math.max(r.top+p.clientTop,0);
    const right=Math.min(r.left+p.clientLeft+p.clientWidth,innerWidth);
    const bottom=Math.min(r.top+p.clientTop+p.clientHeight,innerHeight);
    return right-left>=20 && bottom-top>=20 ? {left,top,right,bottom} : null;
  };
  // Controls scrolled out of view inside a scroll box of their own: wheeling over the box brings them in, scrolling
  // the page does not (and a page under an open phone menu often cannot scroll at all). Named by the group they are
  // in: the last control expanded before them in the box ("Docs" in an open phone menu), else the box's own label.
  const clipped=limit=>{
    const out=[], clickable=['link','menuitem','button','option','tab','checkbox','radio','switch'], memo=new Map();
    const boxFrom=p=>{ // scroller(), remembered per element: a page has many controls under the same ancestors
      if (!p || p===document.body || p===document.documentElement) return null;
      if (!memo.has(p)) {
        const cs=getComputedStyle(p);
        memo.set(p,(/(auto|scroll)/.test(cs.overflowY) && p.scrollHeight>p.clientHeight+2) ||
          (/(auto|scroll)/.test(cs.overflowX) && p.scrollWidth>p.clientWidth+2) ? p : boxFrom(p.parentElement));
      }
      return memo.get(p);
    };
    for (const e of deepAll(selector)) {
      if (out.length>=limit) break;
      if (!safe(e) || e.matches(':disabled') || e.closest('[aria-disabled="true"]') || !visible(e)) continue;
      const rname=role(e), p=rname && clickable.includes(rname) && boxFrom(e.parentElement), v=p && visible(p) && view(p);
      if (!v) continue;
      const r=e.getBoundingClientRect();
      if (!r.width || !r.height || (r.top>=v.top-1 && r.bottom<=v.bottom+1 && r.left>=v.left-1 && r.right<=v.right+1)) continue;
      const label=name(e).replace(/\s+/g,' ').trim();
      if (!label) continue;
      let group=null;
      for (const t of p.querySelectorAll('[aria-expanded="true"]'))
        if (t!==e && t.compareDocumentPosition(e) & Node.DOCUMENT_POSITION_FOLLOWING) group=t;
      const own=p.getAttribute('aria-label') ||
        (p.getAttribute('aria-labelledby')||'').split(/\s+/).filter(Boolean).map(id=>name(byId(p,id))).join(' ');
      const where=((group && name(group)) || own || 'Scroll box').replace(/\s+/g,' ').trim().slice(0,60);
      out.push({node:identity(e),role:rname,kind:'click',value:'',panel:identity(p),
        label:(where+' › '+label).slice(0,200),guard:JSON.stringify(cache.guard(e))});
    }
    return out;
  };
  // Where to wheel to bring control `id` into view in scroll box `pid`: a point over the box where the wheel scrolls
  // this box and not a smaller one inside it, and how far the control is from the middle of the box's view.
  cache.panelPoint=(pid,id)=>{
    const p=cache.nodes.get(pid), e=cache.nodes.get(id), v=p?.isConnected && view(p);
    if (!v || !e?.isConnected) return null;
    const r=e.getBoundingClientRect();
    for (const [fx,fy] of [[.5,.5],[.5,.3],[.5,.7],[.3,.5],[.7,.5],[.2,.2],[.8,.8],[.2,.8],[.8,.2]]) {
      const x=v.left+(v.right-v.left)*fx, y=v.top+(v.bottom-v.top)*fy, h=deepPoint(x,y);
      if (h && (h===p || (p.contains(h) && scroller(h)===p)))
        return {x,y,dx:r.left+r.width/2-(v.left+v.right)/2,dy:r.top+r.height/2-(v.top+v.bottom)/2,
          at:[p.scrollLeft,p.scrollTop]};
    }
    return null;
  };
  // How far to scroll so a control a pinned strip covers sits in the middle of the screen, clear of it.
  cache.centerDelta=id=>{
    const e=cache.nodes.get(id);
    if (!e?.isConnected) return null;
    const r=e.getBoundingClientRect();
    return Math.round(r.y+r.height/2-innerHeight/2);
  };
  cache.rangePlan=id=>{
    const e=cache.nodes.get(id);
    if (!e?.isConnected || e.type!=='range') return null;
    const n=v=>{ const f=parseFloat(v); return Number.isFinite(f) ? f : null; };
    const min=n(e.min) ?? 0, max=n(e.max) ?? 100, step=e.step==='any' ? 1 : (n(e.step) || 1);
    return {min,max,step,value:n(e.value) ?? min};
  };
  cache.setRange=(id,v)=>{
    const e=cache.nodes.get(id);
    if (!e?.isConnected) return false;
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(e,String(v));
    e.dispatchEvent(new Event('input',{bubbles:true}));
    e.dispatchEvent(new Event('change',{bubbles:true}));
    return true;
  };
  cache.scrolled=pid=>{ const p=cache.nodes.get(pid); return p?.isConnected ? [p.scrollLeft,p.scrollTop] : null; };
  // Links inside closed menus (navigation dropdowns, menu panels, collapsed <details>) are hidden, so they are never
  // among the actions below. The agent asks for them only when it is stuck on a page; each one names its trigger
  // (`menu`), the visible control the executor hovers or clicks to open the menu before clicking the link. Controls
  // scrolled out of view inside a scroll box of their own come with them, naming their box (`panel`).
  cache.menus=()=>{
    const shown=e=>{
      if (!visible(e)) return false;
      const r=e.getBoundingClientRect(), x=r.x+r.width/2, y=r.y+r.height/2;
      if (!r.width || !r.height) return false;
      if (x<0 || y<0 || x>=innerWidth || y>=innerHeight) return true; // laid out further away: scrolling reaches it
      return e.contains(deepPoint(x,y)); // clipped by a collapsed panel, or under something
    };
    const usable=t=>!!t && visible(t) && !t.matches(':disabled') && !t.closest('[aria-disabled="true"]');
    const pairs=[]; // [trigger, the part of the page it opens]
    for (const t of deepAll('[aria-expanded="false"],[aria-haspopup]:not([aria-haspopup="false"]):not([aria-expanded])')) {
      if (!usable(t) || ['INPUT','SELECT','TEXTAREA'].includes(t.tagName) || t.getAttribute('role')==='combobox') continue;
      const panels=(t.getAttribute('aria-controls')||'').split(/\s+/).filter(Boolean).map(id=>byId(t,id)).filter(Boolean);
      for (const p of panels.length ? panels : [t.closest('li') || t.parentElement]) if (p) pairs.push([t,p]);
    }
    for (const d of deepAll('details:not([open])')) {
      const s=d.querySelector(':scope > summary');
      if (usable(s)) pairs.push([s,d]);
    }
    // Menus that open on hover with no ARIA at all (li:hover > ul): the list item's own visible control opens it.
    for (const a of deepAll('nav a[href],header a[href],[role="navigation"] a[href],[role="menubar"] a[href]')) {
      if (shown(a)) continue;
      let li=a.parentElement?.closest('li');
      while (li && !visible(li)) li=li.parentElement?.closest('li');
      const t=li && [...li.children].find(c=>!c.contains(a) && usable(c) && c.getBoundingClientRect().height>0);
      if (t) pairs.push([t,li]);
    }
    // Cards that show their link only while pointed at (a caption over a picture: "View profile"). The trigger is the
    // card: its nearest visible wrapper, if that is card-sized rather than a whole section.
    for (const a of deepAll('a[href]')) {
      if (shown(a) || a.closest('nav,header,footer,[role="navigation"],[role="menubar"],details')) continue;
      let card=a.parentElement, hops=0;
      while (card && hops<4 && !visible(card)) { card=card.parentElement; hops++; }
      if (!card || card===document.body || !usable(card) || card.contains(document.activeElement)) continue;
      const r=card.getBoundingClientRect();
      if (r.width<24 || r.height<24 || r.width*r.height>innerWidth*innerHeight/3) continue;
      pairs.push([card,card]);
    }
    // First what a scroll box hides (those controls are visible, only scrolled away), so that none of them is taken
    // for a link in a closed menu that happens to contain the box.
    const out=clipped(40), taken=new Set(out.map(o=>cache.nodes.get(o.node)));
    const limit=out.length+60;
    for (const [t,panel] of pairs) {
      const menu=(name(t)||'Menu').replace(/\s+/g,' ').trim().slice(0,60);
      for (const e of panel.querySelectorAll('a[href],[role="menuitem"],[role="link"]')) {
        if (taken.has(e) || e.contains(t) || !safe(e) || e.matches(':disabled') || shown(e)) continue;
        const rname=role(e), label=name(e).replace(/\s+/g,' ').trim();
        if (!['link','menuitem'].includes(rname) || !label) continue;
        taken.add(e);
        // menu_link: the trigger is itself a link (a hover menu's top item), so clicking it would leave the page.
        out.push({node:identity(e),role:rname,kind:'click',value:'',menu:identity(t),menu_link:t.matches('a[href]'),
          label:(menu+' › '+label).slice(0,200),guard:JSON.stringify(cache.guard(e))});
        if (out.length>=limit) return out;
      }
    }
    return out;
  };
  // Where the pointer goes first on its way from a menu's trigger to a link in the open menu: straight down from the
  // trigger, inside the menu's panel, to the link's row; then across to the link. A person moves like that, and it
  // keeps the pointer inside the menu: a diagonal can slip off it (menus close soon after the pointer leaves) or cross
  // a neighbouring menu's trigger (which opens that menu instead). The panel is the link's highest ancestor that does
  // not contain the trigger.
  cache.waypoint=(trigger,id)=>{
    const t=cache.nodes.get(trigger), e=cache.nodes.get(id);
    if (!t?.isConnected || !e?.isConnected) return null;
    let p=e;
    while (p.parentElement && !p.parentElement.contains(t)) p=p.parentElement;
    const a=t.getBoundingClientRect(), r=p.getBoundingClientRect(), l=e.getBoundingClientRect();
    if (!r.width || !r.height || p===e) return null;
    const clamp=(v,lo,hi)=>Math.min(Math.max(v,lo),Math.max(lo,hi));
    return {x:clamp(a.x+a.width/2,r.left+8,r.right-8), y:clamp(l.y+l.height/2,r.top+8,r.bottom-8)};
  };
  // The words a person reads beside a control, when the control's own name does not tell it apart from another one
  // ("Station" and "Station" are two fields a model cannot choose between; "From" and "To" are written right there).
  // Its own container's short text, or that container's aria-label, up to three levels out.
  const beside=e=>{
    for (let p=e.parentElement, hops=0; p && hops<3 && p!==document.body; p=p.parentElement, hops++) {
      const own=(p.getAttribute('aria-label')||'').replace(/\s+/g,' ').trim();
      if (own) return own.slice(0,24);
      // Its own words first, not its subtree's: a container whose field has just opened a suggestion popup would
      // otherwise read as the whole popup, and the label would change under the model mid-run.
      const mine=[...p.childNodes].filter(n=>n.nodeType===3).map(n=>n.textContent.trim()).filter(Boolean).join(' ');
      const text=(mine || p.innerText || '').replace(/\s+/g,' ').trim();
      if (text && text.length<=24) return text;
    }
    // In a table row, a list item or a card: the row's name, its first cell or first line of text (a file name, an
    // order number, a person), which is what tells "Add to starred" in one row from the one in the next.
    const row=e.closest('tr,[role="row"],li,article,[role="listitem"]');
    if (row && row!==document.body) {
      const cells=[...row.children].filter(c=>!c.contains(e));
      for (const c of cells.length ? cells : [row]) {
        const t=(c.innerText||'').split('\n').map(x=>x.trim()).find(Boolean)||'';
        if (t && t.length<=48) return t.replace(/\s+/g,' ');
      }
    }
    return '';
  };
  // What covers a control is a strip pinned to an edge of the screen (position fixed or sticky, at most a third of the
  // screen, not a dialog): scrolling moves the control out from under it.
  const pinnedStrip=(hit,e)=>{
    for (let n=hit; n && n!==document.body && n!==document.documentElement; n=n.parentElement) {
      const p=getComputedStyle(n).position;
      if (p!=='fixed' && p!=='sticky') continue;
      if (n.contains(e) || n.closest('dialog,[role="dialog"],[aria-modal="true"]')) return false;
      const r=n.getBoundingClientRect();
      return r.height<=innerHeight/3 && (r.top<=4 || r.bottom>=innerHeight-4);
    }
    return false;
  };
  // The small scroll box (a dropdown's list, a listbox: under half the screen) that has scrolled control `e` out of
  // its view, or null. Wheeling over the box brings it in (act() does, given `panel`); scrolling the page would not.
  let boxedCalls=0;
  const boxed=e=>{
    if (++boxedCalls>300) return null;
    const p=scroller(e), v=p && visible(p) && view(p);
    if (!v || p.getBoundingClientRect().height>innerHeight/2) return null;
    const r=e.getBoundingClientRect(), cx=r.x+r.width/2, cy=r.y+r.height/2;
    return cx>=v.left && cx<=v.right && cy>=v.top && cy<=v.bottom ? null : p;
  };
  const MORE=/^(load|show|see|view) (more|all)\b|^more (results|reviews|items|products|comments)\b|^(meer|mehr|plus) /i;
  let offPage=0;
  // A list's pager: its links read "2" or "Next", which says nothing out of context, so they are named "Pages › 2".
  const inPager=e=>!!e.closest('[aria-label*="aginat" i],.pagination,.pager,[class*="paginat" i]');
  const pageLabel=(e,label)=>(label.length<=16 && inPager(e) ? 'Pages › '+label : label).slice(0,200);
  const actions=[];
  const picked=[];
  for (const e of deepAll(selector)) {
    if (!safe(e) || !visible(e) || e.matches(':disabled') || e.closest('[aria-disabled="true"]')) continue;
    const r=e.getBoundingClientRect(), x=r.x+r.width/2, y=r.y+r.height/2, rname=role(e);
    if (!rname || r.width<=0 || r.height<=0) continue;
    if (rname==='gridcell' && e.querySelector('button,[role="button"]')) continue;
    if (['option','menuitem','menuitemradio'].includes(rname) && (x<0 || y<0 || x>=innerWidth || y>=innerHeight ||
        !e.contains(deepPoint(x,y)))) {
      const panel=boxed(e);
      if (panel) {
        picked.push({e,rname,panel,label:(name(e)||rname).replace(/\s+/g,' ').slice(0,200)});
        continue;
      }
    }
    if (x<0 || y<0 || x>=innerWidth || y>=innerHeight) {
      // Above or below the screen: a list's pager and its "Load more" button are offered anyway (a person scrolls to
      // them without a second thought), as `covered`: act() scrolls them to the middle of the screen first.
      if (x<0 || x>=innerWidth || offPage>=24 || !['link','button'].includes(rname)) continue;
      const own=(name(e)||'').replace(/\s+/g,' ').trim();
      if (!inPager(e) && !MORE.test(own)) continue;
      offPage++;
      picked.push({e,rname,covered:true,label:pageLabel(e,own||rname)});
      continue;
    }
    // Covered by a modal/overlay: a user cannot reach it, so it is not offered (the overlay's controls are). Covered
    // by a strip pinned to the edge of the screen (a sticky header, a cookie or ad bar), it is: a person scrolls it
    // clear, and act() does too (`covered`).
    let covered=false;
    if (!e.contains(deepPoint(x,y))) {
      if (!pinnedStrip(deepPoint(x,y), e)) continue;
      covered=true;
    }
    picked.push({e,rname,covered,label:pageLabel(e,(name(e)||rname).replace(/\s+/g,' '))});
  }
  // Clickable things with no role: an icon with a title or aria-label (a pencil "Edit" in a table row), a <div> with
  // a click handler attribute. A pointer cursor says they can be clicked; a name says what they do. The outermost
  // such element only, and never one inside (or around) a control offered above.
  const offered=new Set(picked.map(c=>c.e));
  for (const e of deepAll('[title],[aria-label],[onclick],[data-action],[data-toggle]')) {
    if (offered.has(e) || e.matches(selector) || e.closest(selector) || e.querySelector(selector)) continue;
    if (!safe(e) || !visible(e) || getComputedStyle(e).cursor!=='pointer') continue;
    if (e.parentElement && getComputedStyle(e.parentElement).cursor==='pointer' &&
        e.parentElement.matches('[title],[aria-label],[onclick],[data-action],[data-toggle]')) continue;
    const r=e.getBoundingClientRect(), x=r.x+r.width/2, y=r.y+r.height/2;
    if (r.width<6 || r.height<6 || x<0 || y<0 || x>=innerWidth || y>=innerHeight || !e.contains(deepPoint(x,y))) continue;
    const label=(e.getAttribute('aria-label')||e.getAttribute('title')||name(e)||'').replace(/\s+/g,' ').trim().slice(0,200);
    if (!label) continue;
    clickable.add(e);
    offered.add(e);
    picked.push({e,rname:'button',label});
    if (picked.length>400) break;
  }
  // A custom dropdown made of plain elements: its button is focusable (tabindex) and shows a pointer, its rows show a
  // pointer inside a popup laid over the page (positioned absolute or fixed, with a z-index). Click handlers set from script leave no
  // attribute, so these are the signs. Pointer-cursor cards in the page's own flow are not offered: they are page
  // furniture far more often than controls, and would sit in every decision. The button takes the label written
  // before it ("Language: English (US)"); a row scrolled away in the popup's list comes with its box (`panel`).
  const inPopup=e=>{
    for (let n=e.parentElement, hops=0; n && n!==document.body && hops<4; n=n.parentElement, hops++) {
      const cs=getComputedStyle(n);
      // Laid over the page (a z-index of its own), not a region a site happens to position.
      if ((cs.position==='absolute' || cs.position==='fixed') && cs.zIndex!=='auto' &&
          n.getBoundingClientRect().height<innerHeight*0.8) return true;
    }
    return false;
  };
  let scanned=0;
  for (const e of deepAll('[tabindex]:not([tabindex="-1"]),div,li,span,[data-value]').reverse()) {
    if (++scanned>4000 || picked.length>400) break;
    if (offered.has(e) || e.closest(selector) || e.querySelector(selector)) continue;
    if (getComputedStyle(e).cursor!=='pointer' || !visible(e)) continue;
    const parent=e.parentElement;
    if (parent && getComputedStyle(parent).cursor==='pointer') continue;
    const focusable=e.matches('[tabindex]:not([tabindex="-1"])');
    if (!focusable && !inPopup(e)) continue;
    const r=e.getBoundingClientRect(), x=r.x+r.width/2, y=r.y+r.height/2;
    if (r.width<6 || r.height<6) continue;
    const panel=boxed(e);
    if (!panel && (x<0 || y<0 || x>=innerWidth || y>=innerHeight || !e.contains(deepPoint(x,y)))) continue;
    const text=(e.getAttribute('aria-label')||e.getAttribute('title')||(e.innerText||'')).replace(/\s+/g,' ').trim();
    if (!text || text.length>60) continue;
    const before=focusable ? nearbyLabel(e,true) : '';
    clickable.add(e);
    offered.add(e);
    picked.push({e,rname:'button',panel,label:(before && before!==text ? before+': '+text : text).slice(0,200)});
  }
  // Controls that read alike, told apart by what is written beside them — only where that actually differs, and only
  // for the ones that have it: a label nothing distinguishes is left as it is rather than dressed up.
  const alike=new Map();
  for (const c of picked) (alike.get(c.label) || alike.set(c.label,[]).get(c.label)).push(c);
  for (const group of alike.values()) {
    if (group.length<2) continue;
    for (const c of group) c.beside=beside(c.e);
    const distinct=new Set(group.map(c=>c.beside).filter(Boolean));
    if (distinct.size<2) continue;
    for (const c of group)
      if (c.beside && !c.label.toLowerCase().includes(c.beside.toLowerCase()))
        c.label=(c.beside+' '+c.label).slice(0,200);
  }
  for (const {e,rname,label,covered,panel} of picked) {
    const base={node:identity(e),role:rname,label,...(covered ? {covered:true} : {}),
      ...(panel ? {panel:identity(panel)} : {})};
    for (const key of ['checked','selected','expanded']) {
      const value=e.getAttribute('aria-'+key);
      if (value!==null) base[key]=value;
    }
    if (['checkbox','radio'].includes(e.type)) base.checked=String(e.checked);
    const isSecret=secret(e);
    if (isSecret) base.filled=String(e.value.length>0);
    if (e.tagName==='SELECT') {
      for (const o of e.options) if (!o.selected && !o.disabled && !o.closest('optgroup[disabled]'))
        actions.push({...base,kind:'select',value:o.value,
          current_value:[...e.selectedOptions].map(o=>o.label).join(', '),label:base.label+' → '+o.label});
    } else {
      const editable=!e.readOnly && e.getAttribute('aria-readonly')!=='true' &&
        (['textbox','searchbox','spinbutton','slider'].includes(rname) ||
          (rname==='combobox' && ['INPUT','TEXTAREA'].includes(e.tagName)));
      const value=isSecret ? '' : 'value' in e ? String(e.value) :
        e.isContentEditable || rname==='combobox' ? e.innerText.trim() : '';
      actions.push({...base,kind:editable?'fill':'click',value});
      if (editable) actions.push({...base,kind:'click',value,label:'Open '+base.label});
    }
  }
  // The rows an old-style autocomplete opens under the field being typed into: plain elements with no role, no href
  // and no tabindex, so nothing above offers them. They are not decoration — a site that keeps the real value in a
  // hidden field (station and airport codes, address ids) selects nothing from the typed text alone, so a run that
  // cannot click a row can only type the same words again until its budget runs out. Offered only while a text field
  // has focus, which is the only time they exist: found by hit-testing the strip under (and over) that field, taken as
  // the innermost thing there whose pointer cursor says it can be clicked, inside a popup that overlays the page.
  const suggested=()=>{
    const field=document.activeElement;
    if (!field || field===document.body || !visible(field)) return [];
    if (!(['INPUT','TEXTAREA'].includes(field.tagName) || field.isContentEditable)) return [];
    if (field.tagName==='INPUT' && !['text','search','email','tel','url',''].includes(field.type)) return [];
    const box=field.getBoundingClientRect();
    if (!box.width || !box.height) return [];
    const taken=new Set();
    for (const a of actions) { const e=cache.nodes.get(a.node); if (e) taken.add(e); }
    const popup=e=>{ // the overlay a suggestion row sits in: a positioned ancestor, not the page's own flow
      for (let p=e, n=0; p && n<6 && p!==document.body; p=p.parentElement, n++)
        if (['absolute','fixed'].includes(getComputedStyle(p).position)) return p;
      return null;
    };
    // Find the popup by hit-testing the strip at two columns; then take its rows from the popup itself, because a
    // calendar is a grid and sampling two columns of it would offer two days out of seven.
    const panels=new Set();
    const xs=[box.left+Math.min(24,box.width/2),box.left+box.width/2];
    for (const dir of [1,-1]) for (let d=6; d<=420 && panels.size<3; d+=12) {
      const y=dir>0 ? box.bottom+d : box.top-d;
      if (y<0 || y>=innerHeight) break;
      for (const x of xs) {
        if (x<0 || x>=innerWidth) continue;
        const hit=deepPoint(x,y);
        if (!hit || hit===document.body || hit===document.documentElement || hit.contains(field)) continue;
        const p=popup(hit);
        if (!p || p.contains(field) || !visible(p) || panels.has(p)) continue;
        // A suggestion popup hangs off the field: in its column, and its top edge at the field's bottom (or its
        // bottom at the field's top). Positioned content further down the page is page content, not a popup — some
        // sites wrap whole regions in a positioned element, and walking one would mean a computed style for every
        // node in it on every snapshot.
        const r=p.getBoundingClientRect();
        if (r.right<=box.left || r.left>=box.right) continue;
        if (!((r.top>=box.bottom-8 && r.top<=box.bottom+60) || (r.bottom<=box.top+8 && r.bottom>=box.top-60))) continue;
        if (p.getElementsByTagName('*').length>400) continue;
        panels.add(p);
      }
    }
    const rows=[], seen=new Set();
    const clickable=e=>{
      if (taken.has(e) || seen.has(e) || !visible(e) || e.contains(field)) return null;
      if (getComputedStyle(e).cursor!=='pointer') return null;
      const r=e.getBoundingClientRect();
      if (r.height>120 || r.width<20 || !r.height) return null;
      // On screen, and the thing a click would actually land on at its middle (not covered, not clipped away).
      const cx=r.x+r.width/2, cy=r.y+r.height/2;
      if (cx<0 || cy<0 || cx>=innerWidth || cy>=innerHeight) return null;
      if (!e.contains(deepPoint(cx,cy))) return null;
      const text=(e.innerText||'').replace(/\s+/g,' ').trim();
      if (!text || text.length>80) return null;
      // Something already clickable inside it is already offered: that control is the way in, not this row.
      if ([...e.querySelectorAll(selector)].some(c=>taken.has(c))) return null;
      return {row:e,top:r.top,left:r.x,text};
    };
    for (const panel of panels) {
      for (const e of panel.querySelectorAll('*')) {
        if (rows.length>=24) break;
        const row=clickable(e);
        if (!row) continue;
        // The whole row, never both a row and the span inside it: whichever is outermost wins.
        if ([...seen].some(s=>s.contains(e))) continue;
        for (const had of [...seen]) if (e.contains(had)) {
          seen.delete(had);
          rows.splice(rows.findIndex(r=>r.row===had),1);
        }
        seen.add(e);
        rows.push(row);
      }
    }
    const its=picked.find(c=>c.e===field);
    const of=((its && its.label) || 'Suggestions').replace(/\s+/g,' ').trim().slice(0,60);
    if (rows.length) {
      // On the field itself, so the model is told what the page is doing: its typed text is not the value yet.
      const id=identity(field);
      for (const a of actions) if (a.node===id) a.suggestions=String(rows.length);
    }
    return rows.sort((a,b)=>a.top-b.top || a.left-b.left).map(({row,text})=>({node:identity(row),role:'option',kind:'click',value:'',
      label:(of+' › '+text).slice(0,200),guard:JSON.stringify(cache.guard(row))}));
  };
  actions.push(...suggested());
  const words=[], range=document.createRange(); let node,length=0;
  for (const root of [document.body,...shadows]) {
    const walker=document.createTreeWalker(root,NodeFilter.SHOW_TEXT);
    while ((node=walker.nextNode()) && length<6000) {
      const value=node.textContent.trim(), parent=node.parentElement;
      if (!value || !parent || parent.closest('script,style,noscript,template') || !visible(parent)) continue;
      range.selectNodeContents(node); const r=range.getBoundingClientRect();
      if (!(r.width>0 && r.height>0 && r.bottom>0 && r.top<innerHeight && r.right>0 && r.left<innerWidth)) continue;
      // Same visibility test as elements: text clipped by its container (e.g. calendar months outside a date
      // picker's window) must not reach the model, or the text claims content that has no clickable element.
      // Hit-test the first line box: a wrapped link's bounding-box center can fall between lines.
      const f=range.getClientRects()[0];
      const hit=f && deepPoint(Math.min(Math.max(f.x+f.width/2,0),innerWidth-1),
                               Math.min(Math.max(f.y+f.height/2,0),innerHeight-1));
      if (hit && parent.contains(hit)) { words.push(value); length+=value.length; }
    }
  }
  const text=words.join('\n').slice(0,6000), height=document.documentElement.scrollHeight;
  const page_key=cache.pageKey(), guards={};
  for (const a of actions) if (!(a.node in guards)) guards[a.node]=JSON.stringify(cache.guard(cache.nodes.get(a.node)));
  const marker=[performance.timeOrigin,location.href,scrollX,scrollY,innerWidth,innerHeight,
    document.title,text,actions,page_key[6]];
  const omitted_actions=Math.max(0,actions.length-250);
  actions.splice(250);
  actions.forEach((a,i)=>a.id='e'+(i+1));
  if (scrollY+innerHeight<height-2) actions.push({id:'scroll_down',kind:'scroll',label:'Scroll down',delta:560});
  if (scrollY>0) actions.push({id:'scroll_up',kind:'scroll',label:'Scroll up',delta:-560});
  // A long page (over four screens) also offers a jump to words from the goal, instead of scrolling screen by screen.
  if (height>innerHeight*4) actions.push({id:'find_text',kind:'find',
    label:'Jump to words from the goal on this long page (a section, heading or item further away)'});
  actions.push({id:'wait',kind:'wait',label:'Wait for the page to update'});
  return {url:location.href,title:document.title,ready:document.readyState,text,scroll:{y:scrollY,height,vh:innerHeight},
    actions,marker:JSON.stringify(marker),page_key:JSON.stringify(page_key),guards,omitted_actions};
})()
