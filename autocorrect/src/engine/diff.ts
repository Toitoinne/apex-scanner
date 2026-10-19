// Différence mot à mot entre deux versions d'une phrase. Sert à transformer
// une réponse d'IA (« phrase corrigée ») en modifications locales vérifiables :
// on ne fait jamais confiance aux positions renvoyées par un modèle.

export interface Hunk {
  start: number
  end: number
  original: string
  replacement: string
}

const TOKEN = /[ \t  ]+|\n|[\p{L}\p{M}\p{N}_'’-]+|./gu

function split(text: string): string[] {
  return text.match(TOKEN) ?? []
}

export function diffWords(a: string, b: string): Hunk[] {
  const A = split(a)
  const B = split(b)
  const n = A.length
  const m = B.length
  if (n * m > 250_000) {
    // Trop long pour une LCS complète : une seule modification globale.
    return a === b ? [] : [{ start: 0, end: a.length, original: a, replacement: b }]
  }
  const dp: Uint16Array[] = Array.from({ length: n + 1 }, () => new Uint16Array(m + 1))
  for (let i = n - 1; i >= 0; i--)
    for (let j = m - 1; j >= 0; j--)
      dp[i][j] = A[i] === B[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1])

  const hunks: Hunk[] = []
  let i = 0
  let j = 0
  let posA = 0
  let cur: { start: number; aText: string; bText: string } | null = null
  const flush = () => {
    if (cur) {
      hunks.push({ start: cur.start, end: cur.start + cur.aText.length, original: cur.aText, replacement: cur.bText })
      cur = null
    }
  }
  while (i < n || j < m) {
    if (i < n && j < m && A[i] === B[j]) {
      flush()
      posA += A[i].length
      i++
      j++
    } else if (j < m && (i === n || dp[i][j + 1] >= dp[i + 1][j])) {
      cur ??= { start: posA, aText: '', bText: '' }
      cur.bText += B[j++]
    } else {
      cur ??= { start: posA, aText: '', bText: '' }
      cur.aText += A[i]
      posA += A[i++].length
    }
  }
  flush()
  return hunks
}

export function applyHunks(text: string, hunks: Hunk[]): string {
  let out = text
  for (const h of [...hunks].sort((x, y) => y.start - x.start)) out = out.slice(0, h.start) + h.replacement + out.slice(h.end)
  return out
}

export function levenshtein(a: string, b: string): number {
  if (a === b) return 0
  const prev = new Array(b.length + 1).fill(0).map((_, k) => k)
  for (let i = 1; i <= a.length; i++) {
    let diag = prev[0]
    prev[0] = i
    for (let j = 1; j <= b.length; j++) {
      const tmp = prev[j]
      prev[j] = Math.min(prev[j] + 1, prev[j - 1] + 1, diag + (a[i - 1] === b[j - 1] ? 0 : 1))
      diag = tmp
    }
  }
  return prev[b.length]
}
