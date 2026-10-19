import { ABBREVIATIONS } from './data'

export interface Sentence {
  start: number
  end: number
  text: string
  /** se termine par une ponctuation finale */
  complete: boolean
}

/** Découpe un paragraphe en phrases (positions exactes). */
export function splitSentences(text: string): Sentence[] {
  const out: Sentence[] = []
  let start = 0
  const re = /[.!?…]+[»")\]]*(?=\s|$)/g
  let m: RegExpExecArray | null
  while ((m = re.exec(text))) {
    const end = m.index + m[0].length
    const before = text.slice(start, m.index)
    const lastWord = /([\p{L}.-]+)$/u.exec(before)?.[1] ?? ''
    if (m[0] === '.' && (ABBREVIATIONS.has(lastWord.toLowerCase()) || /^\p{Lu}$/u.test(lastWord))) continue
    push(start, end, true)
    start = end
  }
  if (start < text.length) push(start, text.length, false)
  return out

  function push(s: number, e: number, complete: boolean) {
    while (s < e && /\s/.test(text[s])) s++
    if (e > s) out.push({ start: s, end: e, text: text.slice(s, e), complete })
  }
}
