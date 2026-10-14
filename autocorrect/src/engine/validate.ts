// Garde-fous appliqués à toute proposition de l'IA avant de toucher au texte.
// Une réponse qui viole une règle est rejetée en bloc (on préfère laisser
// une faute que modifier le sens).

import { diffWords, levenshtein, type Hunk } from './diff'
import { tokenize } from './tokenizer'

export type Mode = 'correct' | 'improve'

export interface ValidationResult {
  ok: boolean
  reason?: string
  hunks: Hunk[]
}

/** Éléments qui doivent survivre intacts : nombres, URL, e-mails, sigles, noms propres. */
export function invariants(text: string): string[] {
  const out: string[] = []
  const toks = tokenize(text)
  toks.forEach((t, i) => {
    if (t.type === 'protected' || t.type === 'number') out.push(t.text)
    else if (t.type === 'word' && /^\p{Lu}/u.test(t.text)) {
      // majuscule hors début de phrase → nom propre probable
      let k = i - 1
      while (k >= 0 && toks[k].type === 'space') k--
      const prev = k >= 0 ? toks[k] : undefined
      const sentenceStart = !prev || (prev.type === 'punct' && /[.!?…:«"]/.test(prev.text))
      if (!sentenceStart) out.push(t.text)
    }
  })
  return out.sort()
}

function sameMultiset(a: string[], b: string[]): boolean {
  if (a.length !== b.length) return false
  return a.every((x, i) => x === b[i])
}

const norm = (s: string) => s.replace(/[  ]/g, ' ').replace(/[’]/g, "'")

export function validateRewrite(original: string, proposed: string, mode: Mode): ValidationResult {
  if (proposed === original) return { ok: true, hunks: [] }
  if (!proposed.trim()) return { ok: false, reason: 'vide', hunks: [] }
  const hunks = diffWords(original, proposed)

  // Les invariants (nombres, URL, noms propres…) doivent être conservés.
  const invA = invariants(norm(original))
  const invB = invariants(norm(proposed))
  if (!sameMultiset(invA, invB)) return { ok: false, reason: 'invariants', hunks }

  const ratio = proposed.length / Math.max(1, original.length)
  if (mode === 'correct') {
    if (ratio < 0.8 || ratio > 1.25) return { ok: false, reason: 'longueur', hunks }
    for (const h of hunks) {
      const a = h.original.trim()
      const b = h.replacement.trim()
      // Une correction modifie peu de caractères : sinon, c'est une reformulation.
      const budget = Math.max(3, Math.ceil(Math.max(a.length, b.length) * 0.4))
      if (levenshtein(a.toLowerCase(), b.toLowerCase()) > budget) return { ok: false, reason: 'reformulation', hunks }
      if ((a.match(/\s+/g)?.length ?? 0) > 3 || (b.match(/\s+/g)?.length ?? 0) > 3) return { ok: false, reason: 'reformulation', hunks }
    }
    const changed = hunks.reduce((s, h) => s + Math.max(h.original.length, h.replacement.length), 0)
    if (changed > Math.max(12, original.length * 0.35)) return { ok: false, reason: 'trop-de-changements', hunks }
  } else {
    if (ratio < 0.6 || ratio > 1.4) return { ok: false, reason: 'longueur', hunks }
    // Négations : leur apparition ou disparition change le sens.
    const neg = (s: string) => (s.match(/\b(ne|n'|pas|jamais|aucun|aucune|rien|personne|plus|non|sans)\b/gi) ?? []).length
    if (neg(norm(original)) !== neg(norm(proposed))) return { ok: false, reason: 'negation', hunks }
  }
  return { ok: true, hunks }
}
