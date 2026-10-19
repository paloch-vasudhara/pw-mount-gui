// Minimal CDP driver: node cdp.mjs <outdir> <steps.json>
import { spawn } from 'node:child_process';
import fs from 'node:fs';
const [outDir, stepsFile] = process.argv.slice(2);
const steps = JSON.parse(fs.readFileSync(stepsFile, 'utf8'));
const chrome = spawn('C:/Program Files/Google/Chrome/Application/chrome.exe', [
  '--headless=new', '--remote-debugging-port=9333', '--use-angle=swiftshader', '--enable-unsafe-swiftshader',
  '--window-size=1440,900', `--user-data-dir=${outDir}/profile`, 'about:blank'], { stdio: 'ignore' });
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
let ws;
for (let i = 0; i < 50; i++) {
  try { const l = await (await fetch('http://127.0.0.1:9333/json/list')).json();
    const p = l.find(t => t.type === 'page'); if (p) { ws = new WebSocket(p.webSocketDebuggerUrl); break; } } catch {}
  await sleep(200);
}
await new Promise(r => ws.onopen = r);
let id = 0; const pend = new Map(); const logs = [];
ws.onmessage = (e) => { const m = JSON.parse(e.data);
  if (m.id && pend.has(m.id)) { pend.get(m.id)(m); pend.delete(m.id); }
  if (m.method === 'Runtime.consoleAPICalled') logs.push(m.params.type + ': ' + m.params.args.map(a => a.value ?? a.description).join(' '));
  if (m.method === 'Runtime.exceptionThrown') logs.push('EXCEPTION: ' + JSON.stringify(m.params.exceptionDetails.exception?.description ?? m.params.exceptionDetails.text));
  if (m.method === 'Log.entryAdded') logs.push('LOG ' + m.params.entry.level + ': ' + m.params.entry.text);
};
const send = (method, params = {}) => new Promise(r => { const i = ++id; pend.set(i, r); ws.send(JSON.stringify({ id: i, method, params })); });
await send('Runtime.enable'); await send('Log.enable'); await send('Page.enable');
await send('Emulation.setDeviceMetricsOverride', { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false });
for (const s of steps) {
  if (s.nav) { await send('Page.navigate', { url: s.nav }); await sleep(s.wait ?? 3000); }
  if (s.size) { await send('Emulation.setDeviceMetricsOverride', { width: s.size[0], height: s.size[1], deviceScaleFactor: 1, mobile: false }); await sleep(600); }
  if (s.eval) { const r = await send('Runtime.evaluate', { expression: s.eval, awaitPromise: true, returnByValue: true });
    console.log('EVAL', s.label ?? '', JSON.stringify(r.result?.result?.value ?? r.result?.exceptionDetails?.exception?.description)); await sleep(s.wait ?? 300); }
  if (s.mouse) { for (const [type,x,y] of s.mouse) { await send('Input.dispatchMouseEvent', { type, x, y, button: 'left', buttons: type==='mouseReleased'?0:1, clickCount: 1 }); await sleep(60); } await sleep(s.wait ?? 400); }
  if (s.click || s.focus || s.wheelAt) {
    const sel = s.click || s.focus || s.wheelAt[0];
    const r = await send('Runtime.evaluate', { expression: `(()=>{const e=document.querySelector(${JSON.stringify(sel)}); if(!e) return null; e.scrollIntoView({block:'center'}); const b=e.getBoundingClientRect(); return [b.left+b.width/2, b.top+b.height/2]})()`, returnByValue: true });
    const xy = r.result?.result?.value;
    if (!xy) { console.log('NOT FOUND', sel); } else {
      const [x,y] = xy;
      if (s.wheelAt) { await send('Input.dispatchMouseEvent', { type: 'mouseMoved', x, y }); for (let i=0;i<(s.wheelAt[2]||1);i++){ await send('Input.dispatchMouseEvent', { type: 'mouseWheel', x, y, deltaX: 0, deltaY: s.wheelAt[1] }); await sleep(90);} }
      else { await send('Input.dispatchMouseEvent', { type: 'mousePressed', x, y, button: 'left', buttons: 1, clickCount: 1 }); await send('Input.dispatchMouseEvent', { type: 'mouseReleased', x, y, button: 'left', buttons: 0, clickCount: 1 }); }
      if (s.focus) await send('Runtime.evaluate', { expression: `document.querySelector(${JSON.stringify(sel)}).select?.()` });
    }
    await sleep(s.wait ?? 250);
  }
  if (s.type) { for (const ch of s.type) { await send('Input.dispatchKeyEvent', { type: 'keyDown', key: ch, text: ch, unmodifiedText: ch }); await send('Input.dispatchKeyEvent', { type: 'keyUp', key: ch }); await sleep(40); } await sleep(s.wait ?? 250); }
  if (s.keys) { for (const [key, code, vk] of s.keys) { await send('Input.dispatchKeyEvent', { type: 'rawKeyDown', key, code, windowsVirtualKeyCode: vk, nativeVirtualKeyCode: vk }); await send('Input.dispatchKeyEvent', { type: 'keyUp', key, code, windowsVirtualKeyCode: vk, nativeVirtualKeyCode: vk }); await sleep(80); } await sleep(s.wait ?? 250); }
  if (s.shot) { const r = await send('Page.captureScreenshot', { format: 'png' }); fs.writeFileSync(`${outDir}/${s.shot}.png`, Buffer.from(r.result.data, 'base64')); console.log('SHOT', s.shot); }
}
console.log('--- console/log ---\n' + logs.join('\n'));
ws.close(); chrome.kill();
process.exit(0);
