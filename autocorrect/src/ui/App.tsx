'use client'

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { EditorContent, useEditor } from '@tiptap/react'
import StarterKit from '@tiptap/starter-kit'
import { Placeholder } from '@tiptap/extensions'
import { Lexicon } from '../engine/lexicon'
import { AutoCorrect, AutoCorrectController, DEFAULT_SETTINGS, type Settings, type Status } from '../editor/controller'
import type { AppliedCorrection } from '../editor/core'
import {
  addVersion,
  clearVersions,
  loadDictionary,
  loadDoc,
  loadSettings,
  loadVersions,
  saveDictionary,
  saveDoc,
  saveSettings,
  type Version,
} from './storage'

const WORD = /[\p{L}\p{N}]+(?:['’-][\p{L}\p{N}]+)*/gu
const countWords = (t: string) => t.match(WORD)?.length ?? 0

type Panel = null | 'history' | 'corrections' | 'settings'

interface Popover {
  x: number
  y: number
  from: number
  to: number
  word: string
  suggestions: string[]
}

export function App() {
  const controller = useMemo(() => new AutoCorrectController(), [])
  const lexRef = useRef<Lexicon | null>(null)
  const [settings, setSettings] = useState<Settings>(DEFAULT_SETTINGS)
  const [status, setStatus] = useState<Status>(controller.status)
  const [words, setWords] = useState(0)
  const [chars, setChars] = useState(0)
  const [panel, setPanel] = useState<Panel>(null)
  const [versions, setVersions] = useState<Version[]>([])
  const [log, setLog] = useState<AppliedCorrection[]>([])
  const [toast, setToast] = useState<string | null>(null)
  const [popover, setPopover] = useState<Popover | null>(null)
  const [dictionary, setDictionary] = useState<string[]>([])
  const [aiModel, setAiModel] = useState<string | null>(null)
  const saveTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const lastSnapshot = useRef('')

  const editor = useEditor({
    immediatelyRender: false,
    extensions: [
      StarterKit.configure({ link: false }),
      Placeholder.configure({ placeholder: 'Commencez à écrire… les fautes évidentes disparaîtront d’elles-mêmes.' }),
      AutoCorrect.configure({ controller }),
    ],
    content: loadDoc() ?? '',
    autofocus: 'end',
    editorProps: {
      attributes: { class: 'writing', spellcheck: 'false', 'aria-label': 'Zone de rédaction', autocapitalize: 'off', autocorrect: 'off' },
    },
    onCreate: ({ editor }) => {
      const t = editor.getText()
      setWords(countWords(t))
      setChars(t.length)
      lastSnapshot.current = t
    },
    onUpdate: ({ editor }) => {
      const t = editor.getText()
      setWords(countWords(t))
      setChars(t.length)
      if (saveTimer.current) clearTimeout(saveTimer.current)
      saveTimer.current = setTimeout(() => saveDoc(editor.getJSON()), 800)
      setPopover(null)
    },
  })

  // Chargement du dictionnaire (une fois, mis en cache par le navigateur).
  useEffect(() => {
    let cancelled = false
    const words = loadDictionary()
    setDictionary(words)
    fetch('/dict/fr.txt')
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`)
        return r.text()
      })
      .then((txt) => Lexicon.parseAsync(txt))
      .then((lex) => {
        if (cancelled) return
        lex.addPersonal(words)
        lexRef.current = lex
        controller.setLexicon(lex)
      })
      .catch(() => !cancelled && controller.setLexicon(null, 'error'))
    fetch('/api/correct')
      .then((r) => r.json())
      .then((d: { enabled: boolean; model: string | null }) => {
        controller.setAiAvailable(Boolean(d.enabled))
        setAiModel(d.model)
      })
      .catch(() => controller.setAiAvailable(false))
    const offStatus = controller.onStatus(setStatus)
    const offLog = controller.onCorrections((l) => setLog([...l].reverse()))
    return () => {
      cancelled = true
      offStatus()
      offLog()
    }
  }, [controller])

  // Réglages persistés.
  useEffect(() => {
    const s = loadSettings()
    setSettings(s)
    controller.updateSettings(s)
    setVersions(loadVersions())
  }, [controller])

  const update = useCallback(
    (patch: Partial<Settings>) => {
      setSettings((prev) => {
        const next = { ...prev, ...patch }
        saveSettings(next)
        return next
      })
      controller.updateSettings(patch)
    },
    [controller],
  )

  // Historique des versions : instantané toutes les 30 s si le texte a changé.
  const snapshot = useCallback(() => {
    if (!editor) return
    const t = editor.getText()
    if (!t.trim() || t === lastSnapshot.current) return
    lastSnapshot.current = t
    setVersions(addVersion({ at: Date.now(), words: countWords(t), preview: t.slice(0, 140), doc: editor.getJSON() }))
  }, [editor])

  useEffect(() => {
    const id = setInterval(snapshot, 30_000)
    const onHide = () => {
      snapshot()
      if (editor) saveDoc(editor.getJSON())
    }
    window.addEventListener('pagehide', onHide)
    return () => {
      clearInterval(id)
      window.removeEventListener('pagehide', onHide)
    }
  }, [snapshot, editor])

  const flash = (msg: string) => {
    setToast(msg)
    setTimeout(() => setToast(null), 1800)
  }

  const copy = async () => {
    if (!editor) return
    const text = editor.getText({ blockSeparator: '\n\n' })
    try {
      if (typeof ClipboardItem !== 'undefined' && navigator.clipboard.write) {
        await navigator.clipboard.write([
          new ClipboardItem({
            'text/plain': new Blob([text], { type: 'text/plain' }),
            'text/html': new Blob([editor.getHTML()], { type: 'text/html' }),
          }),
        ])
      } else await navigator.clipboard.writeText(text)
      flash('Texte copié')
    } catch {
      flash('Copie impossible')
    }
  }

  const restore = (v: Version) => {
    if (!editor) return
    snapshot() // la version actuelle reste récupérable
    editor.chain().focus().setContent(v.doc, { emitUpdate: true }).run()
    setPanel(null)
    flash('Version restaurée — Ctrl+Z pour revenir')
  }

  // Mots soulignés (doute) : suggestions au clic.
  const onEditorClick = (e: React.MouseEvent) => {
    const el = (e.target as HTMLElement).closest('.ac-uncertain') as HTMLElement | null
    if (!el || !editor) return setPopover(null)
    const from = editor.view.posAtDOM(el, 0)
    const word = el.textContent ?? ''
    const rect = el.getBoundingClientRect()
    const suggestions = (el.dataset.suggestions ?? '').split('|').filter(Boolean)
    setPopover({ x: rect.left, y: rect.bottom + 6, from, to: from + word.length, word, suggestions })
  }

  const applySuggestion = (p: Popover, s: string) => {
    if (!editor) return
    const { state, view } = editor
    if (state.doc.textBetween(p.from, p.to) === p.word) view.dispatch(state.tr.insertText(s, p.from, p.to))
    setPopover(null)
    editor.commands.focus()
  }

  const addToDictionary = (word: string) => {
    const next = [...new Set([...dictionary, word])]
    setDictionary(next)
    saveDictionary(next)
    lexRef.current?.addPersonal([word])
    controller.refresh()
    setPopover(null)
    flash(`« ${word} » ajouté au dictionnaire`)
  }

  const removeFromDictionary = (word: string) => {
    const next = dictionary.filter((w) => w !== word)
    setDictionary(next)
    saveDictionary(next)
    lexRef.current?.removePersonal(word)
    controller.refresh()
  }

  const indicator = describeStatus(status, settings)

  return (
    <div className="shell" onKeyDown={(e) => e.key === 'Escape' && (setPanel(null), setPopover(null))}>
      <header className="topbar">
        <div className="brand">
          <span className={`dot ${indicator.tone}`} aria-hidden />
          <span className="brand-name">Auto-Correct</span>
          <span className="indicator" title={indicator.detail}>
            {indicator.label}
          </span>
        </div>

        <div className="segmented" role="radiogroup" aria-label="Mode de correction">
          <button role="radio" aria-checked={settings.mode === 'correct'} className={settings.mode === 'correct' ? 'on' : ''} onClick={() => update({ mode: 'correct' })}>
            Correction
          </button>
          <button
            role="radio"
            aria-checked={settings.mode === 'improve'}
            className={settings.mode === 'improve' ? 'on' : ''}
            onClick={() => update({ mode: 'improve' })}
            disabled={!controller.aiAvailable}
            title={controller.aiAvailable ? 'Corrige et améliore légèrement les phrases terminées' : 'Nécessite la configuration de l’API (voir README)'}
          >
            Correction + amélioration
          </button>
        </div>

        <div className="actions">
          <label className="switch" title="Activer ou suspendre la correction automatique">
            <input type="checkbox" checked={settings.enabled} onChange={(e) => update({ enabled: e.target.checked })} />
            <span className="track" aria-hidden>
              <span className="thumb" />
            </span>
            <span className="switch-label">Auto</span>
          </label>
          <IconButton label="Copier le texte" onClick={copy}>
            <path d="M9 9h10v10H9z M5 15V5h10" />
          </IconButton>
          <IconButton label="Historique des versions" active={panel === 'history'} onClick={() => (snapshot(), setPanel(panel === 'history' ? null : 'history'))}>
            <path d="M12 7v5l3 2 M3.5 12a8.5 8.5 0 1 0 2.5-6 M3 4v4h4" />
          </IconButton>
          <IconButton label="Réglages" active={panel === 'settings'} onClick={() => setPanel(panel === 'settings' ? null : 'settings')}>
            <path d="M4 7h10 M18 7h2 M4 17h4 M12 17h8 M16 5v4 M10 15v4" />
          </IconButton>
        </div>
      </header>

      <main className="page" onClick={onEditorClick}>
        <EditorContent editor={editor} />
      </main>

      <footer className="statusbar">
        <span>
          {words.toLocaleString('fr-FR')} mot{words > 1 ? 's' : ''}
        </span>
        <span className="sep">·</span>
        <span>{chars.toLocaleString('fr-FR')} caractères</span>
        <span className="sep">·</span>
        <button className="link" onClick={() => setPanel(panel === 'corrections' ? null : 'corrections')}>
          {status.corrections} correction{status.corrections > 1 ? 's' : ''}
        </button>
      </footer>

      {popover && (
        <div className="popover" style={{ left: Math.min(popover.x, (typeof window !== 'undefined' ? window.innerWidth : 800) - 240), top: popover.y }} role="menu">
          {popover.suggestions.length > 0 ? (
            popover.suggestions.map((s) => (
              <button key={s} role="menuitem" className="suggestion" onClick={() => applySuggestion(popover, s)}>
                {s}
              </button>
            ))
          ) : (
            <div className="muted small">Aucune suggestion sûre</div>
          )}
          <hr />
          <button role="menuitem" className="menu-link" onClick={() => addToDictionary(popover.word)}>
            Ajouter « {popover.word} » au dictionnaire
          </button>
        </div>
      )}

      {panel && (
        <aside className="panel" aria-label="Panneau">
          <div className="panel-head">
            <h2>{panel === 'history' ? 'Versions précédentes' : panel === 'corrections' ? 'Corrections appliquées' : 'Réglages'}</h2>
            <button className="close" onClick={() => setPanel(null)} aria-label="Fermer">
              ×
            </button>
          </div>

          {panel === 'history' && (
            <div className="panel-body">
              {versions.length === 0 && <p className="muted">Les versions sont enregistrées automatiquement toutes les 30 secondes, uniquement dans ce navigateur.</p>}
              <ul className="list">
                {versions.map((v) => (
                  <li key={v.id}>
                    <button className="version" onClick={() => restore(v)}>
                      <span className="when">
                        {new Date(v.at).toLocaleString('fr-FR', { dateStyle: 'short', timeStyle: 'short' })} · {v.words} mots
                      </span>
                      <span className="preview">{v.preview}</span>
                    </button>
                  </li>
                ))}
              </ul>
              {versions.length > 0 && (
                <button className="menu-link danger" onClick={() => (clearVersions(), setVersions([]))}>
                  Effacer l’historique
                </button>
              )}
            </div>
          )}

          {panel === 'corrections' && (
            <div className="panel-body">
              <p className="muted small">Ctrl+Z (⌘Z) annule la dernière correction ; elle ne sera pas réappliquée à cet endroit.</p>
              {log.length === 0 && <p className="muted">Aucune correction pour l’instant.</p>}
              <ul className="list">
                {log.slice(0, 80).map((c, i) => (
                  <li key={`${c.at}-${i}`} className="correction">
                    <del>{visible(c.original)}</del>
                    <span className="arrow">→</span>
                    <ins>{visible(c.replacement)}</ins>
                    <span className={`tag ${c.source}`}>{c.source === 'ai' ? 'IA' : 'local'}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}

          {panel === 'settings' && (
            <div className="panel-body settings">
              <Toggle label="Typographie française" hint="Espaces insécables avant ; : ! ? et dans les guillemets « »." checked={settings.frenchSpacing} onChange={(v) => update({ frenchSpacing: v })} />
              <Toggle label="Apostrophe typographique" hint="Remplace ' par ’." checked={settings.typographicApostrophe} onChange={(v) => update({ typographicApostrophe: v })} />
              <Toggle
                label="Analyse contextuelle (IA)"
                hint={
                  controller.aiAvailable
                    ? `Les phrases terminées sont envoyées au serveur puis à l’API Anthropic (${aiModel}) pour les accords et homophones. Le reste est corrigé localement, dans votre navigateur.`
                    : 'Indisponible : aucune clé API configurée sur le serveur. La correction locale reste active.'
                }
                checked={settings.ai && controller.aiAvailable}
                disabled={!controller.aiAvailable}
                onChange={(v) => update({ ai: v })}
              />
              <h3>Dictionnaire personnel</h3>
              {dictionary.length === 0 ? (
                <p className="muted small">Cliquez sur un mot souligné pour l’ajouter.</p>
              ) : (
                <ul className="chips">
                  {dictionary.map((w) => (
                    <li key={w}>
                      {w}
                      <button aria-label={`Retirer ${w}`} onClick={() => removeFromDictionary(w)}>
                        ×
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </aside>
      )}

      {toast && (
        <div className="toast" role="status">
          {toast}
        </div>
      )}
    </div>
  )
}

function visible(s: string) {
  if (s === '') return '∅'
  if (/^[\u00a0\u202f]$/.test(s)) return 'espace insécable'
  if (s === ' ') return 'espace'
  return s.replace(/ /g, '⍽').replace(/ /g, '⍽').replace(/ /g, '␣')
}

function describeStatus(s: Status, settings: Settings): { label: string; detail: string; tone: string } {
  if (!settings.enabled) return { label: 'En pause', detail: 'La correction automatique est suspendue.', tone: 'off' }
  if (s.lexicon === 'loading') return { label: 'Préparation…', detail: 'Chargement du dictionnaire local.', tone: 'wait' }
  if (s.lexicon === 'error') return { label: 'Dictionnaire indisponible', detail: 'Exécutez « npm run build:dict ».', tone: 'err' }
  const ai =
    s.ai === 'unavailable'
      ? 'IA non configurée'
      : s.ai === 'off'
        ? 'IA désactivée'
        : s.ai === 'error'
          ? `IA en erreur : ${s.aiMessage ?? ''}`
          : s.ai === 'busy'
            ? 'IA : analyse en cours'
            : 'IA contextuelle active'
  return { label: 'Correction active', detail: `Moteur local actif · ${ai}`, tone: s.ai === 'busy' ? 'busy' : s.ai === 'error' ? 'warn' : 'on' }
}

function IconButton({ label, onClick, children, active }: { label: string; onClick: () => void; children: React.ReactNode; active?: boolean }) {
  return (
    <button className={`icon ${active ? 'active' : ''}`} onClick={onClick} aria-label={label} title={label}>
      <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
        {children}
      </svg>
    </button>
  )
}

function Toggle({ label, hint, checked, onChange, disabled }: { label: string; hint: string; checked: boolean; onChange: (v: boolean) => void; disabled?: boolean }) {
  return (
    <label className={`toggle ${disabled ? 'disabled' : ''}`}>
      <span>
        <span className="toggle-label">{label}</span>
        <span className="muted small">{hint}</span>
      </span>
      <span className="switch">
        <input type="checkbox" checked={checked} disabled={disabled} onChange={(e) => onChange(e.target.checked)} />
        <span className="track" aria-hidden>
          <span className="thumb" />
        </span>
      </span>
    </label>
  )
}
