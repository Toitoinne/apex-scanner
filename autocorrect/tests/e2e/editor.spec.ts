// Tests de bout en bout dans un vrai navigateur : vraie frappe clavier,
// vrai curseur DOM, vrais raccourcis Ctrl+Z.
import { expect, test, type Page } from '@playwright/test'

const editor = (page: Page) => page.locator('.writing')
const text = (page: Page) => editor(page).evaluate((el) => (el as HTMLElement).innerText.replace(/ | /g, ' ').trim())

async function open(page: Page) {
  await page.addInitScript(() => localStorage.clear())
  await page.goto('/')
  await expect(page.locator('.indicator')).toHaveText('Correction active', { timeout: 20_000 })
  await editor(page).click()
}

/** Attend que la correction automatique ait fini son travail. */
const settle = (page: Page) => page.waitForTimeout(900)

test('corrige automatiquement pendant la frappe, sans intervention', async ({ page }) => {
  await open(page)
  await page.keyboard.type("Bonjour, je vous envoi les document que vous m'avez demander.", { delay: 25 })
  await settle(page)
  expect(await text(page)).toBe("Bonjour, je vous envoie les documents que vous m'avez demandés.")
})

test('le curseur reste en place : la frappe continue au bon endroit', async ({ page }) => {
  await open(page)
  await page.keyboard.type('il est tres ', { delay: 20 })
  // Correction pendant la frappe, sans pause.
  await page.keyboard.type('content de vous voir.', { delay: 20 })
  await settle(page)
  expect(await text(page)).toBe('Il est très content de vous voir.')

  // Insertion au milieu d'un texte existant : le curseur ne saute pas.
  await page.keyboard.press('Home')
  for (let i = 0; i < 'Il est'.length; i++) await page.keyboard.press('ArrowRight')
  await page.keyboard.type(' deja', { delay: 20 })
  await page.keyboard.type(' vraiment', { delay: 20 })
  await settle(page)
  expect(await text(page)).toBe('Il est déjà vraiment très content de vous voir.')
})

test('Ctrl+Z annule la correction et ne la réapplique pas', async ({ page }) => {
  await open(page)
  await page.keyboard.type('Mon ami Kevin dit deja ', { delay: 20 })
  await settle(page)
  expect(await text(page)).toBe('Mon ami Kevin dit déjà')
  await page.keyboard.press('Control+z')
  await settle(page)
  expect(await text(page)).toBe('Mon ami Kevin dit deja')
  await page.keyboard.type('fini.', { delay: 20 })
  await settle(page)
  expect(await text(page)).toBe('Mon ami Kevin dit deja fini.')
})

test('ne modifie pas les URL, e-mails et noms propres', async ({ page }) => {
  await open(page)
  const s = 'Écrivez à jean.dupont@exemple.fr ou visitez https://exemple.com/a?b=1 avec Maëlle.'
  await page.keyboard.type(s, { delay: 5 })
  await settle(page)
  expect(await text(page)).toBe(s)
})

test('gère le collage de textes volumineux', async ({ page }) => {
  await open(page)
  const para = "je vous envoi les document que vous m'avez demander. il est tres content."
  const html = Array.from({ length: 200 }, () => `<p>${para}</p>`).join('')
  await page.evaluate((h) => {
    const dt = new DataTransfer()
    dt.setData('text/html', h)
    dt.setData('text/plain', h.replace(/<[^>]+>/g, '\n'))
    document.querySelector('.writing')!.dispatchEvent(new ClipboardEvent('paste', { clipboardData: dt, bubbles: true, cancelable: true }))
  }, html)
  await page.waitForTimeout(2500)
  const paras = await editor(page).locator('p').allInnerTexts()
  expect(paras.length).toBe(200)
  for (const p of paras) expect(p.replace(/ | /g, ' ')).toBe("Je vous envoie les documents que vous m'avez demandés. Il est très content.")
})

test('interrupteur : en pause, rien n’est corrigé', async ({ page }) => {
  await open(page)
  await page.locator('.actions .switch').click()
  await expect(page.locator('.indicator')).toHaveText('En pause')
  await editor(page).click()
  await page.keyboard.type('il est tres content.', { delay: 10 })
  await settle(page)
  expect(await text(page)).toBe('il est tres content.')
})

test('copie, compteur de mots et historique', async ({ page, context }) => {
  await context.grantPermissions(['clipboard-read', 'clipboard-write'])
  await open(page)
  await page.keyboard.type('Un deux trois quatre.', { delay: 10 })
  await expect(page.locator('.statusbar')).toContainText('4 mots')
  await page.getByRole('button', { name: 'Copier le texte' }).click()
  await expect(page.locator('.toast')).toHaveText('Texte copié')
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe('Un deux trois quatre.')
  await page.getByRole('button', { name: 'Historique des versions' }).click()
  await expect(page.locator('.version')).toHaveCount(1)
})

test.describe('couche IA (API simulée par interception réseau)', () => {
  async function mockAi(page: Page, fix: (s: string) => string) {
    const calls: string[] = []
    await page.route('**/api/correct', async (route) => {
      if (route.request().method() === 'GET') return route.fulfill({ json: { enabled: true, model: 'simulé' } })
      const body = route.request().postDataJSON()
      calls.push(...body.items.map((i: { text: string }) => i.text))
      await route.fulfill({ json: { items: body.items.map((i: { id: string; text: string }) => ({ id: i.id, corrected: fix(i.text), doubts: [] })) } })
    })
    return calls
  }

  test('applique une correction contextuelle sur une phrase terminée', async ({ page }) => {
    const calls = await mockAi(page, (s) => s.replace('facile a faire', 'facile à faire'))
    await open(page)
    await page.keyboard.type("C'est facile a faire. Ensuite on verra", { delay: 15 })
    await page.waitForTimeout(2500)
    expect(await text(page)).toBe("C'est facile à faire. Ensuite on verra")
    // seule la phrase terminée est envoyée, jamais la phrase en cours
    expect(calls).toEqual(["C'est facile a faire."])
  })

  test('rejette une reformulation en mode « Correction »', async ({ page }) => {
    await mockAi(page, () => 'Le dossier vous sera transmis dès demain matin.')
    await open(page)
    await page.keyboard.type('Je vous envoie le dossier demain. ', { delay: 10 })
    await page.waitForTimeout(2500)
    expect(await text(page)).toBe('Je vous envoie le dossier demain.')
  })

  test('une réponse arrivée après de nouvelles frappes est remappée', async ({ page }) => {
    let release!: () => void
    const gate = new Promise<void>((r) => (release = r))
    await page.route('**/api/correct', async (route) => {
      if (route.request().method() === 'GET') return route.fulfill({ json: { enabled: true, model: 'simulé' } })
      const body = route.request().postDataJSON()
      await gate // la réponse est retenue pendant que l'utilisateur continue d'écrire
      await route.fulfill({ json: { items: body.items.map((i: { id: string; text: string }) => ({ id: i.id, corrected: i.text.replace('a faire', 'à faire'), doubts: [] })) } })
    })
    await open(page)
    await page.keyboard.type("C'est facile a faire.", { delay: 10 })
    await page.waitForTimeout(1800) // requête partie
    await page.keyboard.press('Home')
    await page.keyboard.type('Oui. ', { delay: 10 })
    await page.keyboard.press('End')
    await page.keyboard.type(' Et voilà', { delay: 10 })
    release()
    await page.waitForTimeout(1500)
    expect(await text(page)).toBe("Oui. C'est facile à faire. Et voilà")
  })
})
