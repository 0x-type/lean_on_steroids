# Lean On Steroids

Correction de démonstrations mathématiques manuscrites, vérifiée par Lean 4.

On photographie l'énoncé, puis la copie. Lean On Steroids lit l'écriture, découpe le raisonnement en étapes,
traduit chaque étape en Lean 4 (Mathlib) et la fait vérifier par Lean. Le résultat est l'une de trois
issues, avec un retour de correcteur en français.

## Pourquoi ce projet

Les grands modèles de langage lisent très bien l'écriture manuscrite et comprennent un raisonnement,
mais ils **hallucinent** : ils valident une preuve fausse avec assurance, inventent une justification
absente de la copie, ou « corrigent » en silence l'erreur de l'élève en la lisant. Demander à un chatbot
« ma démonstration est-elle juste ? » donne une opinion, pas une vérification.

Lean 4 est l'inverse : un vérificateur de preuves **exact**. Une étape qu'il accepte est démontrée,
une étape qu'il réfute est fausse, sans avis ni approximation. Mais Lean ne sait pas lire une photo ni
comprendre une rédaction en français.

Lean On Steroids combine les deux, et chacun reste à sa place :

* les modèles d'IA **lisent, structurent et proposent** (une lecture, une traduction, une preuve) ;
* Lean **tranche** : rien n'est validé parce qu'une IA l'affirme, seulement parce que Lean l'a vérifié ;
* des contrôles de fidélité empêchent l'IA de modifier ce que l'élève a écrit.

Le résultat est une correction **stricte et précise** : une IA faible ou qui se trompe ne peut produire
qu'un « examen nécessaire » de plus, jamais un faux « raisonnement vérifié ».

## Les trois issues

| Issue | Condition |
|---|---|
| **raisonnement vérifié** | chaque affirmation de la copie est démontrée dans Lean à partir de ses seules dépendances ; les étapes s'assemblent en l'énoncé ; axiomes standard uniquement ; fidélité contrôlée ; aucune lecture douteuse bloquante |
| **erreur mathématique établie** | Lean démontre qu'une étape est fausse (contre-exemple), sous toutes les hypothèses de son cas, et la lecture comme la traduction de cette ligne sont contrôlées |
| **examen nécessaire** | tous les autres cas : un pas difficile non justifié, une lecture incertaine, une traduction contestée. Ce n'est pas une accusation |

Mode tolérant (par défaut) : une étape vraie à laquelle il ne manque qu'une justification élémentaire est
acceptée, avec une note « pour une rédaction parfaite ». Une étape fausse ou un pas difficile absent ne
le sont jamais.

## Interface web

```sh
pip install -e ".[dev]"
# Lean 4.34.1 et Mathlib : voir lean_workspace/README.md
export OPENROUTER_API_KEY=...
leanonsteroids web                    # http://127.0.0.1:8000/
leanonsteroids web --hote 0.0.0.0 --port 8080   # exposée au réseau, derrière un proxy HTTPS
```

1. **L'énoncé** : une capture d'écran ou une photo de n'importe quel exercice. Il est écrit en Lean,
   compilé par Lean (deux reprises en cas d'erreur), puis relu avec l'image par un juge d'un autre
   fournisseur. S'il ne passe pas ces deux contrôles, il n'est pas validé et la correction ne peut conclure
   ni « vérifié » ni « erreur établie ». On peut aussi choisir un exercice préparé à l'avance
   (`examples/collecte/exercices`), avec son kit de résultats du cours.
2. **La copie** : les photos des pages, dans l'ordre. L'avancement s'affiche étape par étape, puis le
   verdict, le retour à l'élève et le rapport complet.

Les fichiers reçus sont rangés dans `runs/web/`. Le serveur n'utilise que la bibliothèque standard ;
deux corrections au plus tournent en même temps, car Lean est gourmand.

## Ligne de commande

```sh
leanonsteroids corriger --exercice examples/collecte/exercices/mw06.json --images copie.jpg --sortie runs/x
leanonsteroids demo          # copie d'exemple et 4 variantes, sans clé d'API
leanonsteroids photo copie.jpg   # contrôle local de la photo, gratuit
pytest                # 142 tests, dont des cas de bout en bout avec Lean
```

Chaque étape peut être remplacée par un fichier (`--transcription`, `--structure`, `--formalisation`) :
c'est ainsi qu'un enseignant corrige une lecture. Tous les appels aux modèles sont mis en cache dans
`runs/.cache/` (rejeu à l'identique, trace d'audit).

## Profil de modèles par défaut (M)

Une seule clé, OpenRouter. Le profil M a été retenu après comparaison sur des copies réelles :

| Étape | Modèle | Pourquoi |
|---|---|---|
| Lecture (2 lecteurs à l'aveugle) | Gemini 3.8 Flash + Opus 5.5 | une erreur de lecture est la seule que Lean ne peut pas rattraper |
| Arbitrage des doutes | Opus 5.5 | regarde l'image agrandie |
| Structure du raisonnement | Opus 5.5 | les modèles bon marché testés ratent les cas et les témoins |
| Traduction en Lean | traducteur déterministe, puis Opus 5.5 | doit dire exactement ce que dit la copie |
| Niveau 2 (preuve ou contre-exemple) | GPT-6 Luna, puis Luna-Pro | Lean vérifie chaque preuve proposée |
| Assemblage (cas, absurde, témoins) | GPT-6 Luna-Pro | logique seule autorisée, vérifié par Lean |
| Juge (relecture de la traduction) | Gemini 3.1 Pro, effort moyen | autre fournisseur que le traducteur |

Environ 0,40 à 0,80 $ et 3 à 6 minutes par copie. Tout moteur donné sur la ligne de commande l'emporte ;
`--profil aucun` désactive le profil.

## Architecture

```
photo ──► contrôle photo (local)
      ──► 2 lecteurs à l'aveugle ──► consensus ──► arbitrage des doutes décisifs (ratures ignorées)
      ──► structure : étapes, cas, hypothèses, théorèmes cités
      ──► traduction en Lean (code d'abord, IA sinon)
      ──► Lean 4 + Mathlib : chaque étape avec ses seules dépendances
            ├─ niveau 2 : une IA propose une preuve ou un contre-exemple, Lean la revérifie
            └─ assemblage : une IA relie les étapes de l'élève, Lean le revérifie
      ──► fidélité : ancrage, empreintes numériques, lectures alternatives, juge en parallèle
            └─ traduction jugée infidèle : refaite une fois, cette étape seulement
      ──► verdict (3 issues) ──► retour à l'élève, resultat.json, rapport.html
```

## Garde-fous

* **Lecture aveugle.** Les lecteurs ne voient pas l'énoncé : un modèle qui sait ce qui devrait être écrit
  « lit » souvent ce qui devrait être écrit. L'arbitre déclare quand il s'appuie sur le sens.
* **L'IA formalise des énoncés, pas des preuves.** Les étapes sont prouvées par une automatisation fixe ;
  une preuve proposée par une IA ne compte que si Lean l'accepte, et seulement avec des résultats que la
  copie a le droit d'utiliser.
* **Théorèmes cités.** Un grand théorème (TVI, Rolle, Bézout...) ne compte que si la copie le nomme ; un
  catalogue de 12 théorèmes, chacun vérifié dans Lean, fait le lien avec Mathlib. Une faute d'une lettre
  dans un sigle (« TVA » pour TVI) est acceptée si Lean confirme l'usage du théorème.
* **Réfutation honnête.** Une étape n'est déclarée fausse que sous toutes les hypothèses du cas où elle se
  trouve : une étape vraie dans son cas n'est jamais accusée.
* **Empreinte numérique.** Les membres d'une égalité, évalués dans Lean et dans la copie (SymPy), doivent
  coïncider : une traduction qui corrige l'élève est détectée mécaniquement.
* **Lectures incertaines.** Chaque lecture alternative est testée : si elle change le sens d'une étape,
  elle bloque la conclusion. Une rature n'est ignorée que si elle ne fait qu'ajouter une marque.
* **Lean isolé.** Fragments interdits filtrés (sorry, axiom, native_decide...), axiomes contrôlés
  (`propext`, `Classical.choice`, `Quot.sound`), aucun réseau, limites de temps et de mémoire.

## Résultats sur les copies de test

| Copie | Vérité | Verdict |
|---|---|---|
| B06 | juste | raisonnement vérifié (Lean démontre l'énoncé à partir des étapes de l'élève) |
| B07 | erreur | erreur établie, ligne 14 : la convergence uniforme sur [0,1] est fausse |
| A10 | erreur | examen nécessaire : erreur trouvée à la bonne ligne, mais le chiffre est écrit sur du correcteur |
| A06, A01 | lacunaires | examen nécessaire, avec les justifications manquantes |
| B01, A05 | justes | examen nécessaire : pas difficiles qui demandent un kit d'exercice |

Aucune fausse accusation et aucun faux « vérifié » sur l'ensemble des essais.

## Fichiers

```
src/leanonsteroids/
  pipeline.py, cli.py     orchestration, ligne de commande, profils de modèles
  web/                    interface web (server.py, index.html)
  stages/transcribe.py    lecture multi-moteurs, consensus, arbitrage
  stages/agents.py        structure, formalisation, niveau 2, assemblage, juge
  stages/exercise_from_image.py   énoncé lu sur une capture, compilé et relu
  stages/fidelity.py      contrôles de fidélité ; stages/latex2lean.py traduction déterministe
  stages/verdict.py       règles des trois issues ; stages/feedback.py retour en français
  theoremes.py, data/     catalogue des théorèmes citables
  lean/                   politique, générateur, bac à sable, interprétation
  report/html.py          rapport HTML
lean_workspace/           projet Lake (Lean 4.34.1, Mathlib v4.34.1)
examples/                 exercices préparés, copie de démonstration
tests/                    142 tests
```

## Limites connues

* Un énoncé lu sur une capture n'a pas de kit : les pas qui demandent un résultat du cours non cité
  finissent plus souvent en « examen nécessaire » qu'avec un exercice préparé.
* L'empreinte numérique couvre ℕ, ℤ, ℚ ; pour ℝ, la fidélité repose sur la relecture du juge.
* Seule une partie de Mathlib est compilée ; un énoncé qui demande un autre module est refusé avec un
  message clair.

## Sources

* LeanTutor, vérification pas à pas de preuves d'élèves en Lean : https://arxiv.org/html/2506.08321v2
* ProofFlow, formalisation par graphe de dépendances : https://arxiv.org/abs/2510.15981
* Robustesse de l'autoformalisation (autoformaliseurs qui annulent les erreurs) : https://arxiv.org/abs/2606.14867
* Correction automatique de copies manuscrites universitaires : https://arxiv.org/pdf/2603.00895
