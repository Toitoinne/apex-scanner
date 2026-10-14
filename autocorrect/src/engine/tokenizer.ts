// Découpage d'un paragraphe en jetons avec positions exactes (UTF-16, comme
// ProseMirror et les chaînes JavaScript). Les jetons « protected » ne sont
// jamais modifiés : URL, e-mails, domaines, mentions, code, sigles, mots
// contenant des chiffres ou des majuscules internes (iPhone, COVID-19…).

export type TokenType = 'word' | 'space' | 'punct' | 'number' | 'protected'

export interface Token {
  type: TokenType
  text: string
  start: number
  end: number
  /** Élision détachée : « l' », « qu' », « j' »… */
  elision?: boolean
}

const PROTECTED_PATTERNS = [
  /`[^`\n]*`?/y, // code en ligne
  /(?:https?|ftp):\/\/[^\s<>"]+/iy,
  /www\.[^\s<>"]+/iy,
  /[\p{L}\p{N}._%+-]+@[\p{L}\p{N}.-]+\.\p{L}{2,}/uy,
  /[@#][\p{L}\p{N}_]+/uy,
  // domaines, versions, abréviations à points : exemple.fr, v1.2.3, e.g.
  /[\p{L}\p{N}_-]+(?:\.[\p{L}\p{N}_-]+)+(?:\/[^\s]*)?/uy,
  // chemins de fichiers
  /(?:~|\.{1,2})?\/[\p{L}\p{N}_./-]+/uy,
]

const WORD = /[\p{L}\p{M}]+(?:[-'’][\p{L}\p{M}]+)*['’]?/uy
const NUMBER = /\p{N}+(?:[.,:h]\p{N}+)*(?:\s?%)?/uy
const SPACE = /[ \t  ]+/y
const ELISION_PREFIX = /^(l|d|j|m|t|s|n|c|qu|jusqu|lorsqu|puisqu|quoiqu|presqu)(['’])(?=\p{L})/iu

function stick(re: RegExp, text: string, pos: number): string | null {
  re.lastIndex = pos
  const m = re.exec(text)
  return m && m.index === pos && m[0].length > 0 ? m[0] : null
}

function looksTechnical(word: string): boolean {
  if (/\p{N}/u.test(word)) return true
  // majuscule interne : iPhone, JavaScript, McDonald
  if (/\p{Ll}\p{Lu}/u.test(word.replace(/['’-]/g, ''))) return true
  // sigle : ONU, SNCF, PDF (au moins deux majuscules et pas de minuscule)
  const letters = word.replace(/[^\p{L}]/gu, '')
  if (letters.length >= 2 && letters === letters.toUpperCase() && letters !== letters.toLowerCase()) return true
  return false
}

export interface TokenizeOptions {
  /** Indique si une forme complète est un mot connu (aujourd'hui, prud'homme…). */
  isKnown?: (word: string) => boolean
}

export function tokenize(text: string, opts: TokenizeOptions = {}): Token[] {
  const tokens: Token[] = []
  let pos = 0
  const push = (type: TokenType, t: string, extra?: Partial<Token>) => {
    tokens.push({ type, text: t, start: pos, end: pos + t.length, ...extra })
    pos += t.length
  }

  outer: while (pos < text.length) {
    const ch = text[pos]
    let m: string | null

    if ((m = stick(SPACE, text, pos))) {
      push('space', m)
      continue
    }
    if (ch === '\n') {
      push('space', ch)
      continue
    }
    for (const re of PROTECTED_PATTERNS) {
      if ((m = stick(re, text, pos))) {
        // Un simple « mot.Mot » en fin de phrase n'est pas un domaine :
        // on laisse la règle typographique décider seulement si le suffixe
        // ressemble à un mot capitalisé (cas ambigu → reste protégé).
        push('protected', m)
        continue outer
      }
    }
    if ((m = stick(NUMBER, text, pos))) {
      push('number', m)
      continue
    }
    if ((m = stick(WORD, text, pos))) {
      // Apostrophe finale isolée : uniquement pour les élisions (« l' »).
      let word = m
      if (/['’]$/.test(word) && !ELISION_PREFIX.test(word + 'a')) word = word.slice(0, -1)
      if (looksTechnical(word)) {
        push('protected', word)
        continue
      }
      const el = ELISION_PREFIX.exec(word)
      if (el && !(opts.isKnown?.(word) ?? false)) {
        push('word', el[1] + el[2], { elision: true })
        word = word.slice(el[0].length)
        // seconde élision : « qu'l'on » n'existe pas, mais « s'il » si.
      }
      if (word) {
        if (/^(l|d|j|m|t|s|n|c|qu|jusqu|lorsqu|puisqu|quoiqu|presqu)['’]$/i.test(word)) {
          push('word', word, { elision: true })
        } else if (/['’]$/.test(word) && word.length > 1) {
          push('word', word.slice(0, -1))
          push('punct', word.slice(-1))
        } else push('word', word)
      }
      continue
    }
    // Ponctuation et symboles, un caractère (ou point de code) à la fois ;
    // les points de suspension restent groupés.
    if (text.startsWith('...', pos)) {
      push('punct', '...')
      continue
    }
    const cp = text.codePointAt(pos)!
    push('punct', String.fromCodePoint(cp))
  }
  return tokens
}

export function isWordLike(t: Token | undefined): t is Token {
  return !!t && t.type === 'word'
}
