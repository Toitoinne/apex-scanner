import { describe, expect, it } from 'vitest'
import { analyze } from '@/engine/engine'
import { loadLexicon } from './lexicon-fixture'

const lex = loadLexicon()
const fix = (text: string, opts = {}) => analyze(text, lex, opts).text
const NB = ' ' // espace fine insécable

describe('situation A — erreurs certaines corrigées automatiquement', () => {
  it.each([
    ["Bonjour, je vous envoi les document que vous m'avez demander.", "Bonjour, je vous envoie les documents que vous m'avez demandés."],
    ['Il faut mangé avant de partir.', 'Il faut manger avant de partir.'],
    ['Je vais vous envoyé le dossier.', 'Je vais vous envoyer le dossier.'],
    ["Elle a manger une pomme.", "Elle a mangé une pomme."],
    ["La lettre que vous m'avez envoyé est arrivée.", "La lettre que vous m'avez envoyée est arrivée."],
    ["Les pommes que j'ai mangé étaient bonnes.", "Les pommes que j'ai mangées étaient bonnes."],
    ['Je travail sur le projet.', 'Je travaille sur le projet.'],
    ['Tu travail trop.', 'Tu travailles trop.'],
    ['Je le conseil vivement.', 'Je le conseille vivement.'],
    ['Il ma dit oui.', "Il m'a dit oui."],
    ['Je ai faim.', "J'ai faim."],
    ['Je pense que il viendra.', "Je pense qu'il viendra."],
    ['Si il pleut, on reste.', "S'il pleut, on reste."],
    ["c etait bien.", "C'était bien."],
    ["C'est deja fini.", "C'est déjà fini."],
    ['Il est tres content.', 'Il est très content.'],
    ['Je suis parmis vous.', 'Je suis parmi vous.'],
    ['On va appeller demain.', 'On va appeler demain.'],
    ['Je travaille maintenat.', 'Je travaille maintenant.'],
    ['Merci pour le le dossier.', 'Merci pour le dossier.'],
    ['Merci pour les fichier.', 'Merci pour les fichiers.'],
    ['Ce sont des cheval.', 'Ce sont des chevaux.'],
    ['Sa va bien ?', `Ça va bien${NB}?`],
    ['Il fait beau. nous partons.', 'Il fait beau. Nous partons.'],
    ['Bonjour , comment allez-vous ?', `Bonjour, comment allez-vous${NB}?`],
    ['Oui,merci.', 'Oui, merci.'],
    ['Il est  là.', 'Il est là.'],
    ['Attention!', `Attention${NB}!`],
    ['Note: rien.', 'Note : rien.'],
    ['Il a dit « bonjour ».', 'Il a dit « bonjour ».'],
  ])('%s', (input, expected) => {
    expect(fix(input)).toBe(expected)
  })

  it('préserve la casse et les apostrophes typographiques', () => {
    // Les mots tout en majuscules sont traités comme des sigles : jamais modifiés.
    expect(fix('TRES BIEN.')).toBe('TRES BIEN.')
    expect(fix('C’est deja fini.')).toBe('C’est déjà fini.')
    expect(fix('je ai vu l’arbre.')).toBe('J’ai vu l’arbre.')
  })
})

describe('situation B — doute : on ne modifie pas', () => {
  it.each([
    'C’est facile a faire.', // a / à : homophone laissé à l’IA
    'Je vais le marché.', // « le marché » peut être un nom
    'Il veut le passé.',
    'Les documents que j’ai demandé à recevoir.', // participe suivi de « à » : invariable
    'Les heures que j’ai travaillé.', // complément de mesure
    'Je suis allé au cinéma.', // genre de l’auteur inconnu
    'Les gens bien connus.',
    'Il a passé deux cent mille euros.',
    'Je les mange.',
    'Je suis entrain de lire.', // « entrain » existe (« avec entrain »)
    'Il marche avec entrain.',
  ])('%s', (input) => {
    expect(fix(input, { frenchSpacing: false })).toBe(input)
  })

  it('signale sans corriger un mot inconnu ambigu', () => {
    const r = analyze('Il a vu le chein.', lex)
    expect(r.text).toBe('Il a vu le chein.')
    expect(r.issues.map((i) => i.text)).toContain('chein')
    expect(r.issues[0].suggestions.length).toBeGreaterThan(0)
  })
})

describe('situation C — texte correct ou protégé : rien ne change', () => {
  it.each([
    'Bonjour, je vous envoie les documents que vous m’avez demandés.',
    'Écrivez à jean.dupont@exemple.fr ou sur https://exemple.com/page?x=1&y=deux.',
    'Le fichier config.yaml est dans ~/projets/app/src.',
    'J’ai acheté un iPhone 15 Pro chez Apple à Paris pour 1 299,99 €.',
    'La SNCF et l’ONU utilisent le PDF et le JSON.',
    'Kevin et Maëlle habitent à Bordeaux.',
    'Rendez-vous à 10:30 avec @martin et #équipe.',
    'Utilisez `npm run dev` pour démarrer.',
    'This is an English sentence that should stay untouched.',
    'Le temps est compté : 3:2 au final :)',
    'Aujourd’hui, quelqu’un est venu.',
    'Peut-être demain ? Oui, peut-être.',
    'A-t-il compris ? Dis-moi tout.',
  ])('%s', (input) => {
    expect(fix(input, { frenchSpacing: false })).toBe(input)
  })

  it('est idempotent : un texte déjà corrigé ne bouge plus', () => {
    const once = fix("bonjour , je vous envoi les document que vous m'avez demander. il ma dit que c etait tres bien!")
    expect(fix(once)).toBe(once)
  })
})

describe('zone de frappe et refus', () => {
  it('ne touche jamais au mot sous le curseur', () => {
    const text = 'Merci pour les document'
    expect(fix(text, { cursor: text.length })).toBe(text)
    expect(fix(text + ' ', { cursor: text.length + 1 })).toBe('Merci pour les documents ')
  })

  it('ne corrige pas un mot qui pourrait encore être complété', () => {
    // « tres » pourrait devenir « tresse »
    expect(fix('Elle a une tres', { cursor: 15 })).toBe('Elle a une tres')
  })

  it('respecte les corrections refusées', () => {
    const text = 'Je vous envoi le dossier.'
    const r = analyze(text, lex, { isIgnored: (_s, _e, original) => original === 'envoi' })
    expect(r.text).toBe(text)
  })

  it('positions des modifications exprimées dans le texte d’origine', () => {
    const text = 'je ai deja vu'
    const r = analyze(text, lex)
    for (const e of r.edits) expect(text.slice(e.start, e.end)).toBe(e.original)
    let out = text
    for (const e of [...r.edits].sort((a, b) => b.start - a.start)) out = out.slice(0, e.start) + e.replacement + out.slice(e.end)
    expect(out).toBe(r.text)
  })
})

describe('performance', () => {
  it('analyse un long texte collé rapidement', () => {
    const para = "Bonjour, je vous envoi les document que vous m'avez demander. Il est tres content de ce projet et il ma dit que c etait bien. "
    const text = para.repeat(40) // ~5 000 caractères
    const t0 = performance.now()
    const r = analyze(text, lex)
    const ms = performance.now() - t0
    expect(r.edits.length).toBeGreaterThan(100)
    expect(ms).toBeLessThan(1500)
  })
})
