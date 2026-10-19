# Telescope Pointer — mockup

3D status view of a PlaneWave L-350 + RASA 11 inside an observer hemisphere (site 13°N 100°E).
All values are simulated from the control panel; nothing talks to PWI4 yet.

## Run

```
cd pointer-mockup
python serve.py          # http://localhost:8000   (optional: python serve.py 8080)
```

Use `serve.py`, not `python -m http.server`: on Windows the latter often serves `.js` as
`text/plain` and the browser refuses to load the modules. three.js loads from the jsDelivr CDN
(pinned 0.169.0), so the first load needs internet.

## Controls

| Item      | Knob                         | Slider                         |
|-----------|------------------------------|--------------------------------|
| Telescope | Azimuth 0–360° (N top, E right) | Altitude 0–90°              |
| Sun       | Hour angle ±12h (0h top, +W clockwise) | Declination ±23.44°  |
| Moon      | Hour angle ±12h              | Declination ±28.59°            |

Drag, mouse wheel, or arrow keys (Shift = fine, PageUp/PageDown = coarse); or type in the boxes.
The scope control sets the *target*; the simulated mount slews there per-axis (5/12/30 °/s).

## Layout

```
src/config.js            constants (site, dome size, keep-out, slew rates, colours)
src/coords.js            pure maths: alt/az <-> vector, HA/Dec -> alt/az, separations
src/store.js             shared state the view reads (mount, sun, moon in alt/az)
src/sources/mockSource.js  UI inputs + slew simulator -> store   (swap for a PWI4 source)
src/scene/*.js           hemisphere, mount model, sun/moon, pointer arrow + markers
src/ui/*.js              knob, vertical slider, control panel
```

Connecting PWI4 later = a new `sources/pwi4Source.js` that calls `store.set({ mount: {...} })`
from the backend feed, and a local ephemeris that sets `sun` / `moon`. The scene code doesn't change.

`window.__mock` exposes the source/store/camera in the browser console for testing, e.g.
`__mock.source.setScopeTarget({ alt: 30, az: 270 })`.
