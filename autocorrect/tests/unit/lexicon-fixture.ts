import fs from 'node:fs'
import path from 'node:path'
import { Lexicon } from '@/engine/lexicon'

let cached: Lexicon | null = null

/** Lexique réel (public/dict/fr.txt, généré par `npm run build:dict`). */
export function loadLexicon(): Lexicon {
  if (!cached) {
    const file = path.resolve(import.meta.dirname, '../../public/dict/fr.txt')
    cached = Lexicon.parse(fs.readFileSync(file, 'utf8'))
  }
  return cached
}
