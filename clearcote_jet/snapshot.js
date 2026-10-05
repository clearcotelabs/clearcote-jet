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
  const name = (e,seen=new Set()) => {
    if (!e || seen.has(e)) return '';
    seen.add(e);
    const referenced=(e.getAttribute('aria-labelledby')||'').split(/\s+/)
      .map(id=>name(byId(e,id),seen)).filter(Boolean).join(' ');
    return referenced || e.getAttribute('aria-label') ||
      [...(e.labels||[])].map(l=>name(l,seen)).filter(Boolean).join(' ') ||
      (['button','submit','reset'].includes(e.type) ? e.value : '') || e.getAttribute('alt') ||
      (e.tagName==='INPUT' ? '' : [...e.childNodes].map(n=>n.nodeType===3 ? n.textContent :
        n.nodeType===1 && n.getAttribute('aria-hidden')!=='true' ? name(n,seen) : '').join(' ').trim()) ||
      e.getAttribute('title') || e.getAttribute('placeholder') || '';
  };
  const roles=['button','link','checkbox','radio','switch','tab','menuitem','menuitemradio',
    'option','gridcell','combobox','textbox','searchbox','spinbutton'];
  const selector='a[href],button,input,textarea,select,summary,[contenteditable="true"],'+
    roles.map(role=>'[role="'+role+'"]').join(',');
  const role = e => {
    const explicit=e.getAttribute('role');
    if (roles.includes(explicit)) return explicit;
    if (e.tagName==='BUTTON' || e.tagName==='SUMMARY') return 'button';
    if (e.tagName==='A') return 'link';
    if (e.tagName==='SELECT') return 'combobox';
    if (e.tagName==='TEXTAREA' || e.isContentEditable) return 'textbox';
    if (e.tagName==='INPUT') {
      if (['checkbox','radio'].includes(e.type)) return e.type;
      if (['button','submit','reset','image'].includes(e.type)) return 'button';
      if (e.type==='search') return 'searchbox';
      if (e.type==='number') return 'spinbutton';
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
  // Links inside closed menus (navigation dropdowns, menu panels, collapsed <details>) are hidden, so they are never
  // among the actions below. The agent asks for them only when it is stuck on a page; each one names its trigger
  // (`menu`), the visible control the executor hovers or clicks to open the menu before clicking the link.
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
    const out=[], taken=new Set();
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
        if (out.length>=60) return out;
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
  const actions=[];
  for (const e of deepAll(selector)) {
    if (!safe(e) || !visible(e) || e.matches(':disabled') || e.closest('[aria-disabled="true"]')) continue;
    const r=e.getBoundingClientRect(), x=r.x+r.width/2, y=r.y+r.height/2, rname=role(e);
    if (!rname || r.width<=0 || r.height<=0 || x<0 || y<0 || x>=innerWidth || y>=innerHeight) continue;
    if (rname==='gridcell' && e.querySelector('button,[role="button"]')) continue;
    // Covered by a modal/overlay: a user cannot reach it, so it is not offered (the overlay's controls are).
    if (!e.contains(deepPoint(x,y))) continue;
    const base={node:identity(e),role:rname,label:(name(e)||rname).replace(/\s+/g,' ').slice(0,200)};
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
        (['textbox','searchbox','spinbutton'].includes(rname) ||
          (rname==='combobox' && ['INPUT','TEXTAREA'].includes(e.tagName)));
      const value=isSecret ? '' : 'value' in e ? String(e.value) :
        e.isContentEditable || rname==='combobox' ? e.innerText.trim() : '';
      actions.push({...base,kind:editable?'fill':'click',value});
      if (editable) actions.push({...base,kind:'click',value,label:'Open '+base.label});
    }
  }
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
