// Vertical slider: max at the top, min at the bottom. Optional reference ticks.
// Interaction: drag on track, wheel, arrow keys (Shift = fine, PageUp/Down = coarse).

export function createVSlider({
  min = 0, max = 90, step = 1, value = 0, ticks = [], color = '#5cc8ff',
  format = (v) => v.toFixed(1), ariaLabel = 'slider', onInput = () => {},
}) {
  const root = document.createElement('div');
  root.className = 'vslider';
  root.tabIndex = 0;
  root.setAttribute('role', 'slider');
  root.setAttribute('aria-label', ariaLabel);
  root.setAttribute('aria-orientation', 'vertical');
  root.setAttribute('aria-valuemin', min);
  root.setAttribute('aria-valuemax', max);
  root.style.setProperty('--accent', color);

  root.innerHTML = `
    <span class="vslider-end top">${format(max)}</span>
    <div class="vslider-track"><div class="vslider-fill"></div><div class="vslider-thumb"></div></div>
    <span class="vslider-end bottom">${format(min)}</span>`;
  const track = root.querySelector('.vslider-track');
  const fill = root.querySelector('.vslider-fill');
  const thumb = root.querySelector('.vslider-thumb');

  for (const t of ticks) {
    const tick = document.createElement('div');
    tick.className = 'vslider-tick';
    tick.style.bottom = `${((t.value - min) / (max - min)) * 100}%`;
    tick.dataset.label = t.label ?? '';
    track.appendChild(tick);
  }

  let current = value;
  const clamp = (v) => Math.min(max, Math.max(min, v));

  function render() {
    const f = (current - min) / (max - min);
    thumb.style.bottom = `${f * 100}%`;
    fill.style.height = `${f * 100}%`;
    root.setAttribute('aria-valuenow', current.toFixed(2));
    root.setAttribute('aria-valuetext', format(current));
  }

  function set(v, { silent = false } = {}) {
    current = clamp(v);
    render();
    if (!silent) onInput(current);
  }

  function fromPointer(e) {
    const b = track.getBoundingClientRect();
    const f = 1 - (e.clientY - b.top) / b.height;
    const raw = min + Math.min(1, Math.max(0, f)) * (max - min);
    set(Math.round(raw / (step * 0.1)) * step * 0.1);
  }

  root.addEventListener('pointerdown', (e) => {
    root.setPointerCapture(e.pointerId);
    root.classList.add('dragging');
    fromPointer(e);
  });
  root.addEventListener('pointermove', (e) => { if (root.hasPointerCapture(e.pointerId)) fromPointer(e); });
  const end = (e) => { if (root.hasPointerCapture(e.pointerId)) root.releasePointerCapture(e.pointerId); root.classList.remove('dragging'); };
  root.addEventListener('pointerup', end);
  root.addEventListener('pointercancel', end);

  root.addEventListener('wheel', (e) => {
    e.preventDefault();
    const s = e.shiftKey ? step * 0.1 : step;
    set(current + (e.deltaY < 0 ? s : -s));
  }, { passive: false });

  root.addEventListener('keydown', (e) => {
    const s = e.shiftKey ? step * 0.1 : step;
    const map = { ArrowUp: s, ArrowRight: s, ArrowDown: -s, ArrowLeft: -s, PageUp: s * 10, PageDown: -s * 10 };
    if (e.key in map) { e.preventDefault(); set(current + map[e.key]); }
  });

  render();
  return { element: root, set, get value() { return current; } };
}
