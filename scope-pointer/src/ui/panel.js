// Mock control panel: one knob + vertical slider pair per item, with numeric entry and readouts.

import { createKnob } from './knob.js';
import { createVSlider } from './vslider.js';
import { fmtDeg, fmtHA, wrap360, wrapHA } from '../coords.js';
import { MOON_DEC_MAX, SUN_DEC_MAX, SUN_KEEPOUT_DEG } from '../config.js';

const signedDeg = (v) => `${v >= 0 ? '+' : '−'}${Math.abs(v).toFixed(1)}°`;

function card({ key, title, color, knob, slider, fields }) {
  const sec = document.createElement('section');
  sec.className = 'card';
  sec.dataset.kind = key;
  sec.style.setProperty('--accent', color);
  sec.innerHTML = `
    <header class="card-head">
      <span class="swatch"></span><h2>${title}</h2><span class="chip" data-role="chip"></span>
    </header>
    <div class="pair"></div>
    <div class="inputs"></div>
    <dl class="readout"></dl>`;
  const pair = sec.querySelector('.pair');
  const kWrap = document.createElement('div');
  kWrap.className = 'pair-item';
  kWrap.append(knob.element, Object.assign(document.createElement('span'), { className: 'pair-caption', textContent: fields[0].caption }));
  const sWrap = document.createElement('div');
  sWrap.className = 'pair-item';
  sWrap.append(slider.element, Object.assign(document.createElement('span'), { className: 'pair-caption', textContent: fields[1].caption }));
  pair.append(kWrap, sWrap);

  const inputs = sec.querySelector('.inputs');
  const inputEls = fields.map((f, i) => {
    const label = document.createElement('label');
    label.innerHTML = `<span>${f.label}</span><input type="number" step="${f.step}" inputmode="decimal"><em>${f.unit}</em>`;
    const input = label.querySelector('input');
    input.addEventListener('change', () => {
      const v = parseFloat(input.value);
      if (Number.isFinite(v)) (i === 0 ? knob : slider).set(v);
    });
    inputs.appendChild(label);
    return input;
  });

  return {
    element: sec,
    chip: sec.querySelector('[data-role=chip]'),
    readout: sec.querySelector('.readout'),
    inputEls,
  };
}

const rows = (dl, pairs) => {
  dl.innerHTML = pairs.map(([k, v, cls = '']) => `<dt>${k}</dt><dd class="${cls}">${v}</dd>`).join('');
};

export function createPanel(container, source) {
  const { inputs } = source;
  const cards = {};

  // --- Telescope: knob = azimuth, slider = altitude --------------------------------------
  {
    let c;
    const knob = createKnob({
      value: inputs.scope.az, span: 360, step: 1, wrap: wrap360, color: '#5cc8ff', ariaLabel: 'Telescope azimuth',
      format: (v) => `${v.toFixed(1)}°`,
      labels: [{ angle: 0, text: 'N' }, { angle: 90, text: 'E' }, { angle: 180, text: 'S' }, { angle: 270, text: 'W' }],
      onInput: (v) => { source.setScopeTarget({ az: v }); c.inputEls[0].value = v.toFixed(2); },
    });
    const slider = createVSlider({
      min: 0, max: 90, step: 1, value: inputs.scope.alt, color: '#5cc8ff', ariaLabel: 'Telescope altitude',
      format: (v) => `${v.toFixed(0)}°`, ticks: [{ value: 30 }, { value: 60 }],
      onInput: (v) => { source.setScopeTarget({ alt: v }); c.inputEls[1].value = v.toFixed(2); },
    });
    c = card({
      key: 'scope', title: 'Telescope', color: '#5cc8ff', knob, slider,
      fields: [
        { label: 'Az', unit: '°', step: 0.1, caption: 'Azimuth' },
        { label: 'Alt', unit: '°', step: 0.1, caption: 'Altitude' },
      ],
    });
    c.inputEls[0].value = inputs.scope.az.toFixed(2);
    c.inputEls[1].value = inputs.scope.alt.toFixed(2);
    Object.assign(c, { knob, slider });
    cards.scope = c;
  }

  // --- Sun / Moon: knob = hour angle, slider = declination -------------------------------
  const bodyCard = (key, title, color, decMax) => {
    let c;
    const knob = createKnob({
      value: inputs[key].ha, span: 24, step: 0.1, wrap: wrapHA, color, ariaLabel: `${title} hour angle`,
      format: fmtHA,
      labels: [{ angle: 0, text: '0h' }, { angle: 90, text: '+6' }, { angle: 180, text: '12h' }, { angle: 270, text: '−6' }],
      onInput: (v) => { source.setBody(key, { ha: v }); c.inputEls[0].value = v.toFixed(3); },
    });
    const slider = createVSlider({
      min: -decMax, max: decMax, step: 0.5, value: inputs[key].dec, color, ariaLabel: `${title} declination`,
      format: signedDeg, ticks: [{ value: 0 }],
      onInput: (v) => { source.setBody(key, { dec: v }); c.inputEls[1].value = v.toFixed(2); },
    });
    c = card({
      key, title, color, knob, slider,
      fields: [
        { label: 'HA', unit: 'h', step: 0.05, caption: 'Hour angle (+W)' },
        { label: 'Dec', unit: '°', step: 0.1, caption: 'Declination' },
      ],
    });
    c.inputEls[0].value = inputs[key].ha.toFixed(3);
    c.inputEls[1].value = inputs[key].dec.toFixed(2);
    return c;
  };
  cards.sun = bodyCard('sun', 'Sun', '#ffc94a', SUN_DEC_MAX);
  cards.moon = bodyCard('moon', 'Moon', '#c9d3e6', MOON_DEC_MAX);

  container.append(cards.scope.element, cards.sun.element, cards.moon.element);

  const chip = (el, text, tone) => { el.textContent = text; el.dataset.tone = tone; };

  return {
    /** Refresh readouts from store data + derived values. */
    update(data, d) {
      const m = data.mount;
      // Keep controls in sync when the target is set from elsewhere (code, future sky-map click).
      const sc = cards.scope;
      if (Math.abs(sc.knob.value - m.targetAz) > 1e-6) {
        sc.knob.set(m.targetAz, { silent: true });
        sc.inputEls[0].value = m.targetAz.toFixed(2);
      }
      if (Math.abs(sc.slider.value - m.targetAlt) > 1e-6) {
        sc.slider.set(m.targetAlt, { silent: true });
        sc.inputEls[1].value = m.targetAlt.toFixed(2);
      }
      chip(cards.scope.chip, d.inZone ? 'IN SUN ZONE' : m.isSlewing ? 'SLEWING' : 'ON TARGET',
        d.inZone ? 'danger' : m.isSlewing ? 'busy' : 'ok');
      rows(cards.scope.readout, [
        ['Current', `Az ${fmtDeg(m.az)} · Alt ${fmtDeg(m.alt)}`],
        ['Target', `Az ${fmtDeg(m.targetAz)} · Alt ${fmtDeg(m.targetAlt)}`, d.targetInZone ? 'danger' : ''],
        ['To target', fmtDeg(d.distToTarget)],
        [`Sun dist (≥${SUN_KEEPOUT_DEG}°)`, fmtDeg(d.sunDist), d.inZone ? 'danger' : ''],
      ]);

      const body = (c, b) => {
        const up = b.alt > 0;
        chip(c.chip, up ? 'ABOVE HORIZON' : 'BELOW HORIZON', up ? 'ok' : 'muted');
        return [['Alt / Az', `${fmtDeg(b.alt)} · ${fmtDeg(b.az)}`]];
      };
      rows(cards.sun.readout, body(cards.sun, data.sun));
      rows(cards.moon.readout, [...body(cards.moon, data.moon), ['Illuminated', `${(d.moonIllum * 100).toFixed(0)} %`]]);
    },
  };
}
