# Auto-Correct — éditeur qui corrige pendant que vous écrivez

L'utilisateur écrit normalement. Les fautes évidentes disparaissent toutes seules, sans clic, sans suggestion à accepter, sans que le curseur bouge. Ce qui est douteux n'est jamais modifié : au mieux, c'est discrètement souligné.

```
Saisi    : Bonjour, je vous envoi les document que vous m'avez demander.
Résultat : Bonjour, je vous envoie les documents que vous m'avez demandés.
```

Cette phrase est corrigée **entièrement en local**, dans le navigateur, sans IA et sans réseau. Le test `tests/e2e/editor.spec.ts` la vérifie avec une vraie frappe clavier dans Chromium.

---

## 1. Faisabilité

**Oui, c'est réaliste**, à une condition : ne pas tout confier à un modèle de langage.

| Contrainte | Conséquence technique |
|---|---|
| Correction à chaque mot, sans latence perceptible | Le gros du travail est fait **localement** (dictionnaire + règles), en ~1 ms par paragraphe. Un appel d'IA prend 0,5 à 3 s : beaucoup trop lent pour corriger au fil de la frappe. |
| Aucune confirmation demandée | Chaque correction doit être **certaine**. Le moteur distingue certain / douteux et n'applique que le certain. |
| Ne jamais casser le texte | Rien ne touche au mot en cours de frappe, à la sélection, aux URL, aux e-mails, aux noms propres ni aux sigles. Une phrase qui n'est pas en français est ignorée. |
| Ctrl+Z | Chaque correction est une étape d'annulation séparée. Une correction annulée **n'est jamais réappliquée** au même endroit. |
| Coût | L'IA ne reçoit que des **phrases terminées**, une seule fois (cache), par lots de 10 au plus. Elle n'est jamais sollicitée à chaque caractère. |

**Limites honnêtes** : aucun correcteur n'est parfait. Les homophones (*a/à*, *ou/où*, *ces/ses*), les accords à longue distance et le genre de la personne qui écrit (*je suis allé·e*) demandent du contexte. Sans IA, ces cas sont **laissés tels quels** (situation B). Avec l'IA, ils sont corrigés quand le modèle est sûr, puis revérifiés localement.

## 2. Solutions existantes et leurs limites

| Outil | Points forts | Limite par rapport au besoin |
|---|---|---|
| Antidote (Druide) | Analyse du français très fine | Correction **a posteriori** : il faut ouvrir le correcteur et valider chaque suggestion |
| LanguageTool | Libre, multilingue, API, extensions | Souligne et propose, ne corrige pas automatiquement |
| Grammalecte | Libre, excellent en français, règles transparentes | Suggestions à valider, pas d'application automatique |
| Correcteurs de Word / Google Docs | Intégrés | Souligner et cliquer ; peu d'accords complexes |
| Claviers mobiles (iOS, Android) | Correction automatique réelle | Mot par mot, sans grammaire ; remplacements erronés fréquents (aucune notion de certitude) |
| Grammarly | UX très fluide | Centré sur l'anglais ; suggestions à accepter |

Ce qui manque sur le marché, c'est la **correction automatique fiable** : appliquer sans demander, mais seulement ce qui est sûr, et rester annulable. C'est le cœur de ce projet.

## 3. Architecture

```
┌────────────────────────── navigateur ───────────────────────────┐
│  Éditeur TipTap (ProseMirror)                                   │
│    │ chaque transaction                                         │
│    ▼                                                            │
│  Plugin autocorrect (src/editor/core.ts)                        │
│    • suit les paragraphes modifiés, les corrections appliquées  │
│      et celles annulées (positions remappées à chaque frappe)   │
│    │ séparateur tapé → 60 ms / pause → 700 ms                   │
│    ▼                                                            │
│  Moteur local (src/engine/) — 452 000 formes, 0 réseau          │
│    tokeniseur protégé → règles → certain ? → transaction unique │
│    │ phrases terminées, après 1,2 s d'inactivité                │
└────┼────────────────────────────────────────────────────────────┘
     ▼ POST /api/correct (Next.js, clé API côté serveur seulement)
┌────────────────────────── serveur ──────────────────────────────┐
│  limite de débit · taille bornée · cache SHA-256 · aucun log    │
│  API Claude : sortie JSON structurée, effort « low »            │
└─────────────────────────────────────────────────────────────────┘
     ▼ réponse = phrase corrigée (jamais des positions)
  diff mot à mot → garde-fous (validate.ts) → remappage → application
```

### Choix techniques

- **Next.js 16 + React 19 + TypeScript** : application web et route API dans un seul projet, déployable telle quelle (Vercel, Docker…).
- **TipTap 3 (ProseMirror)** plutôt que Lexical. ProseMirror fournit exactement les primitives dont on a besoin : transactions atomiques, **remappage des positions** à travers chaque modification (`Mapping`), sélection remappée automatiquement (le curseur ne bouge pas), historique d'annulation contrôlable (`closeHistory`). Tout le cœur se teste en Node, sans navigateur.
- **Moteur local maison**. Nous avons essayé nspell (Hunspell en JS) : 6,5 s de chargement et 430 Mo de mémoire pour le français, inutilisable dans un navigateur. À la place, le dictionnaire Hunspell de Grammalecte est **développé une fois au build** en un lexique plat de 452 000 formes fléchies avec des étiquettes morphologiques (verbe, infinitif, nom + pluriel) et des fréquences d'usage. Il fait 6,2 Mo, soit **1,3 Mo compressé**, se charge par tranches sans bloquer la frappe, et toute recherche est en O(1).
- **IA hybride** : le modèle ne voit que des phrases terminées, et sa réponse est traitée comme une *proposition* :
  1. diff mot à mot calculé localement (on ne fait jamais confiance aux positions d'un modèle) ;
  2. garde-fous : nombres, URL, e-mails et noms propres identiques, longueur stable, pas de reformulation en mode « Correction », pas de négation ajoutée ou retirée en mode « Amélioration » ;
  3. remappage à travers ce que l'utilisateur a tapé entre-temps. Si le texte visé a changé, la correction est abandonnée.

### Les trois situations

| Situation | Exemple | Comportement |
|---|---|---|
| **A — erreur certaine** | `deja` → `déjà`, `les document` → `les documents`, `vous avez demander` → `demandé`, `je ai` → `j'ai` | corrigée immédiatement |
| **B — erreur probable mais ambiguë** | `facile a faire` (a/à), `je vais le marché`, `chein` (chien ? chemin ? hein ?) | **jamais modifiée** ; mot inconnu souligné en pointillé, suggestions au clic |
| **C — texte correct ou protégé** | URL, `iPhone`, `SNCF`, `Maëlle`, phrase en anglais, `` `code` `` | rien ne change |

Critères de certitude du moteur local (`src/engine/engine.ts`) :

- **Accents** : une seule forme accentuée possible, ou une forme 20 fois plus fréquente que les autres.
- **Fautes de frappe** (distance 1) : mot d'au moins 5 lettres, candidat fréquent et 30 fois plus fréquent que le suivant.
- **Grammaire** : motifs sans ambiguïté, avec exclusions explicites. Par exemple, `pour le marché` n'est pas transformé en infinitif, et `les heures que j'ai travaillé` ne s'accorde pas.
- **Accord du participe avec le COD antéposé** : seulement si le genre du nom est connu (lexique de noms courants), sinon on s'abstient.

## 4. Périmètre du MVP (livré)

- [x] Éditeur web central, épuré, thème clair et sombre, responsive
- [x] Correction automatique en temps réel, locale et sans réseau : orthographe, accents, élisions, conjugaison (confusions nom/verbe, participe/infinitif), accords (pluriel, participe), doublons, majuscules, ponctuation, typographie française (espaces insécables, guillemets)
- [x] Zone de frappe protégée, curseur et sélection préservés, saisie IME respectée
- [x] Ctrl+Z / Cmd+Z annule une correction, qui n'est plus réappliquée ; rétablir avec Ctrl+Maj+Z
- [x] Gros collages traités par lots de 40 paragraphes, sans geler l'interface
- [x] Mode 1 « Correction » (par défaut) et mode 2 « Correction + amélioration » (IA, phrases terminées uniquement)
- [x] Indicateur discret, interrupteur, compteur de mots et de caractères, bouton Copier (texte et HTML)
- [x] Historique des versions (instantané toutes les 30 s, restauration annulable), journal des corrections appliquées
- [x] Dictionnaire personnel (clic sur un mot souligné)
- [x] Confidentialité : correction locale par défaut ; texte, versions et dictionnaire stockés **uniquement dans le navigateur** ; clé API côté serveur ; aucun journal du texte

Hors MVP : comptes et synchronisation, extension Chrome, application de bureau, langues autres que le français.

## 5. Lancer et tester en local

Prérequis : Node.js 20 ou plus récent.

```bash
cd autocorrect
npm install
npm run dev            # génère le lexique au premier lancement (~8 s), puis http://localhost:3000
```

L'éditeur fonctionne immédiatement **sans aucune clé**, avec le moteur local.

### Activer la couche IA (facultatif)

```bash
cp .env.example .env.local
# puis renseignez ANTHROPIC_API_KEY=… dans .env.local, et relancez npm run dev
```

Sans clé, l'interface l'indique (« IA non configurée ») et le mode « Correction + amélioration » est désactivé. Rien n'est simulé.

- Modèle par défaut : `claude-opus-5-5`, avec `effort: low` pour limiter latence et coût, et repli serveur automatique en cas de refus (`fallbacks: "default"`).
- Pour réduire fortement le coût, définissez `AUTOCORRECT_MODEL=claude-haiku-5-5`, après avoir vérifié la qualité sur vos textes.
- **Ordre de grandeur du coût** (estimation, à mesurer en conditions réelles) : environ 1 000 jetons d'entrée et 300 de sortie par lot de phrases (raisonnement compris), soit environ 1 centime de dollar par lot avec Opus 5.5 et environ 40 fois moins avec Haiku 5.5. Chaque phrase n'est envoyée qu'une fois (cache client et serveur).

### Tests

```bash
npm test               # 86 tests unitaires et d'intégration (Vitest, ~3 s)
npm run typecheck
npm run build && npm run test:e2e   # 10 tests navigateur (Playwright, ~45 s)
```

Si Playwright ne trouve pas son navigateur : `npx playwright install chromium`, ou indiquez-en un avec `CHROMIUM_PATH=/chemin/vers/chrome npm run test:e2e`.

| Fichier | Ce qui est vérifié |
|---|---|
| `tests/unit/engine.test.ts` | ~60 phrases : situations A, B et C, protections (URL, sigles, noms propres, anglais), zone de frappe, refus, idempotence, performance |
| `tests/unit/editor.test.ts` | frappe caractère par caractère dans ProseMirror, curseur au milieu du texte, sélection, **Ctrl+Z / rétablir**, correction refusée non réappliquée, **réponses asynchrones remappées ou abandonnées**, collage de 120 paragraphes |
| `tests/unit/diff-validate.test.ts` | diff mot à mot, garde-fous IA, composition des passes, découpage en phrases |
| `tests/unit/ai.test.ts` | forme exacte de la requête à l'API Claude (faux serveur local), cache, refus et JSON invalide |
| `tests/e2e/editor.spec.ts` | Chromium réel : frappe, curseur, Ctrl+Z, protections, collage de 200 paragraphes, interrupteur, copie, historique, couche IA simulée (y compris une réponse tardive) |

## 6. Structure

```
autocorrect/
├─ app/                       Next.js (page, styles, route /api/correct)
├─ src/engine/                moteur pur TypeScript, sans DOM, réutilisable partout
│  ├─ lexicon.ts              lexique compact (formes, morphologie, fréquences)
│  ├─ tokenizer.ts            jetons + protections (URL, e-mails, sigles, code…)
│  ├─ engine.ts               règles, certitude, passes successives
│  ├─ changeset.ts            composition des passes en coordonnées d'origine
│  ├─ data.ts                 listes linguistiques volontairement prudentes
│  ├─ diff.ts / validate.ts   exploitation sûre des réponses de l'IA
│  └─ sentences.ts
├─ src/editor/
│  ├─ core.ts                 plugin ProseMirror (testable en Node)
│  └─ controller.ts           temporisation, lots, IA, état de l'interface
├─ src/server/ai.ts           appel à l'API Claude, cache, limitation de débit
├─ src/ui/                    interface React
├─ scripts/build-dictionary.mjs
└─ tests/{unit,e2e}
```

## 7. Évolution vers un SaaS

Le moteur (`src/engine`) et le cœur éditeur (`src/editor/core.ts`) ne dépendent ni du DOM ni de Next.js : ils se réutilisent tels quels.

1. **Comptes et synchronisation** : authentification (Auth.js, Clerk…), base Postgres pour les documents, versions et dictionnaire personnel (aujourd'hui en `localStorage`). Chiffrement au repos, suppression à la demande (RGPD), hébergement UE.
2. **Quotas et facturation** : la route `/api/correct` est déjà le point de contrôle unique. On y ajoute un compteur de jetons par utilisateur (Stripe metered billing). Le mode local reste gratuit et illimité, l'IA fait partie de l'offre payante.
3. **Infrastructure** : remplacer le cache mémoire et la limitation de débit par Redis. Le lexique statique passe sur un CDN.
4. **Extension Chrome** (Gmail, formulaires, messageries) : moteur et lexique dans le service worker ; un content script observe `input`, `textarea` et `contenteditable`, et applique les corrections via `document.execCommand('insertText')` / `beforeinput` pour garder l'annulation native. Google Docs dessine son texte sur un canvas : il faut passer par son API d'add-on, c'est un chantier à part.
5. **Application de bureau** : Tauri ou Electron autour de la même application web. La correction « partout » (toutes applications) passe par les API d'accessibilité de l'OS, beaucoup plus complexes.
6. **Qualité** : corpus d'évaluation de phrases réelles annotées (taux de corrections fausses, à garder proche de 0 ; taux de fautes corrigées). Mesurer avant d'élargir les règles ou de changer de modèle. Lexique genre/nombre complet (Lexique 3, Grammalecte) pour étendre les accords en local.
7. **Performance** : déplacer le lexique dans un Web Worker si des documents très longs le justifient (l'analyse est déjà découpée par lots).

## Licences des données

Dictionnaire : Grammalecte / Dicollecte (MPL 2.0). Fréquences : FrequencyWords (CC-BY-SA 4.0). Voir `data/README.md`.
