#!/bin/sh
# Premier essai réel via OpenRouter : compare trois configurations de lecture sur la copie
# d'exemple, puis corrige la copie de bout en bout avec la configuration choisie.
# Coût attendu : environ 1 à 2 $ au total (affiché exactement à la fin de chaque étape).
# Prérequis : OPENROUTER_API_KEY, Lean compilé (lean_workspace/README.md).
set -e
[ -n "$OPENROUTER_API_KEY" ] || { echo "OPENROUTER_API_KEY absente"; exit 1; }
cd "$(dirname "$0")/.."

FORT=openrouter:anthropic/claude-opus-5.5
echo "== 1. Qualité maximale : 3 familles de modèles + arbitre"
mathocr evaluer --sortie runs/eval_qualite \
  --ocr $FORT --ocr openrouter:google/gemini-3.1-pro-preview --ocr openrouter:openai/gpt-5 \
  --raisonnement $FORT

echo "== 2. Cascade : 2 lecteurs bon marché, lignes disputées relues par Claude Opus"
mathocr evaluer --sortie runs/eval_cascade --mode-ocr cascade \
  --ocr openrouter:google/gemini-3.8-flash --ocr openrouter:openai/gpt-5-mini \
  --ocr-fort $FORT --raisonnement $FORT --audit 0.1

echo "== 3. Lecteur unique (référence basse)"
mathocr evaluer --sortie runs/eval_unique --ocr $FORT

echo "== 4. Correction complète de la copie (cascade)"
mathocr corriger --exercice examples/somme_impairs/exercice.json \
  --images examples/somme_impairs/copie_p1.webp --sortie runs/essai_reel \
  --mode-ocr cascade --ocr openrouter:google/gemini-3.8-flash --ocr openrouter:openai/gpt-5-mini \
  --ocr-fort $FORT --raisonnement $FORT --juge openrouter:google/gemini-3.1-pro-preview
