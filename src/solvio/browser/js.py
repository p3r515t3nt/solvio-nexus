"""Die fest verdrahteten Seitenskripte.

Alles, was im Browser ausgefuehrt wird, steht hier — als Konstante, vom Modell
nicht erreichbar. Das ist der Unterschied zwischen einer Faehigkeit und einer
Fernsteuerung: `browser_extract` liest, was auf der Seite steht; es fuehrt nicht
aus, was jemand hineinschreibt. Ein Werkzeug „JavaScript ausfuehren" gibt es
bewusst nicht, denn es waere die Faehigkeit, alle anderen Regeln zu umgehen.

Zwei Dinge, die `browser-use` teuer gelernt hat und die hier uebernommen sind,
ohne die Abhaengigkeit zu uebernehmen: Sichtbarkeit muss **gerechnet** werden
(`getComputedStyle`, Rechteckgroesse) und nicht angenommen — und ein Element ist
auch dann bedienbar, wenn nur ein Ereigniszuhoerer daran haengt, weshalb `role`,
`tabindex` und `onclick` mitzaehlen.

Eingesetzt werden in diese Vorlagen ausschliesslich Ganzzahlen (`int()` erzwingt
das). Kein Zeichen aus einem Modellargument erreicht jemals eine Seite.
"""

#: Sichtbaren Text einsammeln. Nicht `innerText` des Body: das nimmt auch
#: Navigation, Fusszeilen und Cookie-Banner mit und verliert die Struktur.
EXTRACT_TEXT = r"""
(function (limit) {
  const SKIP = new Set(['SCRIPT','STYLE','NOSCRIPT','TEMPLATE','SVG','CANVAS','IFRAME']);
  function visible(el) {
    if (!(el instanceof Element)) return true;
    // `aria-hidden` heisst: einem Menschen wird das nicht vorgelesen. Was ein
    // Mensch nicht wahrnimmt, darf das Modell nicht als Seiteninhalt lesen —
    // sonst sehen beide etwas anderes, und genau darauf zielt der Angriff.
    if (el.getAttribute('aria-hidden') === 'true') return false;
    const s = getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden' || s.visibility === 'collapse') return false;
    if (parseFloat(s.opacity || '1') === 0) return false;
    // Text der Groesse null steht im Quelltext und auf keinem Bildschirm.
    if (parseFloat(s.fontSize || '16') < 1) return false;
    // Die klassischen Verstecke: aus dem Bild geschoben, weggeschnitten.
    if (parseFloat(s.textIndent || '0') < -999) return false;
    if ((s.clipPath || '').replace(/\s/g, '') === 'inset(100%)') return false;
    if ((s.clip || '').replace(/\s/g, '') === 'rect(0px,0px,0px,0px)') return false;
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0 && el.childElementCount === 0) return false;
    return true;
  }
  const parts = [];
  let hidden = 0;
  let budget = limit;
  (function walk(node) {
    if (budget <= 0) return;
    if (node.nodeType === Node.TEXT_NODE) {
      const t = (node.textContent || '').replace(/\s+/g, ' ').trim();
      if (t) { parts.push(t); budget -= t.length; }
      return;
    }
    if (node.nodeType !== Node.ELEMENT_NODE) return;
    if (SKIP.has(node.tagName)) return;
    if (!visible(node)) { hidden += 1; return; }
    const display = getComputedStyle(node).display;
    const isBlock = display && display.indexOf('inline') !== 0;
    if (isBlock) parts.push('\n');
    for (const child of node.childNodes) walk(child);
    if (isBlock) parts.push('\n');
  })(document.body || document.documentElement);
  let text = parts.join(' ')
      .split('\n').map(function (s) { return s.replace(/\s+/g, ' ').trim(); })
      .filter(function (s) { return s; }).join('\n');
  const truncated = text.length > limit;
  if (truncated) text = text.slice(0, limit);
  return {
    title: document.title || '',
    url: location.href,
    text: text,
    truncated: truncated,
    hidden_elements: hidden,
    node_count: document.getElementsByTagName('*').length
  };
})(%LIMIT%)
"""

#: Handlungsfaehige Verweise. `href` wird absolut aufgeloest, weil ein relativer
#: Verweis spaeter gegen eine andere Basis geprueft wuerde als hier.
EXTRACT_LINKS = r"""
(function (limit) {
  const out = [];
  const seen = new Set();
  for (const a of document.querySelectorAll('a[href]')) {
    if (out.length >= limit) break;
    const s = getComputedStyle(a);
    if (s.display === 'none' || s.visibility === 'hidden') continue;
    const r = a.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) continue;
    let href = '';
    try { href = new URL(a.getAttribute('href'), location.href).href; } catch (e) { continue; }
    if (!/^https?:/i.test(href)) continue;
    const text = (a.innerText || a.textContent || '').replace(/\s+/g, ' ').trim()
                 || (a.getAttribute('aria-label') || '').trim()
                 || (a.getAttribute('title') || '').trim();
    if (!text) continue;
    const key = text + ' ' + href;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({ text: text.slice(0, 200), href: href,
               downloads: a.hasAttribute('download') });
  }
  return out;
})(%LIMIT%)
"""

#: Bedienbare Elemente mit Rolle und zugaenglichem Namen. Das ist die Sprache,
#: in der das Modell ein Ziel benennt — nicht ein erzeugter CSS-Pfad, der beim
#: naechsten Seiten-Deploy nicht mehr stimmt.
EXTRACT_TARGETS = r"""
(function (limit) {
  const CLICKABLE = 'a[href],button,input,select,textarea,summary,[role],[onclick],[tabindex]';
  const ROLE_BY_TAG = { A: 'link', BUTTON: 'button', SELECT: 'combobox',
                        TEXTAREA: 'textbox', SUMMARY: 'button' };
  function accessibleName(el) {
    const label = (el.getAttribute('aria-label') || '').trim();
    if (label) return label;
    const by = el.getAttribute('aria-labelledby');
    if (by) {
      const parts = by.split(/\s+/).map(function (id) {
        const n = document.getElementById(id);
        return n ? (n.innerText || n.textContent || '') : '';
      }).join(' ').replace(/\s+/g, ' ').trim();
      if (parts) return parts;
    }
    if (el.tagName === 'INPUT' || el.tagName === 'SELECT' || el.tagName === 'TEXTAREA') {
      if (el.id) {
        const lab = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
        if (lab) { const t = (lab.innerText || '').replace(/\s+/g, ' ').trim(); if (t) return t; }
      }
      const wrap = el.closest('label');
      if (wrap) { const t = (wrap.innerText || '').replace(/\s+/g, ' ').trim(); if (t) return t; }
      // Der Wert eines Feldes darf nur dann als Name dienen, wenn er kein
      // Geheimnis sein kann. Ein Passwortfeld mit `value` und ohne Label haette
      // sonst seinen Inhalt als „Namen" ausgewiesen — und der geht ans Modell.
      const t = (el.getAttribute('type') || 'text').toLowerCase();
      const SECRET = ['password', 'hidden'];
      const v = (SECRET.indexOf(t) < 0 ? (el.getAttribute('value') || '') : '').trim()
                || (el.getAttribute('placeholder') || '').trim();
      if (v) return v;
    }
    const text = (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
    if (text) return text;
    return (el.getAttribute('title') || el.getAttribute('name') || '').trim();
  }
  function role(el) {
    const explicit = (el.getAttribute('role') || '').trim();
    if (explicit) return explicit;
    if (el.tagName === 'INPUT') {
      const t = (el.getAttribute('type') || 'text').toLowerCase();
      if (t === 'submit' || t === 'button' || t === 'reset' || t === 'image') return 'button';
      if (t === 'checkbox') return 'checkbox';
      if (t === 'radio') return 'radio';
      if (t === 'file') return 'file';
      return 'textbox';
    }
    return ROLE_BY_TAG[el.tagName] || (el.hasAttribute('onclick') ? 'button' : 'generic');
  }
  const out = [];
  let index = 0;
  for (const el of document.querySelectorAll(CLICKABLE)) {
    if (out.length >= limit) break;
    const s = getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden') continue;
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) continue;
    if (el.disabled) continue;
    const name = accessibleName(el);
    if (!name) continue;
    index += 1;
    el.setAttribute('data-solvio-target', String(index));
    out.push({ ref: index, role: role(el), name: name.slice(0, 160),
               tag: el.tagName.toLowerCase() });
  }
  return out;
})(%LIMIT%)
"""

#: Was ein Klick auf dieses Element bewirken WUERDE. Gefragt wird die
#: DOM-Semantik, nicht die Beschriftung: „Weiter" kann ein Verweis sein und
#: „Mehr erfahren" ein Absenden-Knopf.
INSPECT_TARGET = r"""
(function (ref) {
  const el = document.querySelector('[data-solvio-target="' + ref + '"]');
  if (!el) return { found: false };
  const tag = el.tagName.toLowerCase();
  const type = (el.getAttribute('type') || '').toLowerCase();
  const form = el.closest('form');
  const submits = (
      (tag === 'button' && (type === 'submit' || type === '') && !!form) ||
      (tag === 'input' && (type === 'submit' || type === 'image')) ||
      el.hasAttribute('formaction') ||
      (!!el.getAttribute('form') && (type === 'submit' || tag === 'button')));
  const r = el.getBoundingClientRect();
  return {
    found: true, tag: tag, type: type,
    submits: submits,
    in_form: !!form,
    form_method: form ? (form.getAttribute('method') || 'get').toLowerCase() : '',
    uploads: (tag === 'input' && type === 'file'),
    downloads: el.hasAttribute('download'),
    href: (tag === 'a' ? (el.href || '') : ''),
    target_blank: (el.getAttribute('target') || '') === '_blank',
    x: r.left + r.width / 2, y: r.top + r.height / 2,
    width: r.width, height: r.height
  };
})(%REF%)
"""

#: Ein Element in den sichtbaren Bereich holen. Ohne das trifft ein Mausklick
#: auf Koordinaten, die ausserhalb des Fensters liegen.
SCROLL_TO = r"""
(function (ref) {
  const el = document.querySelector('[data-solvio-target="' + ref + '"]');
  if (!el) return false;
  el.scrollIntoView({ block: 'center', inline: 'center' });
  return true;
})(%REF%)
"""


def _integer(value) -> int:
    """Genau eine Ganzzahl — sonst gar nichts.

    `int()` allein waere zu freundlich: es macht aus `1.5` klaglos eine `1` und
    aus `"7"` eine Sieben. Beides ist hier ungewollt. Was in eine Seite
    eingesetzt wird, soll als Ganzzahl gemeint gewesen sein, nicht in eine
    verwandelt worden sein.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"page scripts take integers, not {type(value).__name__}")
    return value


def with_limit(script: str, limit: int) -> str:
    """Setzt eine Obergrenze ein."""
    return script.replace("%LIMIT%", str(_integer(limit)))


def with_ref(script: str, ref: int) -> str:
    """Setzt eine Elementnummer ein. Kein Zeichen aus einem Modellargument."""
    return script.replace("%REF%", str(_integer(ref)))
