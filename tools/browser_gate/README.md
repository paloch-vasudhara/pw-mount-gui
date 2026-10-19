# Browser gate driver

Supervisor tool for verifying UI in real headless Chrome (CDP), used at task gates because
happy-dom unit tests alone missed real-browser bugs (T0.2).

```
node tools/browser_gate/cdp.mjs <out_dir_outside_repo> <steps.json>
```

- Launches Chrome (`C:/Program Files/Google/Chrome/Application/chrome.exe`) headless with SwiftShader WebGL,
  remote-debugging port **9333**, profile in `<out_dir>/profile` (delete it between runs to avoid stale caches).
- Steps (JSON array, run in order):
  - `{"nav": url, "wait": ms}`
  - `{"size": [w, h]}`
  - `{"eval": "js expression", "label": "name"}` — prints `EVAL <label> <value>` (promises awaited)
  - `{"click": css}` / `{"focus": css}` (click + `select()`) / `{"wheelAt": [css, deltaY, count]}`
  - `{"type": "chars"}` — real keyDown/keyUp with text per character
  - `{"keys": [[key, code, vk], ...]}` — e.g. `[["Tab","Tab",9]]`, `[["ArrowDown","ArrowDown",40]]`
  - `{"mouse": [[type, x, y], ...]}`
  - `{"shot": "name"}` — writes `<out_dir>/name.png`
- Prints console / log / exceptions at the end.
- Generate step files with Python `json.dump` (avoids JS/JSON escaping problems).
- reka-ui selects/popovers need **real** mouse clicks (`click`), not `element.click()` from `eval`.

`example_t02_widgets.json` is the T0.2 re-verification run against `/dev/widgets` (dev server on a throwaway port).
Always run the dev server on a throwaway port and kill it afterwards.
