// Tests d'intégration ProseMirror sans navigateur : frappe simulée caractère
// par caractère, position du curseur, Ctrl+Z, modifications concurrentes.
import { describe, expect, it } from 'vitest'
import { getSchema } from '@tiptap/core'
import StarterKit from '@tiptap/starter-kit'
import { history, undo, redo } from '@tiptap/pm/history'
import { EditorState, TextSelection, type Transaction } from '@tiptap/pm/state'
import {
  applyRemoteHunks,
  autocorrectKey,
  computeLocalCorrections,
  createAutoCorrectPlugin,
  currentVersion,
  historyBarrier,
} from '@/editor/core'
import { loadLexicon } from './lexicon-fixture'

const lex = loadLexicon()
const schema = getSchema([StarterKit])
const settings = { frenchSpacing: true, typographicApostrophe: false }

function create(text = '') {
  const doc = schema.node('doc', null, [schema.node('paragraph', null, text ? [schema.text(text)] : [])])
  let state = EditorState.create({ schema, doc, plugins: [history(), createAutoCorrectPlugin()] })
  state = state.apply(state.tr.setSelection(TextSelection.atEnd(state.doc)))
  return state
}

const text = (s: EditorState) => s.doc.textContent

/** Ce que fait le contrôleur après chaque frappe (sans les délais). */
function autocorrect(state: EditorState): EditorState {
  for (let i = 0; i < 10; i++) {
    const { tr, remaining } = computeLocalCorrections(state, lex, { settings })
    if (tr) {
      state = state.apply(tr)
      if (tr.docChanged) state = state.apply(historyBarrier(state))
    }
    if (!remaining) break
  }
  return state
}

/** Tape une chaîne caractère par caractère (avec horodatage réaliste pour l'historique). */
function type(state: EditorState, chars: string, opts: { correct?: boolean; clock?: { t: number } } = {}): EditorState {
  const clock = opts.clock ?? { t: 1_000_000 }
  for (const ch of chars) {
    clock.t += 80
    const tr = state.tr.insertText(ch)
    tr.setTime(clock.t)
    state = state.apply(tr)
    if (opts.correct !== false) state = autocorrect(state)
  }
  return state
}

const run = (state: EditorState, cmd: typeof undo) => {
  let next = state
  cmd(state, (tr: Transaction) => (next = state.apply(tr)))
  return next
}

/** Texte situé juste avant le curseur. */
const beforeCursor = (s: EditorState, n = 12) => s.doc.textBetween(Math.max(1, s.selection.head - n), s.selection.head)

describe('frappe en temps réel', () => {
  it("corrige la phrase d'exemple au fil de la saisie", () => {
    let s = create()
    s = type(s, "Bonjour, je vous envoi les document que vous m'avez demander.")
    expect(text(s)).toBe("Bonjour, je vous envoie les documents que vous m'avez demandés.")
    // Le curseur est resté en fin de texte, après le point.
    expect(s.selection.head).toBe(s.doc.content.size - 1)
  })

  it('ne modifie jamais le mot en cours de frappe', () => {
    let s = create()
    s = type(s, 'Il est tres')
    expect(text(s)).toBe('Il est tres')
    s = type(s, 'se') // l'utilisateur écrivait « tresse »
    expect(text(s)).toBe('Il est tresse')
  })

  it('garde le curseur au même endroit du texte quand on corrige plus haut', () => {
    let s = create('Je vous envoi le dossier et la suite')
    // curseur au milieu de « suite »
    const pos = s.doc.content.size - 1 - 3
    s = s.apply(s.tr.setSelection(TextSelection.create(s.doc, pos)))
    const charsAfter = s.doc.textBetween(pos, s.doc.content.size - 1)
    s = autocorrect(s)
    expect(text(s)).toBe('Je vous envoie le dossier et la suite')
    expect(s.doc.textBetween(s.selection.head, s.doc.content.size - 1)).toBe(charsAfter)
  })

  it('préserve la sélection et ne touche pas au texte sélectionné', () => {
    let s = create('Je vous envoi le dossier.')
    s = s.apply(s.tr.setSelection(TextSelection.create(s.doc, 9, 14))) // « envoi »
    s = autocorrect(s)
    expect(text(s)).toBe('Je vous envoi le dossier.')
    expect(s.doc.textBetween(s.selection.from, s.selection.to)).toBe('envoi')
  })

  it('gère les accents, apostrophes et caractères spéciaux', () => {
    let s = create()
    s = type(s, 'Ça va ? Oui, c’est deja fait — 100 % réglé. ')
    expect(text(s)).toBe('Ça va ? Oui, c’est déjà fait — 100 % réglé. ')
  })
})

describe('annulation (Ctrl+Z)', () => {
  it('annule la correction seule, puis ne la réapplique plus', () => {
    const clock = { t: 1_000_000 }
    let s = create()
    s = type(s, 'Le mot deja ', { clock })
    expect(text(s)).toBe('Le mot déjà ')
    s = run(s, undo)
    expect(text(s)).toBe('Le mot deja ') // la correction est annulée, la frappe est conservée
    s = type(s, 'ici ', { clock })
    expect(text(s)).toBe('Le mot deja ici ') // et elle n'est pas réappliquée
    expect(autocorrectKey.getState(s)!.ignored.length).toBeGreaterThan(0)
  })

  it('rétablit la correction avec Ctrl+Maj+Z', () => {
    let s = create()
    s = type(s, 'Il est tres ')
    expect(text(s)).toBe('Il est très ')
    s = run(s, undo)
    expect(text(s)).toBe('Il est tres ')
    s = run(s, redo)
    expect(text(s)).toBe('Il est très ')
  })

  it('une seconde annulation défait la frappe, pas une autre correction', () => {
    const clock = { t: 1_000_000 }
    let s = create()
    s = type(s, 'Il est tres ', { clock })
    s = run(s, undo) // correction
    s = run(s, undo) // frappe
    expect(text(s).length).toBeLessThan('Il est tres '.length)
    expect(text(s)).not.toContain('très')
  })
})

describe('modifications concurrentes (réponses asynchrones)', () => {
  it('remappe une correction calculée avant que l’utilisateur ne continue à écrire', () => {
    let s = create('Il a tord. Ensuite')
    const version = currentVersion(s)
    const from = 1 + 'Il a '.length
    // L'utilisateur ajoute du texte AVANT la zone pendant que la requête est en vol.
    s = s.apply(s.tr.insertText('Hier, ', 1))
    const tr = applyRemoteHunks(s, version, [{ from, to: from + 4, original: 'tord', replacement: 'tort' }])
    expect(tr).not.toBeNull()
    s = s.apply(tr!)
    expect(text(s)).toBe('Hier, Il a tort. Ensuite')
  })

  it('abandonne une correction si le texte visé a changé entre-temps', () => {
    let s = create('Il a tord.')
    const version = currentVersion(s)
    const from = 1 + 'Il a '.length
    s = s.apply(s.tr.insertText('raison', from, from + 4))
    const tr = applyRemoteHunks(s, version, [{ from, to: from + 4, original: 'tord', replacement: 'tort' }])
    expect(tr).toBeNull()
    expect(text(s)).toBe('Il a raison.')
  })

  it('ne touche pas au mot sous le curseur', () => {
    let s = create('Il a tord')
    const version = currentVersion(s)
    const from = 1 + 'Il a '.length
    const tr = applyRemoteHunks(s, version, [{ from, to: from + 4, original: 'tord', replacement: 'tort' }])
    expect(tr).toBeNull()
  })

  it("ne défait jamais une correction que l'utilisateur a refusée", () => {
    const clock = { t: 1_000_000 }
    let s = create()
    s = type(s, 'Le mot deja ', { clock })
    s = run(s, undo)
    const version = currentVersion(s)
    const from = 1 + 'Le mot '.length
    const tr = applyRemoteHunks(s, version, [{ from, to: from + 4, original: 'deja', replacement: 'déjà' }])
    expect(tr).toBeNull()
  })
})

describe('collage de textes volumineux', () => {
  it('traite un long collage par lots sans rien perdre', () => {
    const para = "je vous envoi les document que vous m'avez demander. il est tres content."
    const paragraphs = Array.from({ length: 120 }, () => schema.node('paragraph', null, [schema.text(para)]))
    let s = create()
    s = s.apply(s.tr.replaceWith(0, s.doc.content.size, paragraphs))
    let batches = 0
    for (;;) {
      const { tr, remaining } = computeLocalCorrections(s, lex, { settings, maxBlocks: 40 })
      if (tr) s = s.apply(tr)
      batches++
      if (!remaining) break
    }
    expect(batches).toBe(3)
    s.doc.forEach((p) => expect(p.textContent).toBe("Je vous envoie les documents que vous m'avez demandés. Il est très content."))
  })
})
