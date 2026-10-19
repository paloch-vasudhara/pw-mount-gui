// Circular knob (wrapping dial). Angle 0 = top, clockwise positive.
// value = origin + (angle / 360) * span, normalised by `wrap`.
//
// Interaction: drag (absolute — the pointer sets the angle), mouse wheel, arrow keys.
// Shift = fine step (x0.1), PageUp/PageDown = coarse (x10).

const SVGNS = 'http://www.w3.org/2000/svg';
const el = (tag, attrs = {}, parent) => {
  const n = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  if (parent) parent.appendChild(n);
  return n;
};

export function createKnob({
  span = 360, origin = 0, step = 1, value = 0,
  wrap = (v) => v, format = (v) => v.toFixed(1), labels = [], color = '#5cc8ff', ariaLabel = 'knob',
  onInput = () => {},
}) {
  const size = 132, c = size / 2, rTrack = 46;
  const svg = el('svg', { viewBox: `0 0 ${size} ${size}`, class: 'knob', role: 'slider', tabindex: '0', 'aria-label': ariaLabel });
  svg.style.setProperty('--accent', color);

  el('circle', { cx: c, cy: c, r: rTrack + 6, class: 'knob-bezel' }, svg);
  el('circle', { cx: c, cy: c, r: rTrack, class: 'knob-face' }, svg);

  for (let a = 0; a < 360; a += 10) {
    const major = a % 30 === 0;
    const r0 = rTrack - (major ? 7 : 4), rad = a * Math.PI / 180;
    el('line', {
      x1: c + r0 * Math.sin(rad), y1: c - r0 * Math.cos(rad),
      x2: c + (rTrack - 1) * Math.sin(rad), y2: c - (rTrack - 1) * Math.cos(rad),
      class: major ? 'knob-tick major' : 'knob-tick',
    }, svg);
  }
  for (const { angle, text } of labels) {
    const rad = angle * Math.PI / 180, r = rTrack + 13;
    const t = el('text', { x: c + r * Math.sin(rad), y: c - r * Math.cos(rad) + 3.5, class: 'knob-label' }, svg);
    t.textContent = text;
  }

  const arc = el('path', { class: 'knob-arc' }, svg);
  const needle = el('g', { class: 'knob-needle' }, svg);
  el('line', { x1: c, y1: c, x2: c, y2: c - rTrack + 9, class: 'knob-needle-line' }, needle);
  el('circle', { cx: c, cy: c - rTrack + 9, r: 4.5, class: 'knob-needle-dot' }, needle);
  el('circle', { cx: c, cy: c, r: 24, class: 'knob-hub' }, svg);
  const readout = el('text', { x: c, y: c + 4, class: 'knob-value' }, svg);

  let current = wrap(value);

  const angleOf = (v) => ((((v - origin) / span) * 360) % 360 + 360) % 360;

  function render() {
    const a = angleOf(current), rad = a * Math.PI / 180;
    needle.setAttribute('transform', `rotate(${a} ${c} ${c})`);
    const r = rTrack - 2;
    const x = c + r * Math.sin(rad), y = c - r * Math.cos(rad);
    arc.setAttribute('d', a < 0.01 ? '' : `M ${c} ${c - r} A ${r} ${r} 0 ${a > 180 ? 1 : 0} 1 ${x} ${y}`);
    readout.textContent = format(current);
    svg.setAttribute('aria-valuetext', format(current));
  }

  function set(v, { silent = false } = {}) {
    current = wrap(v);
    render();
    if (!silent) onInput(current);
  }

  function fromPointer(e) {
    const b = svg.getBoundingClientRect();
    const dx = e.clientX - (b.left + b.width / 2), dy = e.clientY - (b.top + b.height / 2);
    const a = ((Math.atan2(dx, -dy) * 180 / Math.PI) + 360) % 360;
    const raw = origin + (a / 360) * span;
    set(Math.round(raw / (step * 0.1)) * step * 0.1);
  }

  svg.addEventListener('pointerdown', (e) => {
    svg.setPointerCapture(e.pointerId);
    svg.classList.add('dragging');
    fromPointer(e);
  });
  svg.addEventListener('pointermove', (e) => { if (svg.hasPointerCapture(e.pointerId)) fromPointer(e); });
  const end = (e) => { if (svg.hasPointerCapture(e.pointerId)) svg.releasePointerCapture(e.pointerId); svg.classList.remove('dragging'); };
  svg.addEventListener('pointerup', end);
  svg.addEventListener('pointercancel', end);

  svg.addEventListener('wheel', (e) => {
    e.preventDefault();
    const s = e.shiftKey ? step * 0.1 : step;
    set(current + (e.deltaY < 0 ? s : -s));
  }, { passive: false });

  svg.addEventListener('keydown', (e) => {
    const s = e.shiftKey ? step * 0.1 : step;
    const map = { ArrowRight: s, ArrowUp: s, ArrowLeft: -s, ArrowDown: -s, PageUp: s * 10, PageDown: -s * 10 };
    if (e.key in map) { e.preventDefault(); set(current + map[e.key]); }
  });

  render();
  return { element: svg, set, get value() { return current; } };
}
