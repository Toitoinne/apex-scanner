// Pilotage côté navigateur : temporisation, découpage des gros collages,
// appels IA (phrases terminées uniquement) et état affiché par l'interface.

import { Extension } from '@tiptap/core'
import { Plugin } from '@tiptap/pm/state'
import type { EditorView } from '@tiptap/pm/view'
import { splitSentences } from '../engine/sentences'
import { validateRewrite, type Mode } from '../engine/validate'
import type { Lexicon } from '../engine/lexicon'
import {
  applyRemoteHunks,
  autocorrectKey,
  blocksAt,
  clearFlash,
  computeLocalCorrections,
  createAutoCorrectPlugin,
  currentVersion,
  historyBarrier,
  markAiChecked,
  markAllDirty,
  mappingSince,
  type AppliedCorrection,
  type RemoteHunk,
} from './core'

export interface Settings {
  enabled: boolean
  mode: Mode
  /** Correction contextuelle par IA (envoie les phrases terminées au serveur). */
  ai: boolean
  frenchSpacing: boolean
  typographicApostrophe: boolean
}

export const DEFAULT_SETTINGS: Settings = {
  enabled: true,
  mode: 'correct',
  ai: true,
  frenchSpacing: true,
  typographicApostrophe: false,
}

export type AiStatus = 'unavailable' | 'off' | 'idle' | 'busy' | 'error'

export interface Status {
  lexicon: 'loading' | 'ready' | 'error'
  ai: AiStatus
  aiMessage?: string
  corrections: number
}

/** Délais (ms) : après un séparateur on corrige presque immédiatement, sinon on attend une pause. */
export const TIMING = {
  afterBoundary: 60,
  idle: 700,
  aiCorrect: 1200,
  aiImprove: 2500,
  flash: 1400,
}

export class AutoCorrectController {
  private view: EditorView | null = null
  private lexicon: Lexicon | null = null
  private localTimer: ReturnType<typeof setTimeout> | null = null
  private aiTimer: ReturnType<typeof setTimeout> | null = null
  private flashTimer: ReturnType<typeof setTimeout> | null = null
  private inflight: AbortController | null = null
  private processed = new Set<string>()
  private listeners = new Set<(s: Status) => void>()
  private logListeners = new Set<(r: AppliedCorrection[]) => void>()
  aiAvailable = false
  settings: Settings = { ...DEFAULT_SETTINGS }
  status: Status = { lexicon: 'loading', ai: 'unavailable', corrections: 0 }

  constructor(private readonly endpoint = '/api/correct') {}

  // --- cycle de vie ----------------------------------------------------------
  attach(view: EditorView) {
    this.view = view
    this.scheduleLocal(0)
  }

  detach() {
    this.view = null
    for (const t of [this.localTimer, this.aiTimer, this.flashTimer]) if (t) clearTimeout(t)
    this.inflight?.abort()
  }

  setLexicon(lex: Lexicon | null, state: Status['lexicon'] = lex ? 'ready' : 'error') {
    this.lexicon = lex
    this.setStatus({ lexicon: state })
    if (lex) this.scheduleLocal(0)
  }

  setAiAvailable(ok: boolean) {
    this.aiAvailable = ok
    this.setStatus({ ai: this.computeAiStatus() })
  }

  updateSettings(patch: Partial<Settings>) {
    const prev = this.settings
    this.settings = { ...prev, ...patch }
    if (patch.mode && patch.mode !== prev.mode) this.inflight?.abort()
    this.setStatus({ ai: this.computeAiStatus() })
    const v = this.view
    if (!v) return
    if ((patch.enabled && !prev.enabled) || patch.frenchSpacing !== undefined || patch.typographicApostrophe !== undefined || (patch.mode && patch.mode !== prev.mode) || (patch.ai && !prev.ai)) {
      v.dispatch(markAllDirty(v.state))
      this.scheduleLocal(0)
      this.scheduleAi()
    }
  }

  /** Mots ajoutés au dictionnaire personnel : on réanalyse tout. */
  refresh() {
    const v = this.view
    if (!v) return
    v.dispatch(markAllDirty(v.state))
    this.scheduleLocal(0)
  }

  onStatus(fn: (s: Status) => void) {
    this.listeners.add(fn)
    fn(this.status)
    return () => this.listeners.delete(fn)
  }

  onCorrections(fn: (r: AppliedCorrection[]) => void) {
    this.logListeners.add(fn)
    return () => this.logListeners.delete(fn)
  }

  private setStatus(patch: Partial<Status>) {
    this.status = { ...this.status, ...patch }
    for (const l of this.listeners) l(this.status)
  }

  private computeAiStatus(): AiStatus {
    if (!this.aiAvailable) return 'unavailable'
    if (!this.settings.ai && this.settings.mode === 'correct') return 'off'
    return this.status.ai === 'busy' ? 'busy' : 'idle'
  }

  private aiActive() {
    return this.aiAvailable && this.settings.enabled && (this.settings.ai || this.settings.mode === 'improve')
  }

  // --- réactions aux modifications ------------------------------------------
  handleUpdate(view: EditorView) {
    const ps = autocorrectKey.getState(view.state)
    if (!ps) return
    if (ps.dirty.length) this.scheduleLocal(ps.boundary ? TIMING.afterBoundary : TIMING.idle)
    if (ps.aiDirty.length) this.scheduleAi()
  }

  private scheduleLocal(delay: number) {
    if (this.localTimer) clearTimeout(this.localTimer)
    this.localTimer = setTimeout(() => this.runLocal(), delay)
  }

  private scheduleAi() {
    if (!this.aiActive()) return
    if (this.aiTimer) clearTimeout(this.aiTimer)
    // Toute nouvelle frappe annule une requête en cours sur l'ancien état.
    this.aiTimer = setTimeout(() => void this.runAi(), this.settings.mode === 'improve' ? TIMING.aiImprove : TIMING.aiCorrect)
  }

  runLocal() {
    this.localTimer = null
    const view = this.view
    const lex = this.lexicon
    if (!view || !lex || !this.settings.enabled) return
    if (view.composing) return this.scheduleLocal(250) // saisie IME en cours : on attend
    const { tr, remaining } = computeLocalCorrections(view.state, lex, { settings: this.settings })
    if (tr) {
      view.dispatch(tr)
      if (tr.docChanged) {
        view.dispatch(historyBarrier(view.state))
        this.afterApplied()
      }
    }
    // Gros collage : on rend la main au navigateur entre deux lots.
    if (remaining > 0) this.scheduleLocal(16)
  }

  private afterApplied() {
    const v = this.view
    if (!v) return
    const ps = autocorrectKey.getState(v.state)
    if (ps) {
      this.setStatus({ corrections: ps.log.length })
      for (const l of this.logListeners) l(ps.log)
    }
    if (this.flashTimer) clearTimeout(this.flashTimer)
    this.flashTimer = setTimeout(() => this.view && this.view.dispatch(clearFlash(this.view.state)), TIMING.flash)
  }

  // --- IA ---------------------------------------------------------------------
  async runAi() {
    this.aiTimer = null
    const view = this.view
    if (!view || !this.aiActive()) return
    if (this.inflight) return this.scheduleAi()
    const state = view.state
    const ps = autocorrectKey.getState(state)
    if (!ps || ps.dirty.length) return this.scheduleAi() // le moteur local passe d'abord
    const mode = this.settings.mode
    const head = state.selection.head
    const blocks = blocksAt(state.doc, ps.aiDirty).slice(0, 8)
    const items: { id: string; text: string; before: string; from: number; to: number }[] = []
    const checked: { from: number; to: number }[] = []
    for (const b of blocks) {
      let complete = true
      const sentences = splitSentences(b.text)
      sentences.forEach((s, k) => {
        const from = b.start + s.start
        const to = b.start + s.end
        if (!s.complete || (head > from && head < to)) {
          complete = false
          return
        }
        if (s.text.includes('￼') || s.text.length > 1000 || !/\p{L}{2}/u.test(s.text)) return
        const key = `${mode}\u0000${s.text}`
        if (this.processed.has(key) || items.length >= 10) {
          if (items.length >= 10) complete = false
          return
        }
        items.push({ id: String(items.length), text: s.text, before: sentences[k - 1]?.text ?? '', from, to })
      })
      if (complete) checked.push({ from: b.start - 1, to: b.start + b.text.length + 1 })
    }
    if (items.length === 0) {
      if (checked.length) view.dispatch(markAiChecked(state, checked))
      return
    }

    const version = currentVersion(state)
    const ctrl = new AbortController()
    this.inflight = ctrl
    this.setStatus({ ai: 'busy', aiMessage: undefined })
    try {
      const res = await fetch(this.endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mode, items: items.map(({ id, text, before }) => ({ id, text, before })) }),
        signal: ctrl.signal,
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`)
      const v = this.view
      if (!v || ctrl.signal.aborted || this.settings.mode !== mode) return
      const hunks: RemoteHunk[] = []
      for (const r of (data.items ?? []) as { id: string; corrected: string }[]) {
        const it = items.find((x) => x.id === r.id)
        if (!it) continue
        this.processed.add(`${mode}\u0000${it.text}`)
        const check = validateRewrite(it.text, r.corrected, mode)
        if (!check.ok) continue
        this.processed.add(`${mode}\u0000${r.corrected}`)
        for (const h of check.hunks) hunks.push({ from: it.from + h.start, to: it.from + h.end, original: h.original, replacement: h.replacement })
      }
      const tr = applyRemoteHunks(v.state, version, hunks)
      if (tr) {
        v.dispatch(tr)
        v.dispatch(historyBarrier(v.state))
        this.afterApplied()
      }
      if (checked.length) {
        // Les positions « vérifiées » datent de la requête : on les remappe.
        const mapping = mappingSince(v.state, version)
        if (mapping) v.dispatch(markAiChecked(v.state, checked.map((c) => ({ from: mapping.map(c.from, -1), to: mapping.map(c.to, 1) }))))
      }
      this.setStatus({ ai: this.computeAiStatus(), aiMessage: undefined })
    } catch (err) {
      if (ctrl.signal.aborted) return
      this.setStatus({ ai: 'error', aiMessage: err instanceof Error ? err.message : String(err) })
      // nouvel essai plus tard, sans insister
      if (this.aiTimer) clearTimeout(this.aiTimer)
      this.aiTimer = setTimeout(() => void this.runAi(), 15_000)
    } finally {
      if (this.inflight === ctrl) this.inflight = null
      if (this.status.ai === 'busy') this.setStatus({ ai: this.computeAiStatus() })
    }
  }
}

/** Extension TipTap qui branche le plugin et le contrôleur. */
export const AutoCorrect = Extension.create<{ controller: AutoCorrectController | null }>({
  name: 'autocorrect',
  addOptions() {
    return { controller: null }
  },
  addProseMirrorPlugins() {
    const controller = this.options.controller
    return [
      createAutoCorrectPlugin(),
      new Plugin({
        view: (view) => {
          controller?.attach(view)
          return {
            update: (v, prev) => {
              if (v.state.doc !== prev.doc) controller?.handleUpdate(v)
            },
            destroy: () => controller?.detach(),
          }
        },
      }),
    ]
  },
})
