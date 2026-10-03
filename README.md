# MathOCR — correction de copies manuscrites vérifiée par Lean 4

Prototype : à partir de l'énoncé d'un exercice et des photos d'une copie manuscrite, on reconstruit
**fidèlement** le raisonnement de l'élève, on formalise **ses** étapes en Lean 4 / Mathlib, on les
vérifie dans un environnement isolé, puis on propose un retour en français.

Il y a trois issues possibles, et seulement trois :

| Issue | Condition (toutes requises) |
|---|---|
| **raisonnement vérifié** | énoncé de référence validé · chaque affirmation de la copie prouvée par l'automatisation *élémentaire* à partir de ses **seules** dépendances déclarées · assemblage et accord avec l'énoncé prouvés · axiomes standard uniquement · fidélité contrôlée · aucune lecture ambiguë bloquante |
| **erreur mathématique établie** | une étape est **réfutée** dans Lean (la négation est démontrée, contre-exemple à l'appui), **et** cette étape est fidèle à la copie (ancrage + empreinte numérique ou prédicat), **et** sa lecture n'est pas ambiguë |
| **examen nécessaire** | tous les autres cas, en particulier un échec de Lean (étape ni prouvée ni réfutée) : il ne prouve **pas** que l'élève a tort |

## Démarrage rapide (sans clé d'API)

```sh
pip install -e ".[dev]"
# Lean 4.34.1 + sous-ensemble de Mathlib : voir lean_workspace/README.md
mathocr demo          # la copie fournie + 4 variantes, résultats dans runs/demo/*/rapport.html
pytest                # 73 tests, dont les cas de bout en bout avec Lean
```

Résultat attendu de `mathocr demo` :

| Cas | Issue |
|---|---|
| `copie` — la photo fournie (`examples/somme_impairs/copie_p1.webp`) | raisonnement vérifié (+ remarques de rédaction) |
| `erreur_calcul` — « = n² + 2n » au lieu de « n² + (2n+1) » | erreur mathématique établie (S11, S12 réfutées, n := 0) |
| `formalisation_infidele` — même erreur, mais le formaliseur « répare » l'élève | examen nécessaire (empreinte numérique : copie 15 ≠ Lean 16) |
| `lecture_ambigue` — « (2n+1) » pourrait se lire « (2n-1) » | examen nécessaire (lecture alternative plausible qui change l'étape) |
| `saut_logique` — hérédité « évidente », sans calcul | examen nécessaire (étape vraie mais ni prouvée élémentairement ni réfutée) |

## Une seule clé : OpenRouter

```sh
export OPENROUTER_API_KEY=…
mathocr modeles claude        # modèles qui lisent les images, prix, prise en charge du schéma strict
mathocr modeles gemini

# Qualité maximale : plusieurs lecteurs de familles différentes + arbitre
mathocr corriger --exercice examples/somme_impairs/exercice.json --images copie.jpg --sortie runs/x \
  --ocr openrouter:<claude> --ocr openrouter:<gemini> --raisonnement openrouter:<claude>

# Économique : deux lecteurs bon marché, lignes disputées relues par un modèle fort
mathocr corriger … --mode-ocr cascade --ocr openrouter:<gemini-rapide> --ocr openrouter:<autre-famille> \
  --ocr-fort openrouter:<claude> --raisonnement openrouter:<claude>
```

Remplacer `<claude>`, `<gemini>`… par les identifiants affichés par `mathocr modeles`. Le coût
exact de chaque appel est renvoyé par OpenRouter et figure dans le rapport. Mathpix n'est pas
disponible via OpenRouter (clé séparée, optionnelle). Pour la lecture, choisir des modèles de
familles différentes : deux modèles proches risquent de faire les mêmes erreurs.

## Utilisation avec les modèles

```sh
export ANTHROPIC_API_KEY=…  GEMINI_API_KEY=…  OPENAI_API_KEY=…  MATHPIX_APP_ID=…  MATHPIX_APP_KEY=…
mathocr corriger --exercice examples/somme_impairs/exercice.json \
  --images copie_p1.jpg copie_p2.jpg --sortie runs/eleve42 \
  --ocr anthropic:claude-opus-5-5 --ocr gemini:gemini-3-pro --ocr mathpix \
  --raisonnement anthropic:claude-opus-5-5 --juge gemini:gemini-3-pro
```

Chaque étape peut être remplacée par un fichier (`--transcription`, `--structure`, `--formalisation`) :
c'est ainsi qu'un enseignant corrige une lecture, et que la démo tourne hors-ligne. Tous les appels
aux modèles sont mis en cache dans `runs/.cache/` (rejeu à l'identique, trace d'audit).
Les identifiants de modèles OpenAI/Gemini sont des valeurs par défaut à ajuster
(`MATHOCR_OPENAI_MODEL`, `MATHOCR_GEMINI_MODEL`, ou directement dans `--ocr`).

## Économiser sans perdre en qualité

| Mesure | Effet sur le coût | Pourquoi la qualité ne baisse pas |
|---|---|---|
| **Traduction LaTeX → Lean par le code** (`stages/latex2lean.py`) | supprime l'appel le plus cher pour les étapes de calcul | le code ne « répare » jamais l'élève ; l'empreinte numérique (SymPy, analyseur indépendant) contrôle toujours ; les cas délicats (soustraction dans ℕ…) vont à l'agent |
| **Mémoire partagée par exercice** (`memory.py`) | une étape traduite par l'agent n'est payée qu'une fois, pour toute la classe | on ne mémorise que ce qui a passé les contrôles de fidélité, et tout est revérifié par Lean |
| **Contrôle local de la photo** (`mathocr photo`) | aucun appel pour une photo floue, sombre, vide | ces photos finiraient en « examen nécessaire » |
| **Cache des appels** | renvoyer la même photo ne coûte rien | réponse identique |
| **Compteur de coûts** | jetons et coût réels par copie, dans le rapport | — (c'est l'instrument de mesure) |
| **Lecture en cascade** (`--mode-ocr cascade`) | 2 lecteurs bon marché ; seules les lignes disputées sont relues, zoomées, par un moteur fort | **à valider** avec `mathocr evaluer` : n'adopter que si les erreurs silencieuses n'augmentent pas ; `--audit 0.1` relit 10 % des lignes « d'accord » en continu |

Mesurer une configuration de lecture sur un jeu de copies (photo + transcription vérifiée à la main) :

```sh
mathocr evaluer --ocr gemini:<modèle-rapide> --ocr mathpix --mode-ocr cascade \
  --ocr-fort anthropic:claude-opus-5-5 --raisonnement anthropic:claude-opus-5-5
```

La mesure décisive est le nombre d'**erreurs silencieuses** : symboles mal lus qu'aucun moteur n'a
signalés. Une erreur signalée est rattrapée en aval (analyse de sensibilité, puis enseignant) ;
une erreur silencieuse peut fausser le verdict.

## Architecture

```
photos ─► 1. transcription   OCR multi-moteurs en lecture AVEUGLE (sans l'énoncé) → consensus
        │                    (alignement des lignes, diff par jetons LaTeX) → arbitrage sur zooms
        │                    par un modèle de raisonnement qui déclare s'il a utilisé le contexte
        ├─► 2. structure     graphe d'étapes ; chaque étape cite un extrait EXACT d'une ligne ;
        │                    implicites limitées à : chaîne d'égalités, dépliage, schéma de récurrence
        ├─► 3. formalisation traduction LaTeX → Lean PAR LE CODE (sans LLM) ; l'agent ne reçoit que
        │                    les étapes que le code refuse, puis la mémoire partagée les réutilise
        ├─► 4. Lean          fichier généré par un gabarit de confiance : 1 étape = 1 théorème dont
        │                    les hypothèses sont ses dépendances ; preuve = `mathocr_core` (norm_num,
        │                    ring, omega, linarith, simp, dernier terme d'une somme) ; sondes de
        │                    réfutation (contre-exemples) ; assemblage ; accord avec l'énoncé ;
        │                    `#print axioms` ; niveau 2 optionnel : preuve/réfutation par un agent
        ├─► 5. fidélité      ancrage · structure · couverture · empreintes numériques LaTeX↔Lean ·
        │                    prédicats · sensibilité aux lectures ambiguës · rétro-traduction (agent)
        └─► 6. verdict (3 issues) → retour en français → resultat.json + rapport.html
```

### Pourquoi ces choix

* **Lecture aveugle puis arbitrage déclaré.** Un LLM de vision qui connaît l'énoncé « lit » ce qui
  devrait être écrit : il corrige silencieusement l'élève. Les moteurs lisent donc sans l'énoncé ;
  l'arbitre, lui, doit signaler (`context_dependent`) toute décision fondée sur le sens.
* **L'agent formalise des énoncés, pas des preuves.** Les preuves des étapes viennent d'une
  automatisation fixe et modeste : une étape qui demande un vrai argument absent de la copie n'est
  pas « vérifiée », elle est signalée (saut logique). La littérature récente montre que les
  autoformaliseurs « réparent » massivement les erreurs (voir Sources) : on ne leur fait pas confiance.
* **Empreinte numérique.** Pour chaque (in)égalité, les membres Lean (évalués par `#eval`) et les
  membres LaTeX de la copie (évalués par SymPy) doivent coïncider en plusieurs points : une
  formalisation qui corrige l'élève est détectée mécaniquement (cas `formalisation_infidele`).
* **Sensibilité aux lectures.** Chaque incertitude est testée : la lecture alternative est-elle mal
  formée (symbole non défini : « ℓ », « Y(n) »), équivalente (renommage d'indice k/h, « nulle »/« vide »),
  ou change-t-elle la valeur d'une étape ? Seul le dernier cas bloque (si la lecture est plausible).
* **Garde-fous Lean.** Fragments filtrés (sorry, admit, axiom, native_decide, set_option, `#`,
  commentaires, méta-programmation, parenthèses orphelines…), axiomes contrôlés
  (`propext`, `Classical.choice`, `Quot.sound` seulement), `autoImplicit false`, heartbeats bornés.
* **Isolation.** `unshare -n` (aucun réseau) + limites `setrlimit` (CPU, fichiers, processus) +
  délai d'horloge + environnement vidé (aucune clé transmise à Lean) ; ou image Docker
  (`docker/Dockerfile`, `--network none --read-only`, mémoire/CPU/PIDs bornés).

## OCR de mathématiques manuscrites : état de l'art retenu

* Les LLM de vision de pointe (Claude, GPT, Gemini) dominent désormais la reconnaissance
  d'écriture manuscrite, et surpassent Mathpix sur des copies réelles d'étudiants grâce au contexte ;
  Mathpix reste un bon contre-témoin *littéral* (il n'interprète pas) et fournit des contours et une
  confiance par ligne. D'où l'ensemble hétérogène + consensus plutôt qu'un moteur unique.
* Le contexte aide à lire une écriture difficile mais pousse à corriger l'élève : il est
  réservé à l'arbitrage, et tracé.

## Fichiers

```
src/mathocr/
  schemas.py            modèles de données (traçabilité ligne ↔ étape ↔ théorème)
  pipeline.py, cli.py   orchestration, ligne de commande
  prompts.py            consignes des agents
  llm/                  moteurs : anthropic, openai, gemini, mathpix + cache/rejeu
  stages/transcribe.py  lecture multi-moteurs, consensus, arbitrage
  stages/agents.py      structure, formalisation, niveau 2, rétro-traduction, reformulation
  stages/fidelity.py    contrôles de fidélité ; stages/latexeval.py évaluation SymPy
  stages/latex2lean.py  traduction déterministe LaTeX → Lean
  stages/photo_quality.py  contrôle local des photos ; stages/evaluate.py  mesure de la lecture
  memory.py             mémoire partagée par exercice ; llm/pricing.py  compteur de coûts
  stages/verdict.py     règles des trois issues ; stages/feedback.py retour en français
  lean/                 politique, générateur, bac à sable, interprétation
  report/html.py        rapport HTML (photo annotée cliquable, KaTeX)
lean_workspace/         projet Lake (Lean 4.34.1, Mathlib v4.34.1) + tactiques de confiance
examples/somme_impairs/ énoncé, photo, fixtures de référence, variantes (et leur générateur)
tests/                  73 tests (politique, LaTeX, consensus, moteurs simulés, bout en bout Lean)
```

## Limites connues (prototype)

* La transcription de la démo est une fixture établie à la main pendant le développement ; les
  moteurs réels n'ont pas encore été évalués sur la photo (il faut des clés d'API). Le jeu
  d'évaluation (`examples/jeu_evaluation.json`) ne contient qu'une copie : l'enrichir avant de
  choisir une configuration de lecture.
* Schémas de preuve pris en charge par l'assemblage : direct, récurrence simple. À étendre :
  récurrence forte, disjonction de cas, absurde, contraposée.
* L'empreinte numérique couvre ℕ, ℤ, ℚ ; pour ℝ, la fidélité repose sur la rétro-traduction.
* Le sous-ensemble de Mathlib compilé (`Prelude.lean`) suffit pour l'arithmétique et les sommes ;
  compiler Mathlib complet (ou télécharger son cache) pour l'analyse.

## Sources

* LeanTutor — vérification pas à pas de preuves d'élèves en Lean : https://arxiv.org/html/2506.08321v2
* ProofFlow — formalisation par graphe de dépendances et lemmes par étape, fidélité structurelle : https://arxiv.org/abs/2510.15981
* Robustesse de l'autoformalisation (« biased autoformalizers » qui annulent les erreurs) : https://arxiv.org/abs/2606.14867
* Correction automatique de copies manuscrites universitaires (LLM vs Mathpix) : https://arxiv.org/pdf/2603.00895
* Comparatif OCR manuscrit 2026 (GPT, Claude, Gemini) : https://www.codesota.com/ocr/best-for-handwriting
* Mathpix API v3/text (line data, confiance) : https://docs.mathpix.com/reference/post-v3-text
