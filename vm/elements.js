// Evaluated in the active tab via CDP. Returns the visible interactive elements in
// SCREEN pixels, numbered top-to-bottom; the agent draws these numbers on the
// screenshot so a small vision model can say "click 12" instead of guessing pixels.
(() => {
  const SEL = 'a[href], button, input:not([type=hidden]), textarea, select, summary, label[for],' +
    '[role=button], [role=link], [role=tab], [role=menuitem], [role=checkbox], [role=radio],' +
    '[role=option], [role=combobox], [role=searchbox], [role=textbox], [role=switch],' +
    '[contenteditable=""], [contenteditable=true], [onclick], [tabindex]:not([tabindex="-1"])';
  const vw = window.innerWidth, vh = window.innerHeight;
  // Modern pages (Chrome's New Tab page, web components) hide their controls in shadow roots, which
  // querySelectorAll does not enter; a new session's agent spent 59 steps on an invisible search box.
  const deepAll = (sel) => {
    const out = [], roots = [document];
    while (roots.length) {
      const r = roots.pop();
      out.push(...r.querySelectorAll(sel));
      for (const el of r.querySelectorAll('*')) if (el.shadowRoot) roots.push(el.shadowRoot);
    }
    return out;
  };
  // a contains b, across shadow boundaries
  const within = (a, b) => { for (let n = b; n; n = n.parentNode || n.host) if (n === a) return true; return false; };
  const deepHit = (x, y) => { let h = document.elementFromPoint(x, y);
    while (h && h.shadowRoot) { const d = h.shadowRoot.elementFromPoint(x, y); if (!d || d === h) break; h = d; } return h; };
  // Browser chrome (tabs + toolbar) sits above the viewport; the window is maximized.
  const offX = window.screenX + Math.max(0, (window.outerWidth - vw) / 2);
  const offY = window.screenY + (window.outerHeight - vh);
  const seen = new Set();
  const out = [];
  const textOf = (el) => {
    const t = el.getAttribute('aria-label') || el.getAttribute('title') || el.getAttribute('alt') ||
      (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA'
        ? (el.value ? `${el.placeholder || el.name || 'field'} = ${el.value}` : (el.placeholder || el.name || el.type))
        : el.innerText || el.textContent || '');
    return t.replace(/\s+/g, ' ').trim().slice(0, 80);
  };
  // Drawing apps (draw.io, Lucid, whiteboards) make shape tiles and canvas shapes plain divs/SVG
  // with a hand/grab/move cursor instead of buttons - include those, outermost element only.
  const CURSORS = new Set(['pointer', 'grab', 'grabbing', 'move', 'crosshair', 'copy']);
  const candidates = new Set(deepAll(SEL));
  let scanned = 0;
  for (const el of deepAll('div, span, a, li, img, svg, g, td, canvas ~ *')) {
    if (++scanned > 6000) break;
    if (candidates.has(el)) continue;
    const draggable = el.getAttribute('draggable') === 'true';
    const cur = getComputedStyle(el).cursor;
    if (!draggable && !CURSORS.has(cur)) continue;
    const parent = el.parentElement;
    if (!draggable && parent && getComputedStyle(parent).cursor === cur) continue;  // same widget, take the outer one
    const r = el.getBoundingClientRect();
    if (r.width * r.height > vw * vh * 0.25) continue;  // whole panels/canvases are not targets
    candidates.add(el);
  }
  for (const el of candidates) {
    if (el.disabled) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 4 || r.height < 4) continue;
    if (r.bottom <= 0 || r.right <= 0 || r.top >= vh || r.left >= vw) continue;
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.display === 'none' || parseFloat(cs.opacity) === 0) continue;
    // Must be the topmost thing at its centre (skips elements under modals/overlays).
    const cx = Math.min(Math.max(r.left + r.width / 2, 1), vw - 1);
    const cy = Math.min(Math.max(r.top + Math.min(r.height / 2, 20), 1), vh - 1);
    const hit = deepHit(cx, cy);
    if (!hit || !(hit === el || within(el, hit) || within(hit, el))) continue;
    // One mark per control: skip an element nested inside one already taken.
    let p = el.parentNode || el.host, nested = false;
    while (p) { if (seen.has(p)) { nested = true; break; } p = p.parentNode || p.host; }
    if (nested) continue;
    seen.add(el);
    const x = Math.max(0, r.left), y = Math.max(0, r.top);
    out.push({
      tag: el.tagName.toLowerCase() + (el.type && el.tagName === 'INPUT' ? `[${el.type}]` : ''),
      role: el.getAttribute('role') || '',
      text: textOf(el),
      x: Math.round(offX + x), y: Math.round(offY + y),
      w: Math.round(Math.min(r.right, vw) - x), h: Math.round(Math.min(r.bottom, vh) - y),
    });
  }
  out.sort((a, b) => (Math.abs(a.y - b.y) < 8 ? a.x - b.x : a.y - b.y));
  // The drawing surface: the largest visible <canvas>/<svg> (clipped to the viewport). The agent looks
  // for drawn objects inside it in the screenshot, since canvas shapes are not page elements.
  let surface = null, best = vw * vh * 0.15;
  for (const c of document.querySelectorAll('canvas, svg')) {
    if (c.tagName.toLowerCase() === 'svg' && getComputedStyle(c).pointerEvents === 'none') continue;
    // Visible part = own box clipped by the viewport and by every ancestor that clips its content
    // (draw.io's diagram is one SVG far larger than the screen inside a scrolling container).
    const r = c.getBoundingClientRect();
    let L = Math.max(0, r.left), T = Math.max(0, r.top), R = Math.min(vw, r.right), B = Math.min(vh, r.bottom);
    let clipper = null;
    for (let p = c.parentElement; p && p !== document.body; p = p.parentElement) {
      const st = getComputedStyle(p);
      if (st.overflow !== 'visible' || st.overflowX !== 'visible' || st.overflowY !== 'visible') {
        const q = p.getBoundingClientRect();
        L = Math.max(L, q.left); T = Math.max(T, q.top); R = Math.min(R, q.right); B = Math.min(B, q.bottom);
        clipper = clipper || p;
      }
    }
    const w = R - L, h = B - T;
    if (!(w > 0 && h > 0 && w * h > best)) continue;
    // Only a surface you can actually click: the topmost thing at its centre is it, inside it,
    // or its own clipping container (draw.io reports the container).
    const hit = document.elementFromPoint(L + w / 2, T + h / 2);
    if (!hit || !(hit === c || c.contains(hit) || (clipper && (hit === clipper || clipper.contains(hit))))) continue;
    best = w * h;
    surface = { x: Math.round(offX + L), y: Math.round(offY + T), w: Math.round(w), h: Math.round(h) };
  }
  return { elements: out.slice(0, 160).map((e, i) => ({ id: i + 1, ...e })), surface };
})()
