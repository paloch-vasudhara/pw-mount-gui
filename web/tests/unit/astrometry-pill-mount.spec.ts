// The job pill is mounted once, inside the Sky Viewer frame, and positioned absolutely at its bottom-left corner.
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

const page = readFileSync(resolve(process.cwd(), 'app/pages/index.vue'), 'utf8')
const pill = readFileSync(resolve(process.cwd(), 'app/components/analysis/AstrometryJobPill.vue'), 'utf8')

describe('job pill mount', () => {
  it('is a single instance inside the Sky Viewer SectionFrame', () => {
    expect(page.match(/<AstrometryJobPill\s*\/>/g)).toHaveLength(1)
    const from = page.indexOf('<SectionFrame title="Sky Viewer">')
    const to = page.indexOf('</SectionFrame>', from)
    const at = page.indexOf('<AstrometryJobPill')
    expect(from).toBeGreaterThan(-1)
    expect(at).toBeGreaterThan(from)
    expect(at).toBeLessThan(to)
  })
  it('is absolute, 8 px from the left and bottom', () => {
    expect(pill).toMatch(/position: absolute; left: 8px; bottom: 8px;/)
    expect(pill).not.toMatch(/position: fixed/)
  })
})
