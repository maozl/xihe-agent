// xihe config.yaml read/write — the desktop's bridge to xihe's single source.
//
// xihe reads ALL of its config (model, api_key, tool toggles, …) from one
// `~/.xihe-agent/config.yaml` (single-source after the config convergence: no
// .env, no env-var override, no ${VAR} expansion). The desktop no longer
// injects LLM env into the serve child (that was silently ignored); instead it
// edits the YAML directly. This module line-patches the curated keys the GUI
// exposes, preserving comments / other sections / the rest of the secrets
// verbatim. No YAML dependency: a full js-yaml dump would strip comments, and
// ruamel isn't reliably on the internal npm mirror.
//
// Only a FIXED, curated key set is supported (max nesting depth 2) — that's all
// the panel edits. Everything else stays hand-edited in the file. The reader
// returns EFFECTIVE values: a key absent from the file falls back to the same
// default xihe itself uses (mirrored here so the UI shows what xihe will
// actually run with, never a misleading blank). api_key is NEVER returned as
// plaintext — only whether one is set (the UI masks it).

import { promises as fs } from 'fs'
import { join, dirname } from 'path'
import { homedir } from 'os'

/** Path to xihe's single config source — the same file the serve child reads
 *  (it spawns with no --config, so xihe resolves ~/.xihe-agent/config.yaml). */
export function xiheConfigPath(): string {
  return join(homedir(), '.xihe-agent', 'config.yaml')
}

type LeafType = 'string' | 'number' | 'boolean' | 'enum'

/** The 11 non-secret value fields the panel exposes. Shared by the read shape
 *  (`XiheConfig`) and the write shape (`XiheConfigPatch`) so they can't drift. */
interface XiheConfigValues {
  model?: string
  base_url?: string
  vision_model?: string
  max_iterations?: number
  compression_threshold?: number
  approvals_mode?: string // manual | auto (enum in the UI; bare word in YAML)
  redact_enabled?: boolean
  kbs_enabled?: boolean
  specialists_enabled?: boolean
  image_gen_enabled?: boolean
  tts_enabled?: boolean
}

/** Effective config surfaced to the UI. api_key is read-only indicator only —
 *  the secret never leaves main. Redeclared in renderer/src/lib/desktop.ts
 *  across the bundle boundary — keep in sync. */
export interface XiheConfig extends XiheConfigValues {
  /** true when config.yaml has a non-empty api_key. Never the key itself. */
  api_key_set?: boolean
}

/** Write patch. Presence of `api_key` writes it (empty string CLEARS the key);
 *  absence leaves the existing key line untouched. The other fields are
 *  optional — only the ones present are patched. */
export interface XiheConfigPatch extends XiheConfigValues {
  api_key?: string
}

interface ValueSpec {
  field: keyof XiheConfigValues
  /** YAML path segments, e.g. ['model'] or ['auxiliary', 'image_gen', 'enabled']. */
  path: string[]
  type: LeafType
  /** xihe's own default for this key (mirrored from xihe source). */
  default: string | number | boolean
}

// Curated key set + xihe's own defaults (verified against xihe-agent src):
//   model / base_url / api_key / vision_model / max_iterations /
//   compression_threshold → config.py load_config() defaults
//   approvals.mode        → terminal.py:168  (default 'manual')
//   redact.enabled        → redact.py:19     (default true)
//   kbs.enabled           → kbs_tool.py:32   (default false)
//   specialists.enabled   → specialist_agent_tool.py specialists_enabled() (default false)
//   auxiliary.*.enabled   → auxiliary_client.py:361,364 (default false)
const VALUE_SPECS: ValueSpec[] = [
  { field: 'model', path: ['model'], type: 'string', default: 'glm-5.1-tc' },
  { field: 'base_url', path: ['base_url'], type: 'string', default: 'https://api.openai.com/v1' },
  { field: 'vision_model', path: ['vision_model'], type: 'string', default: '' },
  { field: 'max_iterations', path: ['max_iterations'], type: 'number', default: 30 },
  { field: 'compression_threshold', path: ['compression_threshold'], type: 'number', default: 0.5 },
  { field: 'approvals_mode', path: ['approvals', 'mode'], type: 'enum', default: 'manual' },
  { field: 'redact_enabled', path: ['redact', 'enabled'], type: 'boolean', default: true },
  { field: 'kbs_enabled', path: ['kbs', 'enabled'], type: 'boolean', default: false },
  { field: 'specialists_enabled', path: ['specialists', 'enabled'], type: 'boolean', default: false },
  { field: 'image_gen_enabled', path: ['auxiliary', 'image_gen', 'enabled'], type: 'boolean', default: false },
  { field: 'tts_enabled', path: ['auxiliary', 'tts', 'enabled'], type: 'boolean', default: false },
]

/** Read effective values for the curated keys. Absent keys fall back to
 *  `DEFAULTS`; api_key is surfaced only as api_key_set. Never throws. */
export async function readXiheConfig(): Promise<XiheConfig> {
  const lines = await readLines()
  const cfg: Record<string, string | number | boolean> = {}
  for (const spec of VALUE_SPECS) {
    const raw = valueAt(lines, spec.path)
    cfg[spec.field] = raw === null ? spec.default : coerce(raw, spec.type, spec.default)
  }
  const apiKeyRaw = valueAt(lines, ['api_key'])
  cfg.api_key_set = apiKeyRaw !== null && apiKeyRaw.trim() !== ''
  return cfg as unknown as XiheConfig
}

/** Patch the curated keys into config.yaml via line-level edit (comments /
 *  other sections / untouched keys preserved). Atomic: tmp + rename. Returns
 *  false on any IO failure (the original file is left intact). Never logs
 *  values. */
export async function writeXiheConfig(patch: XiheConfigPatch): Promise<boolean> {
  let lines = await readLines()
  const sets: { path: string[]; literal: string }[] = []
  for (const spec of VALUE_SPECS) {
    if (spec.field in patch) {
      const val = patch[spec.field]
      if (val !== undefined) sets.push({ path: spec.path, literal: emitLiteral(val, spec.type) })
    }
  }
  if ('api_key' in patch && patch.api_key !== undefined) {
    sets.push({ path: ['api_key'], literal: emitLiteral(patch.api_key, 'string') })
  }
  for (const s of sets) lines = setLeaf(lines, s.path, s.literal)
  // Safety net: we only replace-in-place or insert, so line count never drops.
  // If it somehow would, refuse to write rather than ship a truncated config.
  const original = await readLines()
  if (lines.length < original.length) return false
  return writeLines(lines)
}

// YAML line helpers (indent-aware, comment-preserving).
// Convention: top-level (root) children are at column 0; a header at indent N
// has direct children at N+2 (or whatever the file actually uses — detected per
// section). ROOT_INDENT = -2 makes "child indent = parent + 2" hold at root
// (-2 + 2 = 0). Only direct children match; deeper-nested same-named keys never
// collide (e.g. top-level `model:` vs `auxiliary.vision.model:`).

const ROOT_INDENT = -2

function leadingSpaces(line: string): number {
  let n = 0
  while (n < line.length && line[n] === ' ') n++
  return n
}

/** The key part before the first `:` of a (leading-trimmed) line, or ''. */
function keyOf(trimmedStart: string): string {
  const ci = trimmedStart.indexOf(':')
  return ci < 0 ? '' : trimmedStart.slice(0, ci).trim()
}

/** Detect the indent of the first child under the header at `headerIdx`, or
 *  `headerIndent + 2` if the section is empty / has no children yet. */
function childIndentOf(lines: string[], headerIdx: number, headerIndent: number): number {
  for (let i = headerIdx + 1; i < lines.length; i++) {
    const t = lines[i].trim()
    if (t === '' || t.startsWith('#')) continue
    const ind = leadingSpaces(lines[i])
    if (ind > headerIndent) return ind // first child's actual indent
    break // first non-blank is dedented → no children
  }
  return headerIndent + 2
}

/** Find a DIRECT child `seg:` at the given child indent, scanning from `lo`
 *  until the block dedents past `childIndent`. Returns null if not found. */
function findKeyLine(
  lines: string[],
  lo: number,
  childIndent: number,
  seg: string
): { idx: number; indent: number } | null {
  for (let i = lo; i < lines.length; i++) {
    const line = lines[i]
    const t = line.trim()
    if (t === '' || t.startsWith('#')) continue
    const indent = leadingSpaces(line)
    if (indent < childIndent) break // exited the block (dedented past children)
    if (indent > childIndent) continue // grandchild — not a direct child
    if (keyOf(line.slice(indent)) === seg) return { idx: i, indent }
  }
  return null
}

/** Descend a path to its leaf. Returns the leaf's line index + indent, or null
 *  if any segment is missing. */
function descend(lines: string[], path: string[]): { idx: number; indent: number } | null {
  let lo = 0
  let childIndent = 0 // root children at column 0
  for (let i = 0; i < path.length; i++) {
    const found = findKeyLine(lines, lo, childIndent, path[i])
    if (!found) return null
    if (i === path.length - 1) return found
    lo = found.idx + 1
    childIndent = childIndentOf(lines, found.idx, found.indent)
  }
  return null
}

/** Raw scalar string at a path (after the colon, dequoted, decommented), or
 *  null if the leaf is absent. */
function valueAt(lines: string[], path: string[]): string | null {
  const d = descend(lines, path)
  return d ? extractValue(lines[d.idx]) : null
}

/** Parse the value half of a `key: value` line: strip surrounding quotes (if
 *  any) or a trailing inline comment (bare values); empty after colon → ''. */
function extractValue(line: string): string {
  const ci = line.indexOf(':')
  let v = ci >= 0 ? line.slice(ci + 1) : ''
  v = v.trim()
  if (v === '') return ''
  if (v.startsWith('"') || v.startsWith("'")) {
    const quote = v[0]
    const end = v.indexOf(quote, 1)
    return end > 0 ? v.slice(1, end) : v.slice(1)
  }
  // Bare value: a '#' that begins a comment ends the value. A value that is
  // itself just a comment (`key:   # note`, no actual value) → ''. Otherwise a
  // standard ` #` inline comment is cut.
  if (v.startsWith('#')) return ''
  const hash = v.indexOf(' #')
  if (hash >= 0) v = v.slice(0, hash)
  return v.trim()
}

/** Coerce a raw scalar to the spec type, falling back to `def` on any
 *  unparseable value. enum reads like a string (a bare word). */
function coerce(raw: string, type: LeafType, def: string | number | boolean): string | number | boolean {
  const s = raw.trim()
  if (type === 'string' || type === 'enum') return s
  if (type === 'boolean') {
    if (/^(true|yes|on)$/i.test(s)) return true
    if (/^(false|no|off)$/i.test(s)) return false
    return def
  }
  // number
  const n = Number(s)
  return Number.isFinite(n) ? n : def
}

/** Render a value as a YAML scalar literal: bool/number/enum bare; string
 *  double-quoted + escaped so api_key / base_url with special chars are safe. */
function emitLiteral(value: string | number | boolean, type: LeafType): string {
  if (type === 'string') return JSON.stringify(String(value))
  if (type === 'enum') return String(value) // bare word (manual | auto)
  if (type === 'number') return String(Number(value))
  return value ? 'true' : 'false'
}

/** Replace the leaf at `path` if present; otherwise insert it (creating any
 *  missing ancestor section headers). Returns a new lines array. */
function setLeaf(lines: string[], path: string[], literal: string): string[] {
  const existing = descend(lines, path)
  if (existing) {
    const nl = `${' '.repeat(existing.indent)}${path[path.length - 1]}: ${literal}`
    return [...lines.slice(0, existing.idx), nl, ...lines.slice(existing.idx + 1)]
  }
  // Leaf missing — find the deepest EXISTING ancestor (anchor) and insert the
  // missing tail (headers + leaf) under it.
  let lo = 0
  let childIndent = 0
  let anchorIdx = -1
  let anchorChildIndent = 0
  let depth = 0
  for (let i = 0; i < path.length - 1; i++) {
    const found = findKeyLine(lines, lo, childIndent, path[i])
    if (!found) break
    anchorIdx = found.idx
    depth = i + 1
    lo = found.idx + 1
    childIndent = childIndentOf(lines, found.idx, found.indent)
    anchorChildIndent = childIndent
  }
  const tail = path.slice(depth)
  const newLines = tail.map((seg, i) => {
    const indent = anchorChildIndent + i * 2
    return i === tail.length - 1
      ? `${' '.repeat(indent)}${seg}: ${literal}`
      : `${' '.repeat(indent)}${seg}:`
  })
  if (anchorIdx === -1) {
    // Appending at end of file (no ancestor matched). Lead with a blank line
    // unless the file already ends with one, to keep sections visually separate.
    const leadBlank = lines.length > 0 && lines[lines.length - 1].trim() !== '' ? [''] : []
    return [...lines, ...leadBlank, ...newLines]
  }
  // Insert immediately after the anchor header (becomes its first child).
  return [...lines.slice(0, anchorIdx + 1), ...newLines, ...lines.slice(anchorIdx + 1)]
}

async function readLines(): Promise<string[]> {
  try {
    const raw = await fs.readFile(xiheConfigPath(), 'utf8')
    return raw.split(/\r?\n/)
  } catch {
    return [] // missing / unreadable → treat as empty (reader yields all-defaults)
  }
}

async function writeLines(lines: string[]): Promise<boolean> {
  const path = xiheConfigPath()
  try {
    await fs.mkdir(dirname(path), { recursive: true })
    // The file holds secrets. Keep a single rolling .bak before each patch so a
    // botched edit is recoverable — same dir / user / perms as the live file,
    // overwritten every save (never accumulates).
    try {
      await fs.copyFile(path, path + '.bak')
    } catch {
      /* may not exist yet on first write */
    }
    const tmp = path + '.tmp'
    await fs.writeFile(tmp, lines.join('\n'), 'utf8')
    await fs.rename(tmp, path)
    return true
  } catch {
    return false
  }
}
