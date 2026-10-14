// Vérifie la forme de la requête envoyée à l'API Claude et l'exploitation de
// la réponse, grâce à un faux serveur local (aucun appel réseau réel).
import http from 'node:http'
import { afterAll, beforeAll, describe, expect, it } from 'vitest'

let server: http.Server
let lastBody: any = null
let lastHeaders: http.IncomingHttpHeaders = {}
let reply: (body: any) => any = () => ({})

beforeAll(async () => {
  server = http.createServer((req, res) => {
    let raw = ''
    req.on('data', (c) => (raw += c))
    req.on('end', () => {
      lastBody = JSON.parse(raw)
      lastHeaders = req.headers
      res.setHeader('Content-Type', 'application/json')
      res.end(JSON.stringify(reply(lastBody)))
    })
  })
  await new Promise<void>((r) => server.listen(0, r))
  const port = (server.address() as { port: number }).port
  process.env.ANTHROPIC_BASE_URL = `http://127.0.0.1:${port}`
  process.env.ANTHROPIC_API_KEY = 'test-key'
})
afterAll(() => server.close())

const message = (text: string, stop = 'end_turn') => ({
  id: 'msg_test', type: 'message', role: 'assistant', model: 'claude-opus-5-5', stop_reason: stop, stop_sequence: null,
  content: [{ type: 'text', text }], usage: { input_tokens: 10, output_tokens: 10 },
})

describe('correctWithAi', () => {
  it('envoie une requête structurée et lit la réponse JSON', async () => {
    const { correctWithAi } = await import('@/server/ai')
    reply = (body) => {
      const items = JSON.parse(body.messages[0].content.match(/<phrases>\n([\s\S]*)\n<\/phrases>/)[1]).items
      return message(JSON.stringify({ items: items.map((i: any) => ({ id: i.id, corrected: i.text.replace(' a faire', ' à faire'), doubts: [] })) }))
    }
    const res = await correctWithAi('correct', [{ id: '0', text: "C'est facile a faire.", before: '' }])
    expect(res[0].corrected).toBe("C'est facile à faire.")
    expect(lastBody.model).toBe('claude-opus-5-5')
    expect(lastBody.output_config.format.type).toBe('json_schema')
    expect(lastBody.fallbacks).toBe('default')
    expect(lastHeaders['anthropic-beta']).toContain('server-side-fallback-2026-07-01')
    expect(lastBody.system).toMatch(/AUTOMATIQUEMENT/)
  })

  it('met en cache : la même phrase ne repart pas vers l’API', async () => {
    const { correctWithAi } = await import('@/server/ai')
    lastBody = null
    await correctWithAi('correct', [{ id: '7', text: "C'est facile a faire.", before: '' }])
    expect(lastBody).toBeNull()
  })

  it('en cas de refus ou de réponse illisible, le texte reste inchangé', async () => {
    const { correctWithAi } = await import('@/server/ai')
    reply = () => message('', 'refusal')
    expect((await correctWithAi('correct', [{ id: '0', text: 'Phrase un.' }]))[0].corrected).toBe('Phrase un.')
    reply = () => message('pas du json')
    expect((await correctWithAi('correct', [{ id: '0', text: 'Phrase deux.' }]))[0].corrected).toBe('Phrase deux.')
  })
})
