// Génère le lexique compact utilisé par le moteur de correction.
//
// Source : dictionnaire Hunspell « classique » de Grammalecte (paquet npm
// dictionary-fr, licence MPL 2.0). On développe chaque entrée avec ses affixes
// (via les fonctions internes de nspell) pour obtenir toutes les formes
// fléchies, et on garde quelques informations morphologiques simples :
//   V  forme verbale            I  infinitif d'un verbe
//   N  lemme nominal/adjectival (singulier) avec son pluriel
// Fréquences : liste FrequencyWords (sous-titres OpenSubtitles, CC-BY-SA 4.0).
//
// Sortie : public/dict/fr.txt  — une ligne par forme : « mot[\ttags][\tfréquence] »
import fs from 'node:fs'
import path from 'node:path'
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'

const require = createRequire(import.meta.url)
const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)))
const out = path.join(root, 'public', 'dict', 'fr.txt')

if (process.argv.includes('--if-missing') && fs.existsSync(out)) process.exit(0)

const t0 = Date.now()
const dictDir = path.join(root, 'node_modules', 'dictionary-fr')
const aff = fs.readFileSync(path.join(dictDir, 'index.aff'))
const dic = fs.readFileSync(path.join(dictDir, 'index.dic'), 'utf8')

const nspell = require('nspell')
const addWord = require('nspell/lib/util/add.js')
const ruleCodes = require('nspell/lib/util/rule-codes.js')

// Instance sans dictionnaire : seul le fichier d'affixes est analysé.
const spell = nspell({ aff, dic: '0\n' })

const ELISION = /^(l|d|qu|j|m|t|s|n|c|jusqu|lorsqu|puisqu|quoiqu)['’]/i
const NOMINAL = new Set(['S.', 'X.', 'F.', 'W.', 'Wx', 'Wa', 'Wb', 'Wc', 'Wd'])

/** @type {Map<string, {tags: Set<string>, plural?: string}>} */
const words = new Map()
const entry = (w) => {
  let e = words.get(w)
  if (!e) words.set(w, (e = { tags: new Set() }))
  return e
}

const lines = dic.split('\n').slice(1)
for (const raw of lines) {
  const line = raw.split('\t')[0].trim()
  if (!line) continue
  const slash = line.indexOf('/')
  const lemma = slash === -1 ? line : line.slice(0, slash)
  const codesStr = slash === -1 ? '' : line.slice(slash + 1)
  const codes = ruleCodes(spell.flags, codesStr)
  const forms = {}
  addWord(forms, lemma, codes, spell)
  const list = Object.keys(forms).filter((f) => !ELISION.test(f) && !/\s/.test(f))
  for (const f of list) entry(f)

  // Verbe : beaucoup de formes, lemme à l'infinitif.
  const isVerb = /(er|ir|re|ïr)$/.test(lemma) && list.length >= 20 && !codes.some((c) => NOMINAL.has(c))
  if (isVerb) {
    entry(lemma).tags.add('I')
    for (const f of list) entry(f).tags.add('V')
  } else if (codes.some((c) => NOMINAL.has(c)) && !/[sxz]$/.test(lemma)) {
    // Pluriel : forme en -s / -x issue du même lemme (cheval → chevaux).
    const plural = [lemma + 's', lemma + 'x', lemma.replace(/al$/, 'aux'), lemma.replace(/ail$/, 'aux')].find(
      (f) => f !== lemma && list.includes(f),
    )
    if (plural) {
      const e = entry(lemma)
      e.tags.add('N')
      e.plural = plural
    }
  }
}

// Fréquences (mots du lexique uniquement).
const freq = new Map()
const freqFile = path.join(root, 'data', 'fr_50k.txt')
if (fs.existsSync(freqFile)) {
  for (const l of fs.readFileSync(freqFile, 'utf8').split('\n')) {
    const [w, n] = l.trim().split(' ')
    if (w && n && words.has(w) && !freq.has(w)) freq.set(w, Number(n))
  }
}

const sorted = [...words.keys()].sort()
const rows = sorted.map((w) => {
  const e = words.get(w)
  let tags = [...e.tags].sort().join('')
  if (e.plural && e.plural !== w + 's') tags += ':' + e.plural
  const f = freq.get(w)
  if (!tags && !f) return w
  return f ? `${w}\t${tags}\t${f}` : `${w}\t${tags}`
})

fs.mkdirSync(path.dirname(out), { recursive: true })
fs.writeFileSync(out, rows.join('\n') + '\n')
console.log(
  `[dict] ${sorted.length} formes, ${freq.size} fréquences → ${path.relative(root, out)} ` +
    `(${(fs.statSync(out).size / 1e6).toFixed(1)} Mo, ${Date.now() - t0} ms)`,
)
