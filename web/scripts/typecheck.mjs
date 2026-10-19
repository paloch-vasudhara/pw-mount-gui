// Real typecheck gate. tsconfig.json is solution-style (`"files": []` plus
// project references), so plain `vue-tsc --noEmit` checks nothing and exits 0.
// `--build --force` actually checks; the verdict comes from the parsed output,
// not the exit code (vue-tsc --build can print errors while exiting 0).
import { spawnSync } from 'node:child_process'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const ERROR_RE = /^(.+?)\((\d+),(\d+)\): error (TS\d+): (.*)$/

const r = spawnSync(
  process.platform === 'win32' ? 'npx.cmd' : 'npx',
  ['vue-tsc', '--build', '--force'],
  { cwd: ROOT, encoding: 'utf8', shell: process.platform === 'win32' },
)
const output = `${r.stdout || ''}${r.stderr || ''}`
const errors = output.split(/\r?\n/).map(l => l.trim()).filter(l => ERROR_RE.test(l))

console.log(`typecheck: ${errors.length} error(s)`)
if (errors.length) {
  for (const e of errors) console.error(`  ${e}`)
  process.exit(1)
}
if (r.status) { console.error(output); process.exit(r.status) }
console.log('No type errors.')
