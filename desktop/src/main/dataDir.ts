import { mkdirSync } from 'node:fs'
import { homedir } from 'node:os'
import { join } from 'node:path'

let cached: string | null = null

/** Desktop data dir ~/.xihe-desktop (workspaces.json, settings.json, serve.log). */
export function desktopDataDir(): string {
  if (cached) return cached
  cached = join(homedir(), '.xihe-desktop')
  mkdirSync(cached, { recursive: true })
  return cached
}
