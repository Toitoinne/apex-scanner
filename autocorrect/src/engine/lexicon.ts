// Lexique français compact chargé à partir de public/dict/fr.txt
// (voir scripts/build-dictionary.mjs). Toutes les recherches sont en O(1).

const VERB = 1
const INFINITIVE = 2
const NOMINAL = 4

export function fold(word: string): string {
  return word
    .normalize('NFD')
    .replace(/[̀-ͯ]/g, '')
    .replace(/œ/g, 'oe')
    .replace(/æ/g, 'ae')
    .replace(/Œ/g, 'OE')
    .replace(/Æ/g, 'AE')
}

export function normalizeApostrophe(word: string): string {
  return word.replace(/[’ʼ]/g, "'")
}

export class Lexicon {
  /** valeur = fréquence * 8 + bits morphologiques */
  private readonly entries = new Map<string, number>()
  private readonly irregularPlural = new Map<string, string>()
  /** forme sans accents → formes accentuées du lexique */
  private readonly folded = new Map<string, string | string[]>()
  private readonly extra = new Set<string>()

  static parse(text: string): Lexicon {
    const lex = new Lexicon()
    let start = 0
    const len = text.length
    while (start < len) {
      let end = text.indexOf('\n', start)
      if (end === -1) end = len
      if (end > start) lex.addLine(text.slice(start, end))
      start = end + 1
    }
    return lex
  }

  /** Analyse par tranches pour ne pas bloquer l'interface (navigateur). */
  static async parseAsync(text: string, yieldEvery = 30000): Promise<Lexicon> {
    const lex = new Lexicon()
    let start = 0
    let n = 0
    const len = text.length
    while (start < len) {
      let end = text.indexOf('\n', start)
      if (end === -1) end = len
      if (end > start) lex.addLine(text.slice(start, end))
      start = end + 1
      if (++n % yieldEvery === 0) await new Promise((r) => setTimeout(r, 0))
    }
    return lex
  }

  private addLine(line: string) {
    const tab = line.indexOf('\t')
    if (tab === -1) {
      this.addWord(line, 0)
      return
    }
    const word = line.slice(0, tab)
    const rest = line.slice(tab + 1)
    const tab2 = rest.indexOf('\t')
    let tags = tab2 === -1 ? rest : rest.slice(0, tab2)
    const freq = tab2 === -1 ? 0 : Number(rest.slice(tab2 + 1)) || 0
    const colon = tags.indexOf(':')
    if (colon !== -1) {
      this.irregularPlural.set(word, tags.slice(colon + 1))
      tags = tags.slice(0, colon)
    }
    let bits = 0
    if (tags.includes('V')) bits |= VERB
    if (tags.includes('I')) bits |= INFINITIVE
    if (tags.includes('N')) bits |= NOMINAL
    this.addWord(word, freq * 8 + bits)
  }

  private addWord(word: string, value: number) {
    this.entries.set(word, value)
    const f = fold(word)
    if (f !== word) {
      const cur = this.folded.get(f)
      if (cur === undefined) this.folded.set(f, word)
      else if (typeof cur === 'string') this.folded.set(f, [cur, word])
      else cur.push(word)
    }
  }

  get size() {
    return this.entries.size
  }

  /** Ajoute des mots (dictionnaire personnel). */
  addPersonal(words: Iterable<string>) {
    for (const w of words) this.extra.add(normalizeApostrophe(w))
  }

  removePersonal(word: string) {
    this.extra.delete(normalizeApostrophe(word))
  }

  /** Le mot existe-t-il tel quel (ou en minuscule s'il commence par une majuscule) ? */
  has(word: string): boolean {
    const w = normalizeApostrophe(word)
    if (this.entries.has(w) || this.extra.has(w)) return true
    const lower = w.toLowerCase()
    if (lower !== w && (this.entries.has(lower) || this.extra.has(lower))) return true
    return false
  }

  /** Mot présent avec cette casse exacte (utile pour distinguer les noms propres). */
  hasExact(word: string): boolean {
    const w = normalizeApostrophe(word)
    return this.entries.has(w) || this.extra.has(w)
  }

  freq(word: string): number {
    const v = this.entries.get(normalizeApostrophe(word))
    return v === undefined ? 0 : Math.floor(v / 8)
  }

  private bits(word: string): number {
    return (this.entries.get(normalizeApostrophe(word)) ?? 0) & 7
  }

  isVerbForm(word: string): boolean {
    return (this.bits(word) & VERB) !== 0
  }

  isInfinitive(word: string): boolean {
    return (this.bits(word) & INFINITIVE) !== 0
  }

  /** Lemme nominal ou adjectival au singulier qui possède un pluriel distinct. */
  isNominal(word: string): boolean {
    return (this.bits(word) & NOMINAL) !== 0
  }

  plural(word: string): string | null {
    if (!this.isNominal(word)) return null
    const irr = this.irregularPlural.get(word)
    if (irr) return irr
    const s = word + 's'
    return this.entries.has(s) ? s : null
  }

  /** Formes du lexique qui ne diffèrent que par les accents. */
  accentVariants(word: string): string[] {
    const v = this.folded.get(fold(word))
    if (v === undefined) return []
    return (typeof v === 'string' ? [v] : v).filter((w) => w !== word)
  }
}
