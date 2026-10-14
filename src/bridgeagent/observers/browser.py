"""Playwright page -> UINode list.

A small DOM scan tags every visible interactive element (and h1-h3 headings)
with a `data-ba-ref` attribute and returns role / accessible name / state.
The executor targets elements with that attribute, so refs stay valid until
the next observation rewrites them.

Known gaps (documented, not hidden): cross-origin iframes and closed shadow
roots are not scanned.
"""

from __future__ import annotations

from ..tree import Observation, UINode

SCAN_JS = r"""
(maxNodes) => {
  const INTERACTIVE_TAGS = new Set(['a','button','input','select','textarea','summary','option']);
  const ROLE_FOR_TAG = {a:'link', button:'button', select:'combobox', textarea:'textbox', summary:'button',
                        option:'option', h1:'heading', h2:'heading', h3:'heading', img:'image'};
  const INPUT_ROLE = {checkbox:'checkbox', radio:'radio', submit:'button', button:'button', reset:'button',
                      file:'fileinput', range:'slider', number:'spinbutton', search:'searchbox', image:'button'};
  const text = el => (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
  function visible(el) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) return false;
    const s = getComputedStyle(el);
    if (s.visibility === 'hidden' || s.display === 'none' || s.opacity === '0') return false;
    if (r.bottom < 0 || r.right < 0 || r.top > innerHeight || r.left > innerWidth) return false;
    return true;
  }
  function accName(el) {
    const aria = el.getAttribute('aria-label'); if (aria && aria.trim()) return aria.trim();
    const lb = el.getAttribute('aria-labelledby');
    if (lb) {
      const t = lb.split(/\s+/).map(id => { const n = document.getElementById(id); return n ? text(n) : ''; }).join(' ').trim();
      if (t) return t;
    }
    if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) { const t = text(l); if (t) return t; } }
    const pl = el.closest('label'); if (pl && pl !== el) { const t = text(pl); if (t) return t; }
    for (const a of ['placeholder', 'title', 'alt', 'name']) { const v = el.getAttribute(a); if (v && v.trim()) return v.trim(); }
    if (el.tagName === 'INPUT' && ['submit', 'button', 'reset'].includes(el.type) && el.value) return el.value.trim();
    return text(el).slice(0, 120);
  }
  function role(el) {
    const r = el.getAttribute('role'); if (r) return r.split(/\s+/)[0].toLowerCase();
    const tag = el.tagName.toLowerCase();
    if (tag === 'input') return INPUT_ROLE[(el.type || 'text').toLowerCase()] || 'textbox';
    if (el.isContentEditable) return 'textbox';
    if (ROLE_FOR_TAG[tag]) return ROLE_FOR_TAG[tag];
    return (el.hasAttribute('onclick') || el.tabIndex >= 0) ? 'button' : 'generic';
  }
  function interactive(el) {
    const tag = el.tagName.toLowerCase();
    if (INTERACTIVE_TAGS.has(tag)) return true;
    if (el.hasAttribute('role')) return true;
    if (el.isContentEditable) return true;
    if (el.hasAttribute('onclick')) return true;
    const ti = el.getAttribute('tabindex'); if (ti !== null && parseInt(ti) >= 0) return true;
    return false;
  }
  document.querySelectorAll('[data-ba-ref]').forEach(e => e.removeAttribute('data-ba-ref'));
  const out = []; let i = 0;
  for (const el of document.querySelectorAll('body *')) {
    const tag = el.tagName.toLowerCase();
    const isHeading = /^h[1-3]$/.test(tag);
    if (!(interactive(el) || isHeading)) continue;
    if (!visible(el)) continue;
    const rl = role(el);
    if (!isHeading && ['presentation', 'none', 'generic'].includes(rl)) continue;
    const name = accName(el);
    if (!name && !['textbox', 'searchbox', 'combobox'].includes(rl)) continue;
    const ref = 'b' + (++i);
    el.setAttribute('data-ba-ref', ref);
    const r = el.getBoundingClientRect();
    const states = [];
    if (el.disabled || el.getAttribute('aria-disabled') === 'true') states.push('disabled');
    if (el.checked || el.getAttribute('aria-checked') === 'true') states.push('checked');
    if (el.getAttribute('aria-expanded') === 'true') states.push('expanded');
    if (el.getAttribute('aria-selected') === 'true') states.push('selected');
    if (document.activeElement === el) states.push('focused');
    let value = null;
    if ('value' in el && typeof el.value === 'string' && el.type !== 'password'
        && !['submit', 'button', 'reset'].includes(el.type)) value = el.value.slice(0, 80);
    out.push({ref, role: rl, name: name.slice(0, 120), value, states,
              bbox: [Math.round(r.left), Math.round(r.top), Math.round(r.right), Math.round(r.bottom)]});
    if (out.length >= maxNodes) break;
  }
  return out;
}
"""


TEXT_JS = (
    "(limit) => (document.body ? document.body.innerText : '').replace(/\\n{3,}/g, '\\n\\n').trim().slice(0, limit)"
)


def observe_browser(page, max_nodes: int = 250, max_text_chars: int = 2000) -> Observation:
    try:
        page.wait_for_load_state("domcontentloaded", timeout=5000)
    except Exception:
        pass
    raw = page.evaluate(SCAN_JS, max_nodes)
    try:
        page_text = page.evaluate(TEXT_JS, max_text_chars) or ""
    except Exception:
        page_text = ""
    nodes = [
        UINode(
            ref=item["ref"],
            role=item["role"],
            name=item["name"],
            surface="browser",
            bbox=tuple(item["bbox"]),
            value=item.get("value"),
            states=item.get("states", []),
            handle=f'[data-ba-ref="{item["ref"]}"]',
        )
        for item in raw
    ]
    try:
        title = page.title()
    except Exception:
        title = ""
    return Observation(surface="browser", title=title, detail=page.url, nodes=nodes, page_text=page_text)
