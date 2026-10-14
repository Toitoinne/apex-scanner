// Données linguistiques soigneusement limitées aux cas sans ambiguïté.
// Règle d'or : une entrée n'a sa place ici que si la forme fautive n'est
// JAMAIS correcte en français standard.

/** Fautes d'orthographe fréquentes → forme correcte (mot isolé, minuscules). */
export const COMMON_ERRORS: Record<string, string> = {
  parmis: 'parmi',
  malgrés: 'malgré',
  malgrè: 'malgré',
  apeller: 'appeler',
  appeller: 'appeler',
  rapeller: 'rappeler',
  rappeller: 'rappeler',
  language: 'langage',
  dévellopement: 'développement',
  dévelopement: 'développement',
  developpement: 'développement',
  voila: 'voilà',
  ca: 'ça',
  biensur: 'bien sûr',
  'bien-sûr': 'bien sûr',
  jai: "j'ai",
  cest: "c'est",
  aujourdhui: "aujourd'hui",
  "aujourd'hui": "aujourd'hui",
  quelqun: "quelqu'un",
  quelquun: "quelqu'un",
  dailleurs: "d'ailleurs",
  daccord: "d'accord",
  "d'accords": "d'accord",
  jusqua: "jusqu'à",
  "jusqu'a": "jusqu'à",
  quil: "qu'il",
  quils: "qu'ils",
  "c'est-a-dire": "c'est-à-dire",
  "c'est-à-dir": "c'est-à-dire",
  connection: 'connexion',
  example: 'exemple',
  addresse: 'adresse',
  bizzare: 'bizarre',
  dilemne: 'dilemme',
  excercice: 'exercice',
  excercices: 'exercices',
  professionel: 'professionnel',
  professionelle: 'professionnelle',
  professionels: 'professionnels',
  traditionel: 'traditionnel',
  personel: 'personnel',
  personelle: 'personnelle',
  acceuil: 'accueil',
  acceuillir: 'accueillir',
  recu: 'reçu',
  recue: 'reçue',
  recus: 'reçus',
  francais: 'français',
  francaise: 'française',
  garcon: 'garçon',
  lecon: 'leçon',
  facon: 'façon',
  deja: 'déjà',
  tres: 'très',
  apres: 'après',
  pres: 'près',
  probleme: 'problème',
  problemes: 'problèmes',
  interessant: 'intéressant',
  interessante: 'intéressante',
  peutetre: 'peut-être',
  'peut-etre': 'peut-être',
  aparemment: 'apparemment',
  apparement: 'apparemment',
  évidement: 'évidemment',
  évidament: 'évidemment',
  evidemment: 'évidemment',
  notament: 'notamment',
  différament: 'différemment',
  courament: 'couramment',
  vraiement: 'vraiment',
  récurent: 'récurrent',
  occurence: 'occurrence',
  occurences: 'occurrences',
  hormi: 'hormis',
}

/** Expressions de plusieurs mots (comparaison insensible à la casse). */
export const COMMON_PHRASES: Array<[RegExp, string]> = [
  [/^sa va$/i, 'ça va'],
  [/^ca va$/i, 'ça va'],
  [/^en faite$/i, 'en fait'],
  [/^au faite$/i, 'au fait'],
  [/^c'est a dire$/i, "c'est-à-dire"],
  [/^c’est a dire$/i, 'c’est-à-dire'],
  [/^quand meme$/i, 'quand même'],
  [/^tout a fait$/i, 'tout à fait'],
  [/^tous à fait$/i, 'tout à fait'],
  [/^a priori$/i, 'a priori'],
  [/^peut etre$/i, 'peut-être'],
  [/^par contre$/i, 'par contre'],
]

/** Pronoms sujets du singulier (pour la confusion nom/verbe : « je vous envoi »). */
export const SUBJECT_1S = new Set(['je', "j'", 'j’'])
export const SUBJECT_2S = new Set(['tu'])
export const SUBJECT_3S = new Set(['il', 'elle', 'on'])

/** Pronoms compléments qui peuvent s'intercaler entre sujet et verbe. */
export const OBJECT_PRONOUNS = new Set([
  'me', 'te', 'se', 'le', 'la', 'les', 'lui', 'leur', 'nous', 'vous', 'y', 'en', 'ne',
  "m'", "t'", "s'", "l'", "n'", 'm’', 't’', 's’', 'l’', 'n’',
])

/** Formes conjuguées de l'auxiliaire avoir. */
export const AVOIR_FORMS = new Set([
  'ai', 'as', 'a', 'avons', 'avez', 'ont',
  'avais', 'avait', 'avions', 'aviez', 'avaient',
  'aurai', 'auras', 'aura', 'aurons', 'aurez', 'auront',
  'aurais', 'aurait', 'aurions', 'auriez', 'auraient',
  'aie', 'aies', 'ait', 'ayons', 'ayez', 'aient',
  'eus', 'eut', 'eûmes', 'eûtes', 'eurent', 'avoir',
])

/** Adverbes courts qui peuvent séparer l'auxiliaire du participe. */
export const LINK_ADVERBS = new Set([
  'pas', 'bien', 'déjà', 'jamais', 'toujours', 'beaucoup', 'trop', 'rien', 'plus',
  'souvent', 'enfin', 'aussi', 'encore', 'vraiment', 'mal', 'peu', 'presque', 'tout',
  'également', 'finalement', 'récemment', 'enfin', 'longtemps',
])

/** Verbes conjugués suivis d'un infinitif (« je vais manger », « il faut envoyer »). */
export const INFINITIVE_GOVERNORS = new Set([
  'vais', 'vas', 'va', 'allons', 'allez', 'vont', 'allait', 'allais', 'allaient',
  'peux', 'peut', 'pouvons', 'pouvez', 'peuvent', 'pourrais', 'pourrait', 'pourriez', 'pourrions', 'pourraient',
  'veux', 'veut', 'voulons', 'voulez', 'veulent', 'voudrais', 'voudrait', 'voudriez', 'voudrions',
  'dois', 'doit', 'devons', 'devez', 'doivent', 'devrais', 'devrait', 'devriez', 'devrions', 'devraient',
  'faut', 'faudrait', 'fallait', 'souhaite', 'souhaitez', 'souhaitons', 'aimerais', 'aimerait', 'aimerions',
  'pour', 'sans',
])

/** Déterminants qui imposent le pluriel au nom qui suit. */
export const PLURAL_DETERMINERS = new Set([
  'les', 'des', 'ces', 'mes', 'tes', 'ses', 'nos', 'vos', 'leurs', 'plusieurs', 'quelques',
  'deux', 'trois', 'quatre', 'cinq', 'six', 'sept', 'huit', 'neuf', 'dix', 'douze', 'quinze', 'vingt', 'cent',
  'nombreux', 'nombreuses', 'certains', 'certaines', 'différents', 'différentes', 'divers', 'diverses',
])

/** Mots qui ne prennent jamais la marque du pluriel après un déterminant pluriel. */
export const NO_PLURAL_AFTER_DET = new Set([
  'bien', 'mal', 'peu', 'tout', 'plus', 'moins', 'très', 'trop', 'assez', 'presque', 'tant', 'aussi',
  'non', 'oui', 'autre', 'même', 'seul', 'premier', 'dernier', 'uns', 'unes', 'une', 'un', 'leur',
  'quelque', 'ci', 'là', 'ça', 'cela', 'ceci', 'tel', 'quel', 'nul', 'demi', 'mi', 'semi', 'ex',
  'grand', 'petit', 'gros', 'vieux', 'jeune', 'bon', 'beau', 'nouveau', 'mauvais', 'meilleur', // adjectifs antéposés : laissés à l'IA
  'cent', 'mille', 'vingt', 'pense', 'marque', 'ensemble', 'record', 'type', 'maximum', 'minimum', 'standard', 'clé',
])

/** Mots qui empêchent la règle déterminant + nom quand ils précèdent le déterminant « les » (pronom). */
export const PRONOUN_BEFORE_LES = new Set([
  'je', "j'", 'tu', 'il', 'elle', 'on', 'nous', 'vous', 'ils', 'elles', 'ne', "n'", 'me', 'te', 'se',
])

/**
 * Genre des noms fréquents à l'écrit (pour l'accord du participe passé avec
 * un COD placé avant : « les lettres que j'ai envoyées »). Un nom absent de
 * cette liste ne déclenche aucune correction (situation B).
 */
const MASC = `document dossier fichier message mail courriel e-mail rapport contrat devis courrier
 compte-rendu projet produit service colis paquet livre article texte formulaire justificatif
 papier billet chèque paiement virement remboursement achat bon cadeau ticket email lien code
 accès identifiant mot résultat chiffre tableau graphique plan budget objectif retour avis conseil
 commentaire élément point sujet thème dessin film disque cours exercice devoir examen diplôme
 certificat permis contrôle test essai échantillon modèle exemplaire outil logiciel ordinateur
 téléphone appareil meuble vêtement bijou gâteau repas plat légume fruit vin jouet enfant élève
 client fournisseur partenaire collègue ami frère voisin cousin`
const FEM = `lettre facture pièce photo image information donnée note fiche proposition demande
 réponse question remarque modification correction version copie annexe offre commande livraison
 réservation inscription attestation déclaration candidature réunion présentation conférence
 formation mission tâche idée solution erreur faute règle consigne instruction recommandation
 page liste carte clé adresse somme valeur quantité date heure étape méthode procédure
 maison voiture chambre table chaise robe chemise chaussure fleur plante tarte pomme boisson
 bouteille boîte valise affaire chose histoire chanson vidéo musique application ressource
 personne amie sœur voisine cousine cliente collègue élève fille femme mère`

export const GENDER = new Map<string, 'm' | 'f'>()
for (const w of MASC.split(/\s+/).filter(Boolean)) GENDER.set(w, 'm')
for (const w of FEM.split(/\s+/).filter(Boolean)) GENDER.set(w, 'f')
// Épicènes : genre inconnu sans contexte → retirés.
for (const w of ['collègue', 'élève', 'partenaire']) GENDER.delete(w)

/** Verbes dont le participe reste invariable avec un complément de mesure ou intransitifs. */
export const NO_AGREEMENT_VERBS = new Set([
  'marcher', 'durer', 'coûter', 'peser', 'travailler', 'voyager', 'régner', 'nager', 'pleurer',
  'rêver', 'sembler', 'exister', 'rester', 'parler', 'téléphoner', 'succéder', 'plaire', 'mesurer',
])

/** Abréviations après lesquelles un point ne termine pas la phrase. */
export const ABBREVIATIONS = new Set([
  'etc', 'ex', 'cf', 'p', 'pp', 'm', 'mm', 'mme', 'mmes', 'mlle', 'dr', 'pr', 'me', 'st', 'ste',
  'env', 'av', 'apr', 'j.-c', 'n°', 'no', 'vol', 'chap', 'fig', 'éd', 'al', 'tél', 'tel', 'réf', 'ref',
  'art', 'bd', 'sq', 'min', 'max', 'approx', 'janv', 'févr', 'avr', 'juil', 'sept', 'oct', 'nov', 'déc',
])

/** Mots qui commencent par une voyelle mais n'admettent pas l'élision. */
export const NO_ELISION = new Set(['onze', 'onzième', 'oui', 'ouate', 'yacht', 'yaourt', 'yoga', 'uhlan', 'ululement'])

/** Mots qui peuvent être légitimement répétés (« nous nous », « vous vous »). */
export const DUPLICATE_CHECK = new Set([
  'le', 'la', 'les', 'un', 'une', 'des', 'de', 'du', 'à', 'au', 'aux', 'et', 'en', 'pour', 'par',
  'dans', 'sur', 'avec', 'ce', 'cette', 'ces', 'mon', 'ton', 'son', 'ma', 'ta', 'sa', 'mes', 'tes',
  'ses', 'notre', 'votre', 'leur', 'je', 'tu', 'il', 'ils', 'elles', 'on', 'est', 'sont', 'qui', 'mais', 'ou', 'donc',
])

/** Particules acceptées dans les mots composés (« a-t-il », « dis-moi »). */
export const HYPHEN_PARTICLES = new Set([
  't', 'il', 'elle', 'on', 'ils', 'elles', 'je', 'tu', 'nous', 'vous', 'moi', 'toi', 'le', 'la',
  'les', 'lui', 'leur', 'y', 'en', 'ce', 'là', 'ci',
])
