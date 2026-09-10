// Extension → monaco language id. Only the ids monaco actually registers
// (esm/vs/languages/definitions/*) — an unknown id silently disables
// tokenization, which is the correct fallback for odd files anyway.

const EXT_TO_LANG: Record<string, string> = {
  ts: 'typescript', tsx: 'typescript', mts: 'typescript',
  js: 'javascript', jsx: 'javascript', mjs: 'javascript', cjs: 'javascript',
  json: 'json', jsonc: 'json',
  py: 'python', pyw: 'python',
  java: 'java', kt: 'kotlin', kts: 'kotlin', scala: 'scala', groovy: 'clojure',
  cs: 'csharp', fs: 'fsharp', vb: 'vb',
  c: 'c', h: 'c', cpp: 'cpp', cc: 'cpp', cxx: 'cpp', hpp: 'cpp', hh: 'cpp',
  m: 'objective-c', mm: 'objective-c',
  go: 'go', rs: 'rust', rb: 'ruby', php: 'php', swift: 'swift', dart: 'dart',
  lua: 'lua', pl: 'perl', pm: 'perl', r: 'r', jl: 'julia',
  pyproj: 'ini', toml: 'ini', ini: 'ini', cfg: 'ini', conf: 'ini', properties: 'ini',
  xml: 'xml', svg: 'xml', xsl: 'xml', xslt: 'xml', csproj: 'xml', pom: 'xml',
  html: 'html', htm: 'html', vue: 'html', svelte: 'html',
  css: 'css', scss: 'scss', less: 'less',
  yaml: 'yaml', yml: 'yaml',
  md: 'markdown', markdown: 'markdown', mdx: 'mdx', rst: 'restructuredtext',
  sql: 'sql', psql: 'pgsql',
  sh: 'shell', bash: 'shell', zsh: 'shell', ps1: 'powershell', psm1: 'powershell',
  bat: 'bat', cmd: 'bat',
  dockerfile: 'dockerfile', proto: 'proto', graphql: 'graphql', gql: 'graphql',
  tf: 'hcl', hcl: 'hcl',
}

export function detectLanguage(filename: string): string | undefined {
  const base = filename.split(/[\\/]/).pop() ?? filename
  const dot = base.lastIndexOf('.')
  const ext = dot >= 0 ? base.slice(dot + 1).toLowerCase() : base.toLowerCase()
  if (ext === 'dockerfile') return 'dockerfile'
  return EXT_TO_LANG[ext]
}
