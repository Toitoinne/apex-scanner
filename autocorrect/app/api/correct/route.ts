import { NextResponse, type NextRequest } from 'next/server'
import { AiError, aiConfigured, correctWithAi, LIMITS, MODEL, rateLimit, type AiItem, type Mode } from '@/server/ai'

export const runtime = 'nodejs'
export const dynamic = 'force-dynamic'

/** État de la couche IA (sans exposer la clé). */
export async function GET() {
  return NextResponse.json({ enabled: aiConfigured(), model: aiConfigured() ? MODEL : null })
}

export async function POST(req: NextRequest) {
  if (!aiConfigured()) {
    return NextResponse.json({ error: 'IA non configurée (ANTHROPIC_API_KEY absente)' }, { status: 503 })
  }
  const clientId = req.headers.get('x-forwarded-for')?.split(',')[0].trim() || 'local'
  if (!rateLimit(clientId)) return NextResponse.json({ error: 'Trop de requêtes' }, { status: 429 })

  let body: { mode?: unknown; items?: unknown }
  try {
    body = await req.json()
  } catch {
    return NextResponse.json({ error: 'JSON invalide' }, { status: 400 })
  }
  const mode: Mode = body.mode === 'improve' ? 'improve' : 'correct'
  if (!Array.isArray(body.items) || body.items.length === 0 || body.items.length > LIMITS.items) {
    return NextResponse.json({ error: 'items invalides' }, { status: 400 })
  }
  const items: AiItem[] = []
  let total = 0
  for (const raw of body.items as Record<string, unknown>[]) {
    if (typeof raw?.id !== 'string' || typeof raw?.text !== 'string') return NextResponse.json({ error: 'item invalide' }, { status: 400 })
    const before = typeof raw.before === 'string' ? raw.before.slice(-400) : ''
    if (raw.text.length > LIMITS.chars) return NextResponse.json({ error: 'phrase trop longue' }, { status: 413 })
    total += raw.text.length + before.length
    items.push({ id: raw.id.slice(0, 64), text: raw.text, before })
  }
  if (total > LIMITS.total) return NextResponse.json({ error: 'requête trop volumineuse' }, { status: 413 })

  try {
    const results = await correctWithAi(mode, items)
    return NextResponse.json({ items: results }, { headers: { 'Cache-Control': 'no-store' } })
  } catch (err) {
    if (err instanceof AiError) return NextResponse.json({ error: err.message }, { status: err.status })
    return NextResponse.json({ error: 'Erreur interne' }, { status: 500 })
  }
}
