import { describe, expect, it } from 'vitest'
import { ChangeSet } from '@/engine/changeset'
import { applyHunks, diffWords } from '@/engine/diff'
import { splitSentences } from '@/engine/sentences'
import { validateRewrite } from '@/engine/validate'

describe('diffWords', () => {
  it('produit des modifications minimales qui reconstruisent la cible', () => {
    const a = 'Je vous envoi les document que vous avez demander.'
    const b = 'Je vous envoie les documents que vous avez demandés.'
    const h = diffWords(a, b)
    expect(h.map((x) => [x.original, x.replacement])).toEqual([
      ['envoi', 'envoie'],
      ['document', 'documents'],
      ['demander', 'demandés'],
    ])
    expect(applyHunks(a, h)).toBe(b)
  })
})

describe('validateRewrite', () => {
  it('accepte une correction', () => {
    expect(validateRewrite('Il a tord.', 'Il a tort.', 'correct').ok).toBe(true)
  })
  it('refuse une reformulation en mode correction', () => {
    expect(validateRewrite('Je vous envoie le dossier demain.', 'Le dossier vous sera transmis dès demain.', 'correct').ok).toBe(false)
  })
  it('refuse toute modification des nombres, noms propres, URL', () => {
    expect(validateRewrite('Payez 120 € à Martin.', 'Payez 210 € à Martin.', 'correct').ok).toBe(false)
    expect(validateRewrite('Payez 120 € à Martin.', 'Payez 120 € à Martine.', 'correct').ok).toBe(false)
    expect(validateRewrite('Voir https://a.fr/x.', 'Voir https://a.fr/y.', 'improve').ok).toBe(false)
  })
  it('refuse une négation ajoutée ou retirée en mode amélioration', () => {
    expect(validateRewrite("Je ne viendrai pas demain à la réunion.", 'Je viendrai demain à la réunion.', 'improve').ok).toBe(false)
  })
  it('accepte une amélioration légère', () => {
    expect(validateRewrite('Je pense que je pense que ce projet est bien.', 'Je pense que ce projet est bien.', 'improve').ok).toBe(true)
  })
})

describe('ChangeSet', () => {
  it('compose plusieurs passes en coordonnées d’origine', () => {
    const cs = new ChangeSet('abc def ghi')
    cs.apply([{ start: 4, end: 7, replacement: 'DEFX', meta: { rule: 'r', category: 'orthographe' } }])
    cs.apply([{ start: 0, end: 3, replacement: 'A', meta: { rule: 'r', category: 'orthographe' } }])
    cs.apply([{ start: 1, end: 4, replacement: 'xy', meta: { rule: 'r', category: 'orthographe' } }])
    expect(cs.text).toBe('AxyFX ghi')
    const edits = cs.edits()
    let out = 'abc def ghi'
    for (const e of [...edits].sort((a, b) => b.start - a.start)) out = out.slice(0, e.start) + e.replacement + out.slice(e.end)
    expect(out).toBe(cs.text)
  })
})

describe('splitSentences', () => {
  it('découpe en respectant les abréviations', () => {
    const s = splitSentences('Bonjour M. Dupont. Voir p. 12, etc. Fin ? Suite')
    expect(s.map((x) => [x.text, x.complete])).toEqual([
      ['Bonjour M. Dupont.', true],
      ['Voir p. 12, etc. Fin ?', true],
      ['Suite', false],
    ])
  })
})
