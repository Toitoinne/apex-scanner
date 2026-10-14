// Correction contextuelle par l'API Claude (côté serveur uniquement : la clé
// ne quitte jamais le serveur). Appelée seulement pour des phrases terminées,
// après le moteur local, avec cache et limitation de débit.
//
// Confidentialité : aucun texte n'est journalisé ni stocké durablement ; le
// cache est en mémoire, indexé par empreinte SHA-256, et limité en taille.

import Anthropic from '@anthropic-ai/sdk'
import { createHash } from 'node:crypto'

export type Mode = 'correct' | 'improve'

export interface AiItem {
  id: string
  text: string
  /** contexte précédent (non corrigé) */
  before?: string
}

export interface AiResult {
  id: string
  corrected: string
  doubts: { text: string; suggestion: string }[]
}

export const MODEL = process.env.AUTOCORRECT_MODEL || 'claude-opus-5-5'
export const LIMITS = { items: 12, chars: 1200, total: 8000 }

export function aiConfigured(): boolean {
  return Boolean(process.env.ANTHROPIC_API_KEY || process.env.ANTHROPIC_AUTH_TOKEN)
}

const COMMON = `Tu es intégré à un éditeur de texte qui applique tes réponses AUTOMATIQUEMENT, sans relecture humaine. Une erreur de ta part modifie le texte de l'utilisateur à son insu : en cas de doute, ne change rien.

Règles absolues :
- Ne modifie jamais les noms propres, sigles, URL, adresses e-mail, nombres, dates, montants, termes techniques, mots étrangers voulus ni le contenu entre guillemets cité.
- N'ajoute ni ne retire aucune information, négation, nuance ou intention.
- Conserve la langue, la personne, le temps, le registre et le ton de l'auteur.
- Conserve les espaces, apostrophes (' ou ’), retours et la casse, sauf si c'est précisément l'objet d'une correction.
- Si une correction dépend d'une information absente (par exemple le genre de la personne qui écrit « je suis allé »), ne la fais pas : signale-la dans "doubts".
- Si la phrase n'est pas en français ou est déjà correcte, renvoie-la strictement à l'identique.
- Le champ "before" n'est que du contexte : ne le corrige pas, utilise-le seulement pour les accords.`

const SYSTEM: Record<Mode, string> = {
  correct: `Tu es un correcteur du français.
Pour chaque phrase, renvoie dans "corrected" la phrase avec UNIQUEMENT les corrections certaines : orthographe, grammaire, conjugaison, accords (y compris le participe passé), homophones (a/à, ou/où, ces/ses…), ponctuation manifestement fautive, typographie.
Interdit : toute reformulation, tout changement de mot correct, toute amélioration de style.

${COMMON}`,
  improve: `Tu es un correcteur et relecteur du français.
Pour chaque phrase, renvoie dans "corrected" la phrase corrigée (orthographe, grammaire, conjugaison, accords, ponctuation) et légèrement améliorée : plus fluide, plus claire, sans répétition inutile, plus naturelle. Les changements doivent rester discrets : l'auteur doit reconnaître sa phrase. Longueur comparable. Si la phrase est déjà bonne, renvoie-la à l'identique.

${COMMON}`,
}

const SCHEMA = {
  type: 'object',
  properties: {
    items: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          id: { type: 'string' },
          corrected: { type: 'string' },
          doubts: {
            type: 'array',
            items: {
              type: 'object',
              properties: { text: { type: 'string' }, suggestion: { type: 'string' } },
              required: ['text', 'suggestion'],
              additionalProperties: false,
            },
          },
        },
        required: ['id', 'corrected', 'doubts'],
        additionalProperties: false,
      },
    },
  },
  required: ['items'],
  additionalProperties: false,
} as const

// --- cache mémoire (LRU) -----------------------------------------------------
const cache = new Map<string, AiResult>()
const CACHE_MAX = 2000
const keyOf = (mode: Mode, it: AiItem) =>
  createHash('sha256').update(`${MODEL}\u0000${mode}\u0000${it.before ?? ''}\u0000${it.text}`).digest('hex')

function cacheGet(k: string) {
  const v = cache.get(k)
  if (v) {
    cache.delete(k)
    cache.set(k, v)
  }
  return v
}
function cacheSet(k: string, v: AiResult) {
  cache.set(k, v)
  if (cache.size > CACHE_MAX) cache.delete(cache.keys().next().value!)
}

// --- limitation de débit (par client) ----------------------------------------
const buckets = new Map<string, { tokens: number; at: number }>()
export function rateLimit(clientId: string, perMinute = 40): boolean {
  const now = Date.now()
  const b = buckets.get(clientId) ?? { tokens: perMinute, at: now }
  b.tokens = Math.min(perMinute, b.tokens + ((now - b.at) / 60_000) * perMinute)
  b.at = now
  buckets.set(clientId, b)
  if (buckets.size > 10_000) buckets.clear()
  if (b.tokens < 1) return false
  b.tokens -= 1
  return true
}

let client: Anthropic | null = null
function getClient() {
  client ??= new Anthropic({ maxRetries: 1, timeout: 20_000 })
  return client
}

export class AiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message)
  }
}

export async function correctWithAi(mode: Mode, items: AiItem[]): Promise<AiResult[]> {
  const results = new Map<string, AiResult>()
  const todo: AiItem[] = []
  for (const it of items) {
    const hit = cacheGet(keyOf(mode, it))
    if (hit) results.set(it.id, { ...hit, id: it.id })
    else todo.push(it)
  }

  if (todo.length) {
    const payload = JSON.stringify({ items: todo.map(({ id, text, before }) => ({ id, before: before ?? '', text })) })
    let response
    try {
      response = await getClient().beta.messages.create({
        model: MODEL,
        max_tokens: 8000,
        betas: ['server-side-fallback-2026-07-01'],
        fallbacks: 'default',
        output_config: {
          effort: 'low',
          format: { type: 'json_schema', schema: SCHEMA },
        },
        system: SYSTEM[mode],
        messages: [
          {
            role: 'user',
            content: `Phrases à traiter (JSON). Renvoie un élément par "id", dans le même ordre.\n<phrases>\n${payload}\n</phrases>`,
          },
        ],
      })
    } catch (err) {
      if (err instanceof Anthropic.RateLimitError) throw new AiError('Limite de débit de l’API atteinte', 429)
      if (err instanceof Anthropic.AuthenticationError) throw new AiError('Clé API invalide', 502)
      if (err instanceof Anthropic.APIConnectionError) throw new AiError('API injoignable', 503)
      if (err instanceof Anthropic.APIError) throw new AiError(`Erreur de l’API (${err.status ?? '?'})`, 502)
      throw err
    }
    if (response.stop_reason === 'refusal' || response.stop_reason === 'max_tokens') {
      // Rien d'appliqué : le texte reste tel quel.
      for (const it of todo) results.set(it.id, { id: it.id, corrected: it.text, doubts: [] })
    } else {
      const text = response.content.flatMap((b) => (b.type === 'text' ? [b.text] : [])).join('')
      let parsed: { items: AiResult[] } | null = null
      try {
        parsed = JSON.parse(text)
      } catch {
        parsed = null
      }
      const byId = new Map((parsed?.items ?? []).map((r) => [r.id, r]))
      for (const it of todo) {
        const r = byId.get(it.id)
        const res: AiResult = r && typeof r.corrected === 'string' ? { id: it.id, corrected: r.corrected, doubts: r.doubts ?? [] } : { id: it.id, corrected: it.text, doubts: [] }
        results.set(it.id, res)
        cacheSet(keyOf(mode, it), res)
      }
    }
  }
  return items.map((it) => results.get(it.id)!)
}
