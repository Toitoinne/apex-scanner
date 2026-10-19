// Persistance locale (navigateur uniquement). Rien n'est envoyé au serveur.
// Chaque accès est protégé : navigation privée, quota plein ou stockage
// bloqué ne doivent jamais casser l'éditeur.

import type { JSONContent } from '@tiptap/core'
import { DEFAULT_SETTINGS, type Settings } from '../editor/controller'

const K = {
  settings: 'autocorrect.settings.v1',
  doc: 'autocorrect.doc.v1',
  versions: 'autocorrect.versions.v1',
  dictionary: 'autocorrect.dictionary.v1',
}

function read<T>(key: string, fallback: T): T {
  try {
    const raw = window.localStorage.getItem(key)
    return raw ? (JSON.parse(raw) as T) : fallback
  } catch {
    return fallback
  }
}

function write(key: string, value: unknown) {
  try {
    window.localStorage.setItem(key, JSON.stringify(value))
  } catch {
    /* stockage indisponible : on continue sans */
  }
}

export const loadSettings = (): Settings => ({ ...DEFAULT_SETTINGS, ...read<Partial<Settings>>(K.settings, {}) })
export const saveSettings = (s: Settings) => write(K.settings, s)

export const loadDoc = (): JSONContent | null => read<JSONContent | null>(K.doc, null)
export const saveDoc = (d: JSONContent) => write(K.doc, d)

export interface Version {
  id: string
  at: number
  words: number
  preview: string
  doc: JSONContent
}

const MAX_VERSIONS = 40

export const loadVersions = (): Version[] => read<Version[]>(K.versions, [])
export function addVersion(v: Omit<Version, 'id'>): Version[] {
  const list = loadVersions()
  if (list[0]?.preview === v.preview && list[0]?.words === v.words) return list
  const next = [{ ...v, id: `${v.at}-${Math.random().toString(36).slice(2, 7)}` }, ...list].slice(0, MAX_VERSIONS)
  write(K.versions, next)
  return next
}
export function clearVersions() {
  write(K.versions, [])
}

export const loadDictionary = (): string[] => read<string[]>(K.dictionary, [])
export const saveDictionary = (words: string[]) => write(K.dictionary, [...new Set(words)].sort())
