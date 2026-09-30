# Image d'isolation pour la vérification Lean (moteur --sandbox docker).
# Construction (à la racine du dépôt) :  docker build -f docker/Dockerfile -t mathocr-lean .
# Exécution par MathOCR : --network none, --read-only, mémoire/CPU/PIDs bornés, fichier monté en lecture seule.
FROM ubuntu:24.04
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates git zstd \
    && rm -rf /var/lib/apt/lists/*
RUN useradd -m lean
USER lean
WORKDIR /home/lean
RUN curl -sSfL https://raw.githubusercontent.com/leanprover/elan/master/elan-init.sh | sh -s -- -y --default-toolchain none
ENV PATH=/home/lean/.elan/bin:$PATH
COPY --chown=lean lean_workspace/lean-toolchain lean_workspace/lakefile.toml lean_workspace/lake-manifest.json /home/lean/ws/
COPY --chown=lean lean_workspace/MathOCRCheck /home/lean/ws/MathOCRCheck
WORKDIR /home/lean/ws
# Avec accès au cache Mathlib : `lake exe cache get` accélère fortement la construction.
RUN lake exe cache get || true
RUN lake build MathOCRCheck.Prelude MathOCRCheck.Auto
