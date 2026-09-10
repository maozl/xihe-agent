/** Line-level change marks for git-style gutter decorations (IDEA-like).
 *
 * diffLineMarks(base, cur) → per-line marks on the *current* text:
 *  - 'added'    line exists only in cur
 *  - 'modified' line pairs with a removed base line (replace)
 *  - 'deleted'  base-only line(s); one mark anchored to the current line that
 *               follows the deletion (bottom line when it's at EOF)
 *
 * Algorithm: trim the common prefix/suffix (a typical edit leaves a tiny
 * middle), then LCS over what's left. Past DP_CAP the middle degrades to a
 * single replace block — an approximation the cap (≈2000×2000) only reaches
 * on wholesale rewrites of huge files.
 */

export interface LineMark {
  kind: 'added' | 'modified' | 'deleted'
  /** 1-based line in the current text. */
  line: number
}

// op codes between the two line arrays: equal / removed-from-base / added-in-cur
type Op = 0 | 1 | 2

const DP_CAP = 4_000_000

/** LCS edit script of x→y as an op sequence (0 eq, 1 del, 2 add). */
function lcsOps(x: string[], y: string[]): Op[] {
  const n = x.length
  const m = y.length
  const w = m + 1
  const dp = new Uint32Array((n + 1) * w)
  for (let i = 1; i <= n; i++) {
    const xi = x[i - 1]
    for (let j = 1; j <= m; j++) {
      dp[i * w + j] =
        xi === y[j - 1]
          ? dp[(i - 1) * w + (j - 1)] + 1
          : Math.max(dp[(i - 1) * w + j], dp[i * w + (j - 1)])
    }
  }
  const ops: Op[] = []
  let i = n
  let j = m
  while (i > 0 && j > 0) {
    if (x[i - 1] === y[j - 1]) {
      ops.push(0)
      i--
      j--
    } else if (dp[(i - 1) * w + j] >= dp[i * w + (j - 1)]) {
      ops.push(1)
      i--
    } else {
      ops.push(2)
      j--
    }
  }
  while (i > 0) {
    ops.push(1)
    i--
  }
  while (j > 0) {
    ops.push(2)
    j--
  }
  ops.reverse()
  return ops
}

export function diffLineMarks(base: string, cur: string): LineMark[] {
  if (base === cur) return []
  // '' is "no lines", not one empty line — an empty base must mark every
  // current line added, not pair-modify against a phantom line.
  const a = base === '' ? [] : base.split('\n')
  const b = cur === '' ? [] : cur.split('\n')

  let pre = 0
  while (pre < a.length && pre < b.length && a[pre] === b[pre]) pre++
  let sa = a.length
  let sb = b.length
  while (sa > pre && sb > pre && a[sa - 1] === b[sb - 1]) {
    sa--
    sb--
  }
  const ma = a.slice(pre, sa)
  const mb = b.slice(pre, sb)

  const ops =
    (ma.length + 1) * (mb.length + 1) > DP_CAP
      ? ([...Array<Op>(ma.length).fill(1), ...Array<Op>(mb.length).fill(2)] as Op[])
      : lcsOps(ma, mb)

  const marks: LineMark[] = []
  let ai = pre
  let bi = pre
  let delRun = 0
  let addRun = 0
  // A contiguous non-eq cluster = one edit: paired lines are modifications,
  // surplus adds are additions, surplus dels get one gray marker after them.
  const flush = (): void => {
    const paired = Math.min(delRun, addRun)
    for (let k = 0; k < paired; k++) marks.push({ kind: 'modified', line: bi - addRun + k + 1 })
    for (let k = paired; k < addRun; k++) marks.push({ kind: 'added', line: bi - addRun + k + 1 })
    if (delRun > addRun) {
      marks.push({ kind: 'deleted', line: Math.min(bi + 1, Math.max(b.length, 1)) })
    }
    delRun = 0
    addRun = 0
  }
  for (const op of ops) {
    if (op === 0) {
      flush()
      ai++
      bi++
    } else if (op === 1) {
      ai++
      delRun++
    } else {
      bi++
      addRun++
    }
  }
  flush()
  return marks
}
