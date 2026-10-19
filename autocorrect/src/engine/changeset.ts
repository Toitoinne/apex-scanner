// Composition de plusieurs séries de modifications successives, exprimée
// au final par rapport au texte d'origine.

export interface EditMeta {
  rule: string
  category: Category
}

export type Category =
  | 'orthographe'
  | 'grammaire'
  | 'conjugaison'
  | 'accord'
  | 'ponctuation'
  | 'typographie'
  | 'style'

export interface Edit {
  /** positions dans le texte d'origine */
  start: number
  end: number
  original: string
  replacement: string
  rules: EditMeta[]
}

interface Piece {
  oStart: number
  oEnd: number
  text: string
  changed: boolean
  rules: EditMeta[]
}

export interface RangeReplace {
  start: number
  end: number
  replacement: string
  meta: EditMeta
}

export class ChangeSet {
  private pieces: Piece[]

  constructor(readonly original: string) {
    this.pieces = [{ oStart: 0, oEnd: original.length, text: original, changed: false, rules: [] }]
  }

  get text(): string {
    return this.pieces.map((p) => p.text).join('')
  }

  /** Applique des remplacements exprimés dans le texte courant (sans chevauchement). */
  apply(edits: RangeReplace[]) {
    const sorted = [...edits].sort((a, b) => b.start - a.start || b.end - a.end)
    for (const e of sorted) this.replace(e)
  }

  private replace({ start, end, replacement, meta }: RangeReplace) {
    const before: Piece[] = []
    const covered: Piece[] = []
    const after: Piece[] = []
    let cur = 0
    let cs = start
    let ce = end
    for (const p of this.pieces) {
      const ps = cur
      const pe = cur + p.text.length
      cur = pe
      const touches = start === end ? ps < start && start < pe : ps < end && pe > start
      if (!touches) {
        if (pe <= start) before.push(p)
        else after.push(p)
        continue
      }
      if (p.changed) {
        covered.push(p)
        cs = Math.min(cs, ps)
        ce = Math.max(ce, pe)
        continue
      }
      // Morceau inchangé : découpe exacte.
      if (ps < start) {
        const k = start - ps
        before.push({ ...p, oEnd: p.oStart + k, text: p.text.slice(0, k) })
      }
      const from = Math.max(start, ps)
      const to = Math.min(end, pe)
      if (to > from || start === end) {
        covered.push({
          ...p,
          oStart: p.oStart + (from - ps),
          oEnd: p.oStart + (to - ps),
          text: p.text.slice(from - ps, to - ps),
        })
      }
      if (pe > end) {
        const k = end - ps
        after.push({ ...p, oStart: p.oStart + k, text: p.text.slice(k) })
      }
    }
    const curText = this.text
    let oStart: number
    let oEnd: number
    if (covered.length) {
      oStart = covered[0].oStart
      oEnd = covered[covered.length - 1].oEnd
    } else {
      // Insertion à une frontière de morceaux.
      const left = before[before.length - 1]
      oStart = oEnd = left ? left.oEnd : 0
    }
    const text = curText.slice(cs, start) + replacement + curText.slice(end, ce)
    const rules = covered.flatMap((p) => p.rules).concat(meta)
    this.pieces = [...before, { oStart, oEnd, text, changed: true, rules }, ...after].filter(
      (p) => p.text.length > 0 || p.changed,
    )
  }

  /** Intervalle du texte d'origine couvert par l'intervalle courant [s, e). */
  toOriginalRange(s: number, e: number): [number, number] {
    const spans = this.spans()
    let os = this.original.length
    for (const { p, ps, pe } of spans) {
      if (s < pe || (s === ps && p.text.length === 0)) {
        os = p.changed ? p.oStart : p.oStart + (s - ps)
        break
      }
    }
    let oe = 0
    for (let i = spans.length - 1; i >= 0; i--) {
      const { p, ps, pe } = spans[i]
      if ((e > ps && e <= pe) || (e === ps && p.text.length === 0)) {
        oe = p.changed ? p.oEnd : p.oStart + (e - ps)
        break
      }
      if (e === 0 && ps === 0) oe = 0
    }
    return [Math.min(os, oe), Math.max(os, oe)]
  }

  private spans() {
    let cur = 0
    return this.pieces.map((p) => {
      const ps = cur
      cur += p.text.length
      return { p, ps, pe: cur }
    })
  }

  /** Position exacte dans l'original, ou null si elle tombe dans une modification. */
  toOriginal(pos: number): number | null {
    let cur = 0
    for (const p of this.pieces) {
      const ps = cur
      const pe = cur + p.text.length
      cur = pe
      if (pos >= ps && pos <= pe) {
        if (!p.changed) return p.oStart + (pos - ps)
        if (pos === ps) return p.oStart
        if (pos === pe) return p.oEnd
        return null
      }
    }
    return null
  }

  edits(): Edit[] {
    const out: Edit[] = []
    let pending: Edit | null = null
    for (const p of this.pieces) {
      if (p.changed) {
        if (pending && pending.end === p.oStart) {
          pending.end = p.oEnd
          pending.replacement += p.text
          pending.rules.push(...p.rules)
        } else {
          if (pending) out.push(pending)
          pending = { start: p.oStart, end: p.oEnd, original: '', replacement: p.text, rules: [...p.rules] }
        }
      } else if (p.text.length > 0) {
        if (pending) out.push(pending)
        pending = null
      }
    }
    if (pending) out.push(pending)
    return out
      .map((e) => ({ ...e, original: this.original.slice(e.start, e.end) }))
      .filter((e) => e.original !== e.replacement)
  }
}
