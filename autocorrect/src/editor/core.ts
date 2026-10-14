// Cœur de l'intégration ProseMirror, indépendant du DOM (testable en Node).
//
// • Le plugin suit les paragraphes modifiés (« dirty »), les corrections
//   appliquées et celles que l'utilisateur a annulées (Ctrl+Z), en faisant
//   suivre toutes les positions à travers chaque transaction.
// • computeLocalCorrections() construit UNE transaction contenant toutes les
//   corrections certaines : la sélection est remappée par ProseMirror, donc
//   le curseur ne bouge pas par rapport au texte qui l'entoure.
// • applyRemoteHunks() applique des corrections calculées de façon asynchrone
//   (IA) sur un état plus ancien : positions remappées et texte revérifié.

import { closeHistory, isHistoryTransaction } from '@tiptap/pm/history'
import type { Node as PMNode } from '@tiptap/pm/model'
import { EditorState, Plugin, PluginKey, type Transaction } from '@tiptap/pm/state'
import { Mapping, type StepMap } from '@tiptap/pm/transform'
import { Decoration, DecorationSet } from '@tiptap/pm/view'
import { analyze, type AnalyzeOptions, type Issue } from '../engine/engine'
import type { Lexicon } from '../engine/lexicon'

export const autocorrectKey = new PluginKey<AutoCorrectState>('autocorrect')

export type Source = 'local' | 'ai'

export interface AppliedCorrection {
  from: number
  to: number
  original: string
  replacement: string
  source: Source
  rule: string
  at: number
}

export type Range = [number, number]

interface Ignored {
  from: number
  to: number
  text: string
}

export interface AutoCorrectState {
  applied: AppliedCorrection[]
  ignored: Ignored[]
  /** « original→remplacement » annulé N fois : au-delà de 2, plus jamais appliqué. */
  rejected: Map<string, number>
  /** plages modifiées à (ré)analyser */
  dirty: Range[]
  aiDirty: Range[]
  issues: DecorationSet
  flashes: DecorationSet
  version: number
  maps: { version: number; map: StepMap }[]
  /** dernière saisie : un séparateur (espace, ponctuation, retour) a été tapé */
  boundary: boolean
  log: AppliedCorrection[]
}

type Meta =
  | { type: 'applied'; records: AppliedCorrection[]; analyzed: { from: number; to: number }[]; issues: { from: number; to: number; issue: Issue }[] }
  | { type: 'ai-applied'; records: AppliedCorrection[] }
  | { type: 'ai-checked'; ranges: { from: number; to: number }[] }
  | { type: 'clear-flash' }
  | { type: 'mark-all-dirty' }

const MAX_MAPS = 2000
const SEPARATOR = /[\s.,;:!?…)»"\]]$/

function mapRange<T extends { from: number; to: number }>(items: T[], tr: Transaction): T[] {
  const out: T[] = []
  for (const it of items) {
    const from = tr.mapping.mapResult(it.from, 1)
    const to = tr.mapping.mapResult(it.to, -1)
    if (from.deletedAcross && to.deletedAcross) continue
    if (to.pos < from.pos) continue
    out.push({ ...it, from: from.pos, to: to.pos })
  }
  return out
}

function mergeRanges(ranges: Range[]): Range[] {
  const sorted = ranges.filter(([a, b]) => b >= a).sort((x, y) => x[0] - y[0])
  const out: Range[] = []
  for (const r of sorted) {
    const last = out[out.length - 1]
    if (last && r[0] <= last[1] + 1) last[1] = Math.max(last[1], r[1])
    else out.push([r[0], r[1]])
  }
  return out
}

function mapRanges(ranges: Range[], tr: Transaction): Range[] {
  return ranges.map(([a, b]) => [tr.mapping.map(a, -1), tr.mapping.map(b, 1)] as Range)
}

function changedRanges(tr: Transaction, prev: Range[]): Range[] {
  const out = mapRanges(prev, tr)
  tr.mapping.maps.forEach((map, i) => {
    const rest = tr.mapping.slice(i + 1)
    map.forEach((_os, _oe, ns, ne) => {
      out.push([rest.map(ns, -1), rest.map(ne, 1)])
    })
  })
  return mergeRanges(out)
}

/** Retire des plages les intervalles déjà analysés. */
function subtract(ranges: Range[], done: { from: number; to: number }[]): Range[] {
  let cur = ranges
  for (const d of done) {
    const next: Range[] = []
    for (const [a, b] of cur) {
      if (b < d.from || a > d.to) next.push([a, b])
      else {
        if (a < d.from) next.push([a, d.from - 1])
        if (b > d.to) next.push([d.to + 1, b])
      }
    }
    cur = next
  }
  return cur
}

export function createAutoCorrectPlugin() {
  return new Plugin<AutoCorrectState>({
    key: autocorrectKey,
    state: {
      init: (_config, state) => ({
        applied: [],
        ignored: [],
        rejected: new Map(),
        dirty: [[0, state.doc.content.size]],
        aiDirty: [[0, state.doc.content.size]],
        issues: DecorationSet.empty,
        flashes: DecorationSet.empty,
        version: 0,
        maps: [],
        boundary: true,
        log: [],
      }),
      apply(tr, value, _old, newState): AutoCorrectState {
        const meta = tr.getMeta(autocorrectKey) as Meta | undefined
        let next: AutoCorrectState = value
        if (tr.docChanged) {
          const maps = value.maps.concat(tr.mapping.maps.map((map, k) => ({ version: value.version + k + 1, map })))
          next = {
            ...value,
            applied: mapRange(value.applied, tr),
            ignored: mapRange(value.ignored, tr),
            issues: value.issues.map(tr.mapping, tr.doc),
            flashes: value.flashes.map(tr.mapping, tr.doc),
            version: value.version + tr.mapping.maps.length,
            maps: maps.length > MAX_MAPS ? maps.slice(-MAX_MAPS) : maps,
            dirty: mapRanges(value.dirty, tr),
            aiDirty: mapRanges(value.aiDirty, tr),
            log: value.log,
          }
          const ours = meta && (meta.type === 'applied' || meta.type === 'ai-applied')
          if (!ours) {
            next.dirty = changedRanges(tr, value.dirty)
            next.aiDirty = changedRanges(tr, value.aiDirty)
            const inserted = insertedText(tr)
            next.boundary = inserted === null ? true : SEPARATOR.test(inserted)
          }
          if (isHistoryTransaction(tr)) next = detectRejections(next, newState.doc)
        }
        if (!meta) return next
        switch (meta.type) {
          case 'applied': {
            next = { ...next, applied: next.applied.concat(meta.records).slice(-500), log: next.log.concat(meta.records).slice(-200) }
            // Les paragraphes analysés ne sont plus « sales ».
            next.dirty = subtract(next.dirty, meta.analyzed)
            let issues = next.issues
            for (const r of meta.analyzed) issues = issues.remove(issues.find(r.from, r.to))
            issues = issues.add(
              newState.doc,
              meta.issues.map(({ from, to, issue }) =>
                Decoration.inline(from, to, {
                  class: 'ac-uncertain',
                  title: issue.suggestions.length ? `${issue.message} — suggestions : ${issue.suggestions.join(', ')}` : issue.message,
                  'data-suggestions': issue.suggestions.join('|'),
                }),
              ),
            )
            next.issues = issues
            next.flashes = next.flashes.add(
              newState.doc,
              meta.records.filter((r) => r.to > r.from).map((r) => Decoration.inline(r.from, r.to, { class: 'ac-fixed' })),
            )
            return next
          }
          case 'ai-applied':
            return {
              ...next,
              applied: next.applied.concat(meta.records).slice(-500),
              log: next.log.concat(meta.records).slice(-200),
              flashes: next.flashes.add(
                newState.doc,
                meta.records.filter((r) => r.to > r.from).map((r) => Decoration.inline(r.from, r.to, { class: 'ac-fixed ac-fixed-ai' })),
              ),
            }
          case 'ai-checked':
            return { ...next, aiDirty: subtract(next.aiDirty, meta.ranges) }
          case 'clear-flash':
            return { ...next, flashes: DecorationSet.empty }
          case 'mark-all-dirty':
            return { ...next, dirty: [[0, newState.doc.content.size]], aiDirty: [[0, newState.doc.content.size]] }
        }
      },
    },
    props: {
      decorations(state) {
        const s = autocorrectKey.getState(state)
        if (!s) return null
        return s.issues.add(state.doc, s.flashes.find())
      },
    },
  })
}

function insertedText(tr: Transaction): string | null {
  let text: string | null = null
  for (const step of tr.steps) {
    const slice = (step as unknown as { slice?: { content: PMNode['content'] } }).slice
    if (slice) text = slice.content.textBetween(0, slice.content.size, '\n', '\n')
  }
  return text
}

/** Après un Ctrl+Z : une correction dont le texte d'origine est revenu est mémorisée comme refusée. */
function detectRejections(s: AutoCorrectState, doc: PMNode): AutoCorrectState {
  const applied: AppliedCorrection[] = []
  const ignored = [...s.ignored]
  const rejected = new Map(s.rejected)
  for (const a of s.applied) {
    const current = safeText(doc, a.from, a.to)
    if (current === a.original && a.original !== a.replacement) {
      ignored.push({ from: a.from, to: a.to, text: a.original })
      const key = `${a.original}→${a.replacement}`
      rejected.set(key, (rejected.get(key) ?? 0) + 1)
    } else applied.push(a)
  }
  return { ...s, applied, ignored, rejected }
}

function safeText(doc: PMNode, from: number, to: number): string | null {
  if (from < 0 || to > doc.content.size || from > to) return null
  return doc.textBetween(from, to, '\n', '￼')
}

export interface Block {
  /** position du premier caractère du paragraphe */
  start: number
  text: string
  node: PMNode
}

export function blocksAt(doc: PMNode, ranges: Range[] | 'all'): Block[] {
  const seen = new Map<number, Block>()
  const visit = (from: number, to: number) => {
    doc.nodesBetween(from, to, (node, pos) => {
      if (node.isTextblock) {
        if (!seen.has(pos)) seen.set(pos, { start: pos + 1, node, text: node.textBetween(0, node.content.size, undefined, '\uFFFC') })
        return false
      }
      return true
    })
  }
  const size = doc.content.size
  if (ranges === 'all') visit(0, size)
  else for (const [a, b] of ranges) visit(Math.max(0, Math.min(a, size)), Math.max(0, Math.min(Math.max(a, b), size)))
  return [...seen.values()].sort((a, b) => a.start - b.start)
}

export interface LocalSettings {
  frenchSpacing: boolean
  typographicApostrophe: boolean
}

export interface ComputeOptions {
  blocks?: Range[] | 'all'
  maxBlocks?: number
  settings: LocalSettings
  now?: number
}

export interface ComputeResult {
  tr: Transaction | null
  /** blocs restant à traiter (gros collage) */
  remaining: number
}

function overlapsSelection(state: EditorState, from: number, to: number): boolean {
  const { from: sf, to: st, empty } = state.selection
  if (empty) return false
  return from < st && to > sf
}

export function computeLocalCorrections(state: EditorState, lex: Lexicon, opts: ComputeOptions): ComputeResult {
  const ps = autocorrectKey.getState(state)
  if (!ps) return { tr: null, remaining: 0 }
  const all = blocksAt(state.doc, opts.blocks ?? ps.dirty)
  const maxBlocks = opts.maxBlocks ?? 40
  const blocks = all.slice(0, maxBlocks)
  const tr = state.tr
  const records: AppliedCorrection[] = []
  const issues: { from: number; to: number; issue: Issue }[] = []
  const now = opts.now ?? Date.now()
  const head = state.selection.head
  const edits: { from: number; to: number; original: string; replacement: string; rule: string }[] = []

  for (const b of blocks) {
    const end = b.start + b.text.length
    const cursor = head >= b.start && head <= end ? head - b.start : null
    const analyzeOpts: AnalyzeOptions = {
      cursor,
      frenchSpacing: opts.settings.frenchSpacing,
      typographicApostrophe: opts.settings.typographicApostrophe,
      isIgnored: (s, e, original, replacement) => {
        const from = b.start + s
        const to = b.start + e
        if (overlapsSelection(state, from, to)) return true
        if ((ps.rejected.get(`${original}→${replacement}`) ?? 0) >= 2) return true
        return ps.ignored.some((ig) => ig.from < to + 1 && ig.to > from - 1 && ig.text.includes(original.trim() || original))
      },
    }
    const res = analyze(b.text, lex, analyzeOpts)
    for (const e of res.edits) {
      if (e.original.includes('￼') || e.replacement.includes('￼')) continue
      edits.push({ from: b.start + e.start, to: b.start + e.end, original: e.original, replacement: e.replacement, rule: e.rules.map((r) => r.rule).join('+') })
    }
    for (const is of res.issues) issues.push({ from: b.start + is.start, to: b.start + is.end, issue: is })
  }

  // Application de droite à gauche : les positions à gauche restent valides.
  edits.sort((a, b) => b.from - a.from)
  const stepIndex: number[] = []
  for (const e of edits) {
    stepIndex.push(tr.steps.length)
    if (e.replacement === '') tr.delete(e.from, e.to)
    else tr.insertText(e.replacement, e.from, e.to)
  }
  edits.forEach((e, k) => {
    const rest = tr.mapping.slice(stepIndex[k] + 1)
    const from = rest.map(e.from, -1)
    records.push({ from, to: from + e.replacement.length, original: e.original, replacement: e.replacement, source: 'local', rule: e.rule, at: now })
  })
  const analyzed = blocks.map((b) => ({ from: tr.mapping.map(b.start - 1, -1), to: tr.mapping.map(b.start + b.text.length + 1, 1) }))
  const mappedIssues = issues
    .map((i) => ({ ...i, from: tr.mapping.map(i.from, 1), to: tr.mapping.map(i.to, -1) }))
    .filter((i) => i.to > i.from)
  tr.setMeta(autocorrectKey, { type: 'applied', records, analyzed, issues: mappedIssues } satisfies Meta)
  if (edits.length === 0) tr.setMeta('addToHistory', false)
  else closeHistory(tr)
  return { tr, remaining: Math.max(0, all.length - blocks.length) }
}

/** Transaction vide qui ferme le groupe d'annulation : la frappe suivante ne fusionne pas avec la correction. */
export function historyBarrier(state: EditorState): Transaction {
  return closeHistory(state.tr).setMeta('addToHistory', false)
}

// ---------------------------------------------------------------------------
// Corrections asynchrones (IA)

export interface RemoteHunk {
  /** positions absolues dans le document au moment de la requête */
  from: number
  to: number
  original: string
  replacement: string
}

export function mappingSince(state: EditorState, version: number): Mapping | null {
  const ps = autocorrectKey.getState(state)
  if (!ps) return null
  if (version === ps.version) return new Mapping()
  const maps = ps.maps.filter((m) => m.version > version)
  if (maps.length !== ps.version - version) return null // historique de positions perdu
  return new Mapping(maps.map((m) => m.map))
}

export function currentVersion(state: EditorState): number {
  return autocorrectKey.getState(state)?.version ?? 0
}

/**
 * Applique des corrections calculées sur la version `version` du document.
 * Chaque modification est remappée ; elle est abandonnée si le texte a changé
 * entre-temps, si elle touche la zone de frappe ou une correction refusée.
 */
export function applyRemoteHunks(state: EditorState, version: number, hunks: RemoteHunk[], now = Date.now()): Transaction | null {
  const mapping = mappingSince(state, version)
  const ps = autocorrectKey.getState(state)
  if (!mapping || !ps) return null
  const head = state.selection.head
  const valid: (RemoteHunk & { rule: string })[] = []
  for (const h of hunks) {
    const from = mapping.mapResult(h.from, 1)
    const to = mapping.mapResult(h.to, -1)
    if (from.deleted || to.deleted || to.pos < from.pos) continue
    if (safeText(state.doc, from.pos, to.pos) !== h.original) continue
    // zone de frappe : le mot sous le curseur
    const $h = state.doc.resolve(head)
    const blockStart = $h.start()
    const blockText = $h.parent.textBetween(0, $h.parent.content.size, undefined, '￼')
    const off = head - blockStart
    let zs = off
    let ze = off
    while (zs > 0 && /[\p{L}\p{N}'’-]/u.test(blockText[zs - 1])) zs--
    while (ze < blockText.length && /[\p{L}\p{N}'’-]/u.test(blockText[ze])) ze++
    if (from.pos <= blockStart + ze && to.pos >= blockStart + zs) continue
    if (overlapsSelection(state, from.pos, to.pos)) continue
    if (ps.ignored.some((ig) => ig.from < to.pos && ig.to > from.pos)) continue
    if ((ps.rejected.get(`${h.original}→${h.replacement}`) ?? 0) >= 1) continue
    // Ne jamais défaire une correction locale.
    if (ps.applied.some((a) => a.from < to.pos && a.to > from.pos && h.replacement.includes(a.original) && !h.original.includes(a.original))) continue
    valid.push({ ...h, from: from.pos, to: to.pos, rule: 'ia' })
  }
  if (valid.length === 0) return null
  valid.sort((a, b) => b.from - a.from)
  const tr = state.tr
  const stepIndex: number[] = []
  for (const v of valid) {
    stepIndex.push(tr.steps.length)
    if (v.replacement === '') tr.delete(v.from, v.to)
    else tr.insertText(v.replacement, v.from, v.to)
  }
  const records: AppliedCorrection[] = valid.map((v, k) => {
    const from = tr.mapping.slice(stepIndex[k] + 1).map(v.from, -1)
    return { from, to: from + v.replacement.length, original: v.original, replacement: v.replacement, source: 'ai', rule: v.rule, at: now }
  })
  tr.setMeta(autocorrectKey, { type: 'ai-applied', records } satisfies Meta)
  closeHistory(tr)
  return tr
}

export function markAiChecked(state: EditorState, ranges: { from: number; to: number }[]): Transaction {
  return state.tr.setMeta(autocorrectKey, { type: 'ai-checked', ranges } satisfies Meta).setMeta('addToHistory', false)
}

export function clearFlash(state: EditorState): Transaction {
  return state.tr.setMeta(autocorrectKey, { type: 'clear-flash' } satisfies Meta).setMeta('addToHistory', false)
}

export function markAllDirty(state: EditorState): Transaction {
  return state.tr.setMeta(autocorrectKey, { type: 'mark-all-dirty' } satisfies Meta).setMeta('addToHistory', false)
}
