// Moteur de correction local, déterministe et sans réseau.
//
// Principe : chaque règle propose des candidats marqués « certain » ou non.
// Seuls les candidats certains, hors de la zone de frappe et non rejetés par
// l'utilisateur, sont appliqués. Les autres deviennent des « signalements »
// discrets (soulignement) sans jamais modifier le texte.

import { ChangeSet, type Category, type Edit, type EditMeta, type RangeReplace } from './changeset'
import {
  ABBREVIATIONS,
  AVOIR_FORMS,
  COMMON_ERRORS,
  COMMON_PHRASES,
  DUPLICATE_CHECK,
  GENDER,
  HYPHEN_PARTICLES,
  INFINITIVE_GOVERNORS,
  LINK_ADVERBS,
  NO_AGREEMENT_VERBS,
  NO_ELISION,
  NO_PLURAL_AFTER_DET,
  OBJECT_PRONOUNS,
  PLURAL_DETERMINERS,
  PRONOUN_BEFORE_LES,
  SUBJECT_1S,
  SUBJECT_2S,
  SUBJECT_3S,
} from './data'
import { Lexicon, normalizeApostrophe } from './lexicon'
import { tokenize, type Token } from './tokenizer'

export type { Edit, Category } from './changeset'

export interface Issue {
  start: number
  end: number
  text: string
  suggestions: string[]
  rule: string
  message: string
}

export interface AnalyzeOptions {
  /** Position du curseur dans le paragraphe (null si le curseur est ailleurs). */
  cursor?: number | null
  /** Espaces insécables avant ; : ! ? et dans les guillemets (typographie française). */
  frenchSpacing?: boolean
  /** Remplacer l'apostrophe droite par l'apostrophe typographique. */
  typographicApostrophe?: boolean
  /** Corrections refusées par l'utilisateur (Ctrl+Z) : intervalle d'origine + texte. */
  isIgnored?: (start: number, end: number, original: string, replacement: string) => boolean
  maxPasses?: number
}

export interface AnalyzeResult {
  text: string
  edits: Edit[]
  issues: Issue[]
}

interface Candidate {
  start: number
  end: number
  replacement: string
  rule: string
  category: Category
  certain: boolean
  suggestions?: string[]
  message?: string
}

const VOWEL_START = /^[aeiouyàâäéèêëîïôöùûüœæ]/i
const SENTENCE_END = new Set(['.', '!', '?', '…'])
const FR_LETTERS = 'abcdefghijklmnopqrstuvwxyzàâäçéèêëîïôöùûüÿœ'.split('')

function matchCase(original: string, replacement: string): string {
  if (!original) return replacement
  const letters = original.replace(/[^\p{L}]/gu, '')
  if (letters.length > 1 && letters === letters.toUpperCase()) return replacement.toUpperCase()
  if (original[0] !== original[0].toLowerCase()) return replacement[0].toUpperCase() + replacement.slice(1)
  return replacement
}

const isLower = (w: string) => w === w.toLowerCase()
const isCapitalized = (w: string) => w[0] !== w[0].toLowerCase()

class Context {
  /** index du prochain jeton non-espace (ou -1) */
  readonly next: number[]
  readonly prev: number[]
  readonly sentenceStart: boolean[]
  readonly sentenceId: number[]
  readonly foreign = new Set<number>()
  readonly apostrophe: string

  constructor(
    readonly text: string,
    readonly tokens: Token[],
    readonly lex: Lexicon,
  ) {
    const n = tokens.length
    this.next = new Array(n).fill(-1)
    this.prev = new Array(n).fill(-1)
    let last = -1
    for (let i = 0; i < n; i++) {
      this.prev[i] = last
      if (tokens[i].type !== 'space') last = i
    }
    last = -1
    for (let i = n - 1; i >= 0; i--) {
      this.next[i] = last
      if (tokens[i].type !== 'space') last = i
    }
    // Phrases.
    this.sentenceStart = new Array(n).fill(false)
    this.sentenceId = new Array(n).fill(0)
    let sid = 0
    let atStart = true
    for (let i = 0; i < n; i++) {
      const t = tokens[i]
      this.sentenceId[i] = sid
      if (t.type === 'space') {
        if (t.text.includes('\n')) {
          sid++
          atStart = true
        }
        continue
      }
      if (atStart && (t.type === 'word' || t.type === 'protected' || t.type === 'number')) {
        this.sentenceStart[i] = true
        atStart = false
      } else if (t.type !== 'punct') atStart = false
      if (t.type === 'punct' && SENTENCE_END.has(t.text)) {
        const p = this.prev[i]
        const prevWord = p >= 0 ? tokens[p].text.toLowerCase() : ''
        if (t.text === '.' && ABBREVIATIONS.has(prevWord)) continue
        if (t.text === '.' && p >= 0 && tokens[p].type === 'word' && tokens[p].text.length === 1 && isCapitalized(tokens[p].text)) continue // initiale : « J. Dupont »
        sid++
        atStart = true
      }
    }
    // Phrases en langue étrangère : on ne touche à rien.
    const stats = new Map<number, [number, number]>()
    for (let i = 0; i < n; i++) {
      const t = tokens[i]
      if (t.type !== 'word' || t.elision || t.text.length < 2) continue
      if (isCapitalized(t.text) && !this.sentenceStart[i]) continue
      const s = stats.get(this.sentenceId[i]) ?? [0, 0]
      s[0]++
      if (!this.known(t.text) && !COMMON_ERRORS[t.text.toLowerCase()]) s[1]++
      stats.set(this.sentenceId[i], s)
    }
    for (const [id, [total, unknown]] of stats) if (total >= 3 && unknown / total > 0.4) this.foreign.add(id)

    const curly = (text.match(/’/g) ?? []).length
    const straight = (text.match(/'/g) ?? []).length
    this.apostrophe = curly > straight ? '’' : "'"
  }

  known(word: string): boolean {
    if (this.lex.has(word)) return true
    if (word.includes('-')) {
      const parts = word.split('-')
      return parts.every((p) => p && (this.lex.has(p) || HYPHEN_PARTICLES.has(p.toLowerCase())))
    }
    return false
  }

  word(i: number): string {
    return i >= 0 ? this.tokens[i].text.toLowerCase() : ''
  }

  isWord(i: number): boolean {
    return i >= 0 && this.tokens[i].type === 'word'
  }

  /** Jeton suivant uniquement séparé par une espace simple (pas de ponctuation). */
  nextWord(i: number): number {
    const j = this.next[i]
    return this.isWord(j) && this.adjacent(i, j) ? j : -1
  }

  prevWord(i: number): number {
    const j = this.prev[i]
    return this.isWord(j) && this.adjacent(j, i) ? j : -1
  }

  /** Deux jetons consécutifs séparés seulement par des espaces (ou collés après élision). */
  adjacent(a: number, b: number): boolean {
    const between = this.text.slice(this.tokens[a].end, this.tokens[b].start)
    if (this.tokens[a].elision) return between === ''
    return /^[   ]+$/.test(between)
  }
}

// ---------------------------------------------------------------------------
// Règles

type Rule = (ctx: Context, out: Candidate[], opts: AnalyzeOptions) => void

const typography: Rule = (ctx, out, opts) => {
  const { tokens, text } = ctx
  const frSpacing = opts.frenchSpacing ?? true
  for (let i = 0; i < tokens.length; i++) {
    const t = tokens[i]
    const prev = tokens[i - 1]
    const next = tokens[i + 1]
    if (t.type === 'space' && !t.text.includes('\n')) {
      // Espaces multiples entre deux mots.
      if (/^ {2,}$/.test(t.text) && prev && next && prev.type !== 'space' && next.type !== 'space') {
        out.push({ start: t.start, end: t.end, replacement: ' ', rule: 'espaces-multiples', category: 'typographie', certain: true })
        continue
      }
      // Espace avant virgule, point, parenthèse fermante.
      if (
        prev && next && next.type === 'punct' && [',', '.', '...', '…', ')'].includes(next.text) &&
        (prev.type === 'word' || prev.type === 'number' || prev.type === 'protected') &&
        !/^\p{N}/u.test(text.slice(next.end, next.end + 1))
      ) {
        out.push({ start: t.start, end: t.end, replacement: '', rule: 'espace-avant-ponctuation', category: 'ponctuation', certain: true })
        continue
      }
      // Espace après parenthèse ouvrante.
      if (prev && prev.type === 'punct' && prev.text === '(' && next && next.type !== 'space') {
        out.push({ start: t.start, end: t.end, replacement: '', rule: 'espace-parenthese', category: 'ponctuation', certain: true })
      }
      continue
    }
    if (t.type !== 'punct') continue
    // Espace manquante après , ; ! ? suivis directement d'une lettre.
    if ([',', ';', '!', '?'].includes(t.text) && next && next.type === 'word' && prev && prev.type !== 'space') {
      out.push({ start: t.end, end: t.end, replacement: ' ', rule: 'espace-apres-ponctuation', category: 'ponctuation', certain: true })
    }
    if (!frSpacing) continue
    // Typographie française : espace fine insécable avant ; ! ?, insécable avant :
    if (['!', '?', ';', ':'].includes(t.text) && prev) {
      const nbsp = t.text === ':' ? ' ' : ' '
      if (t.text === ':' && next && (next.type === 'number' || (next.type === 'punct' && /[()\-\/]/.test(next.text)) || (next.type === 'word' && /^[DPpOo]$/.test(next.text)))) continue
      if (t.text === ':' && next && next.type === 'protected') continue
      if (t.text === ';' && next && next.type === 'punct' && next.text === ')') continue // émoticône ;)
      const before = tokens[i - 1]
      if (before.type === 'word' || before.type === 'number' || before.type === 'protected' || (before.type === 'punct' && ['»', ')', '"'].includes(before.text))) {
        out.push({ start: t.start, end: t.start, replacement: nbsp, rule: 'espace-insecable', category: 'typographie', certain: true })
      } else if (before.type === 'space' && before.text === ' ' && tokens[i - 2] && tokens[i - 2].type !== 'space' && !(tokens[i - 2].type === 'punct' && ['!', '?', ';', ':'].includes(tokens[i - 2].text))) {
        out.push({ start: before.start, end: before.end, replacement: nbsp, rule: 'espace-insecable', category: 'typographie', certain: true })
      }
    }
    if (t.text === '«' && next) {
      if (next.type === 'word' || next.type === 'number') out.push({ start: t.end, end: t.end, replacement: '\u00a0', rule: 'guillemets', category: 'typographie', certain: true })
      else if (next.type === 'space' && next.text === ' ' && tokens[i + 2] && tokens[i + 2].type !== 'space')
        out.push({ start: next.start, end: next.end, replacement: '\u00a0', rule: 'guillemets', category: 'typographie', certain: true })
    }
    if (t.text === '»' && prev) {
      if (prev.type === 'word' || prev.type === 'number' || (prev.type === 'punct' && ['.', '!', '?', '…'].includes(prev.text)))
        out.push({ start: t.start, end: t.start, replacement: '\u00a0', rule: 'guillemets', category: 'typographie', certain: true })
      else if (prev.type === 'space' && prev.text === ' ' && tokens[i - 2] && tokens[i - 2].type !== 'space')
        out.push({ start: prev.start, end: prev.end, replacement: '\u00a0', rule: 'guillemets', category: 'typographie', certain: true })
    }
  }
}

const apostrophe: Rule = (ctx, out, opts) => {
  if (!opts.typographicApostrophe) return
  for (const t of ctx.tokens) {
    if (t.type === 'word' && t.text.includes("'")) {
      out.push({ start: t.start, end: t.end, replacement: t.text.replace(/'/g, '’'), rule: 'apostrophe', category: 'typographie', certain: true })
    }
  }
}

const phrases: Rule = (ctx, out) => {
  const { tokens } = ctx
  for (let i = 0; i < tokens.length; i++) {
    if (!ctx.isWord(i) || ctx.foreign.has(ctx.sentenceId[i])) continue
    // expressions de 2 ou 3 mots
    let j = i
    for (let len = 2; len <= 3; len++) {
      j = ctx.nextWord(j)
      if (j === -1) break
      const span = ctx.text.slice(tokens[i].start, tokens[j].end)
      const norm = span.replace(/[   ]+/g, ' ')
      for (const [re, fix] of COMMON_PHRASES) {
        if (re.test(norm) && norm.toLowerCase() !== fix.toLowerCase()) {
          out.push({ start: tokens[i].start, end: tokens[j].end, replacement: matchCase(span, fix), rule: 'expression', category: 'orthographe', certain: true })
        }
      }
    }
  }
}

function edits1(word: string): string[] {
  const out: string[] = []
  for (let i = 0; i <= word.length; i++) {
    const a = word.slice(0, i)
    const b = word.slice(i)
    if (b) out.push(a + b.slice(1)) // suppression
    if (b.length > 1) out.push(a + b[1] + b[0] + b.slice(2)) // inversion
    for (const c of FR_LETTERS) {
      if (b) out.push(a + c + b.slice(1)) // substitution
      out.push(a + c + b) // insertion
    }
  }
  return out
}

function spellCandidates(lex: Lexicon, word: string): { word: string; freq: number }[] {
  const seen = new Set<string>()
  const res: { word: string; freq: number }[] = []
  for (const c of edits1(word)) {
    if (c === word || seen.has(c)) continue
    seen.add(c)
    if (lex.hasExact(c)) res.push({ word: c, freq: lex.freq(c) })
  }
  return res.sort((a, b) => b.freq - a.freq)
}

const spelling: Rule = (ctx, out) => {
  const { tokens, lex } = ctx
  for (let i = 0; i < tokens.length; i++) {
    const t = tokens[i]
    if (t.type !== 'word' || t.elision) continue
    if (ctx.foreign.has(ctx.sentenceId[i])) continue
    const w = t.text
    const lower = normalizeApostrophe(w.toLowerCase())
    const startOfSentence = ctx.sentenceStart[i]
    const proper = isCapitalized(w) && !startOfSentence

    // 1. Fautes fréquentes connues.
    const curated = COMMON_ERRORS[lower]
    if (curated && !proper && curated !== lower) {
      out.push({ start: t.start, end: t.end, replacement: matchCase(w, curated), rule: 'faute-courante', category: 'orthographe', certain: true })
      continue
    }
    if (ctx.known(w)) continue
    if (proper) continue // nom propre probable
    if (w.length < 2) continue

    const base = isCapitalized(w) ? lower : w
    if (!isLower(base)) continue

    // 2. Accents manquants ou erronés : une seule forme possible → certain.
    const variants = lex.accentVariants(base).filter((v) => isLower(v))
    if (variants.length > 0) {
      const ranked = variants.map((v) => ({ word: v, freq: lex.freq(v) })).sort((a, b) => b.freq - a.freq)
      const [top, second] = ranked
      const certain = ranked.length === 1 || (top.freq > 0 && top.freq >= 20 * (second?.freq ?? 0))
      out.push({
        start: t.start,
        end: t.end,
        replacement: matchCase(w, top.word),
        rule: 'accents',
        category: 'orthographe',
        certain,
        suggestions: ranked.slice(0, 4).map((r) => matchCase(w, r.word)),
        message: 'Accentuation',
      })
      continue
    }
    // Un mot inconnu en début de phrase peut être un nom propre : pas de correction.
    if (isCapitalized(w)) continue

    // 3. Faute de frappe (distance 1) avec un candidat nettement dominant.
    const cands = base.length >= 3 ? spellCandidates(lex, base) : []
    if (cands.length === 0) {
      out.push({ start: t.start, end: t.end, replacement: w, rule: 'inconnu', category: 'orthographe', certain: false, suggestions: [], message: 'Mot inconnu' })
      continue
    }
    const [top, second] = cands
    const certain =
      base.length >= 5 && top.freq >= 50 && (second === undefined || top.freq >= 30 * Math.max(second.freq, 1))
    out.push({
      start: t.start,
      end: t.end,
      replacement: top.word,
      rule: 'frappe',
      category: 'orthographe',
      certain,
      suggestions: cands.slice(0, 4).map((c) => c.word),
      message: 'Orthographe',
    })
  }
}

const capitalization: Rule = (ctx, out) => {
  const { tokens } = ctx
  for (let i = 0; i < tokens.length; i++) {
    if (!ctx.sentenceStart[i] || !ctx.isWord(i)) continue
    const t = tokens[i]
    if (!isLower(t.text[0]) || !/\p{L}/u.test(t.text[0])) continue
    if (ctx.foreign.has(ctx.sentenceId[i]) || !(ctx.known(t.text) || t.elision)) continue
    const p = ctx.prev[i]
    if (p === -1) {
      // Début de paragraphe : seulement s'il s'agit visiblement de prose (phrase terminée).
      const sid = ctx.sentenceId[i]
      const ended = tokens.some((x, k) => ctx.sentenceId[k] === sid && x.type === 'punct' && SENTENCE_END.has(x.text) && x.text !== '…')
      if (!ended) continue
    } else {
      const pt = tokens[p]
      if (pt.type !== 'punct' || !['.', '!', '?'].includes(pt.text)) continue
      const gap = ctx.text.slice(pt.end, t.start)
      if (!/^[  ]+$/.test(gap)) continue
    }
    out.push({ start: t.start, end: t.start + 1, replacement: t.text[0].toUpperCase(), rule: 'majuscule', category: 'typographie', certain: true })
  }
}

const elision: Rule = (ctx, out) => {
  const { tokens } = ctx
  const ap = ctx.apostrophe
  for (let i = 0; i < tokens.length; i++) {
    if (!ctx.isWord(i)) continue
    const w = ctx.word(i)
    const j = ctx.nextWord(i)
    if (j === -1 || tokens[i].elision) continue
    const nxt = tokens[j].text
    const nl = nxt.toLowerCase()
    if (!VOWEL_START.test(nxt) || NO_ELISION.has(nl)) continue
    let ok = false
    if (['je', 'me', 'te', 'se', 'ne'].includes(w)) ok = true
    else if (w === 'que') ok = ['il', 'ils', 'elle', 'elles', 'on', 'un', 'une', 'en', 'à', 'aux', 'au'].includes(nl)
    else if (w === 'si') ok = nl === 'il' || nl === 'ils'
    else if (w === 'ce') ok = ['est', 'était', 'étaient', 'en'].includes(nl)
    else if (['c', 'j', 'l', 'd', 'n', 'm', 'qu'].includes(w)) ok = ctx.known(nxt) || ctx.lex.accentVariants(nl).length > 0
    if (!ok || ctx.foreign.has(ctx.sentenceId[i])) continue
    const src = tokens[i].text
    out.push({
      start: tokens[i].start,
      end: tokens[j].start,
      replacement: (src.length === 1 || src.toLowerCase() === 'qu' ? src : src.slice(0, -1)) + ap,
      rule: 'elision',
      category: 'orthographe',
      certain: true,
    })
  }
}

const duplicates: Rule = (ctx, out) => {
  const { tokens } = ctx
  for (let i = 0; i < tokens.length; i++) {
    if (!ctx.isWord(i)) continue
    const j = ctx.nextWord(i)
    if (j === -1) continue
    const w = ctx.word(i)
    if (w === ctx.word(j) && DUPLICATE_CHECK.has(w)) {
      out.push({ start: tokens[i].end, end: tokens[j].end, replacement: '', rule: 'doublon', category: 'grammaire', certain: true })
    }
  }
}

/** Remonte depuis i en sautant les pronoms compléments ; renvoie l'index du sujet éventuel. */
function subjectBefore(ctx: Context, i: number): number {
  let k = ctx.prevWord(i)
  let guard = 0
  while (k !== -1 && guard++ < 4) {
    const w = normalizeApostrophe(ctx.word(k))
    if (SUBJECT_1S.has(w) || SUBJECT_2S.has(w) || SUBJECT_3S.has(w)) return k
    if (!OBJECT_PRONOUNS.has(w)) return -1
    k = ctx.prevWord(k)
  }
  return -1
}

const grammar: Rule = (ctx, out) => {
  const { tokens, lex } = ctx
  for (let i = 0; i < tokens.length; i++) {
    if (!ctx.isWord(i) || tokens[i].elision) continue
    if (ctx.foreign.has(ctx.sentenceId[i])) continue
    const t = tokens[i]
    const w = t.text
    if (!isLower(w)) continue

    // R1 — nom employé à la place du verbe : « je vous envoi » → « envoie ».
    if (lex.isNominal(w) && !lex.isVerbForm(w)) {
      const s = subjectBefore(ctx, i)
      if (s !== -1) {
        const subj = normalizeApostrophe(ctx.word(s))
        const endings = SUBJECT_2S.has(subj) ? ['es', 'les', 'nes', 'tes'] : ['e', 'le', 'ne', 'te']
        const forms = endings
          .map((e) => w + e)
          .filter((f) => lex.isVerbForm(f))
          .map((f) => ({ f, freq: lex.freq(f) }))
          .sort((a, b) => b.freq - a.freq)
        if (forms.length > 0 && (forms.length === 1 || forms[0].freq >= 20 * Math.max(forms[1].freq, 1))) {
          out.push({ start: t.start, end: t.end, replacement: forms[0].f, rule: 'nom-verbe', category: 'conjugaison', certain: true })
          continue
        }
      }
    }

    // R1b — « il ma dit » → « il m'a dit » (après un pronom sujet, « ma » ne peut être un possessif).
    if ((w === 'ma' || w === 'ta') && ctx.nextWord(i) !== -1) {
      const s = ctx.prevWord(i)
      if (s !== -1 && ['il', 'elle', 'on', 'qui', 'ça', 'cela'].includes(ctx.word(s)) && lex.isVerbForm(ctx.word(ctx.nextWord(i)))) {
        out.push({ start: t.start, end: t.end, replacement: w[0] + ctx.apostrophe + 'a', rule: 'elision', category: 'orthographe', certain: true })
        continue
      }
    }

    // R2 — auxiliaire avoir + infinitif en -er : « vous avez demander » → « demandé ».
    if (w.endsWith('er') && lex.isInfinitive(w)) {
      let k = ctx.prevWord(i)
      let hops = 0
      while (k !== -1 && LINK_ADVERBS.has(ctx.word(k)) && hops++ < 2) k = ctx.prevWord(k)
      const aux = ctx.word(k)
      if (k !== -1 && AVOIR_FORMS.has(aux) && aux !== 'avoir') {
        const before = normalizeApostrophe(ctx.word(ctx.prevWord(k)))
        const pronounOk =
          aux !== 'a' ||
          ['il', 'elle', 'on', 'qui', 'y', 'ça', 'cela', "n'", "qu'", "l'", "m'", "t'", "s'", 'lui', 'leur', 'nous', 'vous'].includes(before)
        const doubled = AVOIR_FORMS.has(before)
        if (pronounOk && !doubled) {
          out.push({ start: t.start, end: t.end, replacement: w.slice(0, -2) + 'é', rule: 'participe-passe', category: 'conjugaison', certain: true })
          continue
        }
      }
    }

    // R2b — verbe + participe à la place de l'infinitif : « je vais mangé » → « manger ».
    if (w.endsWith('é') && lex.isInfinitive(w.slice(0, -1) + 'er') && lex.isVerbForm(w)) {
      let k = ctx.prevWord(i)
      let hops = 0
      while (k !== -1 && hops++ < 3) {
        const pw = normalizeApostrophe(ctx.word(k))
        if (LINK_ADVERBS.has(pw) || ['me', 'te', 'se', 'nous', 'vous', 'lui', 'leur', 'y', 'en', "m'", "t'", "s'"].includes(pw)) {
          k = ctx.prevWord(k)
          continue
        }
        break
      }
      if (k !== -1 && INFINITIVE_GOVERNORS.has(ctx.word(k))) {
        out.push({ start: t.start, end: t.end, replacement: w.slice(0, -1) + 'er', rule: 'infinitif', category: 'conjugaison', certain: true })
        continue
      }
    }

    // R3 — déterminant pluriel + nom au singulier : « les document » → « documents ».
    const d = ctx.prevWord(i)
    if (d !== -1 && PLURAL_DETERMINERS.has(ctx.word(d)) && !/[sxz]$/.test(w) && !NO_PLURAL_AFTER_DET.has(w)) {
      const det = ctx.word(d)
      const beforeDet = normalizeApostrophe(ctx.word(ctx.prevWord(d)))
      const plural = lex.plural(w)
      const pronounLes = det === 'les' && PRONOUN_BEFORE_LES.has(beforeDet)
      const following = ctx.next[i] >= 0 ? tokens[ctx.next[i]] : undefined
      const compound = following && following.type === 'punct' && following.text === '-' && following.start === t.end
      if (plural && !pronounLes && !compound && !w.includes('-')) {
        out.push({ start: t.start, end: t.end, replacement: plural, rule: 'pluriel', category: 'accord', certain: true })
        continue
      }
    }
  }
}

const SINGULAR_DET = new Map<string, 'm' | 'f' | '?'>([
  ['le', 'm'], ['la', 'f'], ["l'", '?'], ['un', 'm'], ['une', 'f'], ['ce', 'm'], ['cet', 'm'], ['cette', 'f'],
  ['mon', '?'], ['ma', 'f'], ['ton', '?'], ['ta', 'f'], ['son', '?'], ['sa', 'f'], ['notre', '?'], ['votre', '?'], ['leur', '?'],
])

/** R7 — accord du participe avec le COD placé avant (« les documents que vous m'avez demandés »). */
const agreement: Rule = (ctx, out) => {
  const { tokens, lex } = ctx
  for (let i = 0; i < tokens.length; i++) {
    if (!ctx.isWord(i) || ctx.foreign.has(ctx.sentenceId[i])) continue
    const p = tokens[i].text
    const m = /^(.+)é(e?)(s?)$/.exec(p)
    if (!m || !isLower(p)) continue
    const stem = m[1]
    const inf = stem + 'er'
    if (!lex.isInfinitive(inf) || NO_AGREEMENT_VERBS.has(inf)) continue
    // Mot suivant : « à », « de » ou infinitif → participe invariable, on s'abstient.
    const nx = ctx.nextWord(i)
    if (nx !== -1) {
      const nw = normalizeApostrophe(ctx.word(nx))
      if (['à', 'de', "d'", 'que', "qu'"].includes(nw) || lex.isInfinitive(nw)) continue
    }
    let k = ctx.prevWord(i)
    let hops = 0
    while (k !== -1 && LINK_ADVERBS.has(ctx.word(k)) && hops++ < 2) k = ctx.prevWord(k)
    if (k === -1 || !AVOIR_FORMS.has(ctx.word(k)) || ctx.word(k) === 'avoir') continue
    let s = ctx.prevWord(k)
    // pronom complément d'objet indirect éventuel
    if (s !== -1 && ["m'", "t'", 'me', 'te', 'lui', 'leur', 'nous', 'vous'].includes(normalizeApostrophe(ctx.word(s)))) {
      const s2 = ctx.prevWord(s)
      if (s2 !== -1 && ['je', "j'", 'tu', 'il', 'elle', 'on', 'nous', 'vous', 'ils', 'elles'].includes(normalizeApostrophe(ctx.word(s2)))) s = s2
    }
    if (s === -1 || !['je', "j'", 'tu', 'il', 'elle', 'on', 'nous', 'vous', 'ils', 'elles'].includes(normalizeApostrophe(ctx.word(s)))) continue
    const q = ctx.prevWord(s)
    if (q === -1 || !['que', "qu'"].includes(normalizeApostrophe(ctx.word(q)))) continue
    const n = ctx.prevWord(q)
    if (n === -1) continue
    const noun = ctx.word(n)
    const d = ctx.prevWord(n)
    if (d === -1) continue
    const det = normalizeApostrophe(ctx.word(d))
    // Le groupe nominal ne doit pas être complément d'un autre nom.
    const bd = ctx.prev[d]
    const okHead =
      bd === -1 ||
      tokens[bd].type === 'punct' ||
      (tokens[bd].type === 'word' && lex.isVerbForm(tokens[bd].text.toLowerCase()) && !['de', "d'", 'du', 'des', 'à', 'au', 'aux'].includes(ctx.word(bd)))
    if (!okHead) continue
    let plural: boolean
    let gender: 'm' | 'f' | undefined
    if (PLURAL_DETERMINERS.has(det)) {
      if (!/[sx]$/.test(noun)) continue
      plural = true
      gender = GENDER.get(noun.replace(/[sx]$/, ''))
    } else if (SINGULAR_DET.has(det)) {
      plural = false
      gender = GENDER.get(noun)
      const dg = SINGULAR_DET.get(det)
      if (gender && dg !== '?' && dg !== gender) continue // incohérence : on s'abstient
    } else continue
    if (!gender) continue
    const target = stem + 'é' + (gender === 'f' ? 'e' : '') + (plural ? 's' : '')
    if (target !== p && lex.hasExact(target)) {
      out.push({ start: tokens[i].start, end: tokens[i].end, replacement: target, rule: 'accord-participe', category: 'accord', certain: true })
    }
  }
}

const RULES: Rule[] = [phrases, spelling, elision, grammar, agreement, duplicates, capitalization, typography, apostrophe]

// ---------------------------------------------------------------------------

function cursorZone(text: string, cursor: number | null | undefined, lex: Lexicon): [number, number] | null {
  if (cursor == null) return null
  const toks = tokenize(text, { isKnown: (w) => lex.has(w) })
  let zone: [number, number] | null = null
  for (const t of toks) {
    if (t.type === 'space' || t.type === 'punct') continue
    if (t.start <= cursor && cursor <= t.end) {
      zone = zone ? [Math.min(zone[0], t.start), Math.max(zone[1], t.end)] : [t.start, t.end]
    }
  }
  return zone
}

export function analyze(text: string, lex: Lexicon, opts: AnalyzeOptions = {}): AnalyzeResult {
  const cs = new ChangeSet(text)
  const zone = cursorZone(text, opts.cursor, lex)
  const maxPasses = opts.maxPasses ?? 4
  let issues: Issue[] = []

  for (let pass = 0; pass < maxPasses; pass++) {
    const cur = cs.text
    const tokens = tokenize(cur, { isKnown: (w) => lex.has(w) })
    const ctx = new Context(cur, tokens, lex)
    const candidates: Candidate[] = []
    for (const rule of RULES) rule(ctx, candidates, opts)

    const accepted: Candidate[] = []
    const pending: Candidate[] = []
    for (const c of candidates) {
      const [os, oe] = cs.toOriginalRange(c.start, c.end)
      if (zone && os <= zone[1] && oe >= zone[0]) continue
      const original = cur.slice(c.start, c.end)
      if (!c.certain) {
        pending.push(c)
        continue
      }
      if (c.replacement === original) continue
      if (opts.isIgnored?.(os, oe, text.slice(os, oe), c.replacement)) continue
      const overlaps = accepted.some((a) =>
        c.start === c.end || a.start === a.end
          ? c.start <= a.end && a.start <= c.end
          : c.start < a.end && a.start < c.end,
      )
      if (!overlaps) accepted.push(c)
    }

    if (accepted.length === 0) {
      issues = pending
        .filter((c) => !candidates.some((o) => o.certain && o !== c && o.start < c.end && c.start < o.end))
        .map((c) => {
          const s = cs.toOriginal(c.start)
          const e = cs.toOriginal(c.end)
          if (s == null || e == null) return null
          return { start: s, end: e, text: text.slice(s, e), suggestions: c.suggestions ?? [], rule: c.rule, message: c.message ?? '' }
        })
        .filter((x): x is Issue => x !== null)
      break
    }
    cs.apply(
      accepted.map<RangeReplace>((c) => ({
        start: c.start,
        end: c.end,
        replacement: c.replacement,
        meta: { rule: c.rule, category: c.category } satisfies EditMeta,
      })),
    )
  }

  return { text: cs.text, edits: cs.edits(), issues }
}
