"""Exécution isolée de Lean 4.

Trois moteurs d'isolation, du plus fort au plus simple :

* ``docker``  : conteneur jetable, ``--network none``, système de fichiers en
  lecture seule, mémoire/CPU/PIDs bornés (image : ``docker/Dockerfile``) ;
* ``unshare`` : espace de noms réseau vide (pas d'accès réseau) + limites
  ``setrlimit`` (CPU, mémoire, fichiers, processus) + délai d'horloge ;
* ``local``   : limites ``setrlimit`` et délai seulement (repli).

Dans tous les cas : environnement vidé (aucune clé d'API transmise à Lean),
répertoire de travail temporaire, sortie JSON de Lean analysée ligne à ligne.
"""

from __future__ import annotations

import json
import os
import resource
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from ..schemas import LeanMessage, LeanRun


@dataclass
class SandboxConfig:
    workspace: Path  # projet lake contenant Mathlib compilé
    backend: str = "auto"  # auto | docker | unshare | local
    wall_timeout_s: float = 300.0
    cpu_limit_s: int = 600
    memory_mb: int = 8192  # limite interne de Lean (-M)
    address_space_mb: int = 0  # RLIMIT_AS ; 0 = pas de limite (Lean projette les .olean en mémoire)
    threads: int = 2
    docker_image: str = "leanonsteroids-lean:latest"
    extra_env: dict[str, str] = field(default_factory=dict)


@lru_cache(maxsize=4)
def _lake_env(workspace: str) -> tuple[str, str, str]:
    """(chemin de lean, LEAN_PATH, version) obtenus via `lake env`."""
    lake = shutil.which("lake") or str(Path.home() / ".elan/bin/lake")
    out = subprocess.run(
        [lake, "env", "sh", "-c", 'command -v lean; printf "%s\\n" "$LEAN_PATH"; lean --version'],
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    ).stdout.strip().splitlines()
    return out[0], out[1], out[2] if len(out) > 2 else ""


@lru_cache(maxsize=1)
def _unshare_ok() -> bool:
    exe = shutil.which("unshare")
    if not exe:
        return False
    try:
        return subprocess.run([exe, "-n", "true"], capture_output=True, timeout=10).returncode == 0
    except Exception:
        return False


@lru_cache(maxsize=1)
def _docker_ok() -> bool:
    exe = shutil.which("docker")
    if not exe:
        return False
    try:
        return subprocess.run([exe, "info"], capture_output=True, timeout=10).returncode == 0
    except Exception:
        return False


def _limits(cfg: SandboxConfig):
    def apply():
        os.setsid()
        resource.setrlimit(resource.RLIMIT_CPU, (cfg.cpu_limit_s, cfg.cpu_limit_s + 5))
        resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 2**20, 64 * 2**20))
        resource.setrlimit(resource.RLIMIT_NOFILE, (4096, 4096))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        if cfg.address_space_mb:
            lim = cfg.address_space_mb * 2**20
            resource.setrlimit(resource.RLIMIT_AS, (lim, lim))

    return apply


def parse_json_messages(stdout: str) -> list[LeanMessage]:
    msgs = []
    for raw in stdout.splitlines():
        raw = raw.strip()
        if not raw.startswith("{"):
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        pos = obj.get("pos") or {}
        sev = obj.get("severity", "information")
        if sev not in ("error", "warning", "information"):
            sev = "information"
        msgs.append(
            LeanMessage(
                severity=sev,
                line=int(pos.get("line", 0)),
                col=int(pos.get("column", 0)),
                text=obj.get("data", ""),
            )
        )
    return msgs


def resolve_backend(cfg: SandboxConfig) -> str:
    if cfg.backend != "auto":
        return cfg.backend
    if _unshare_ok():
        return "unshare"
    if _docker_ok():
        return "docker"
    return "local"


def run_lean(source: str, cfg: SandboxConfig, filename: str = "Copie.lean") -> LeanRun:
    backend = resolve_backend(cfg)
    with tempfile.TemporaryDirectory(prefix="leanonsteroids-lean-") as tmp:
        tmpdir = Path(tmp)
        src = tmpdir / filename
        src.write_text(source, encoding="utf-8")

        if backend == "docker":
            cmd = [
                "docker", "run", "--rm", "--network", "none", "--read-only",
                "--memory", f"{cfg.memory_mb + 1024}m", "--cpus", str(cfg.threads),
                "--pids-limit", "256", "--security-opt", "no-new-privileges",
                "--tmpfs", "/tmp:size=256m",
                "-v", f"{tmpdir}:/job:ro",
                cfg.docker_image,
                "lake", "env", "lean", "--json", f"-M{cfg.memory_mb}", f"-j{cfg.threads}", f"/job/{filename}",
            ]
            env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
            preexec = None
            version = "(docker)"
        else:
            lean, lean_path, version = _lake_env(str(cfg.workspace.resolve()))
            cmd = [lean, "--json", f"-M{cfg.memory_mb}", f"-j{cfg.threads}", str(src)]
            if backend == "unshare":
                cmd = [shutil.which("unshare") or "unshare", "-n", *cmd]
            # Environnement minimal : aucune variable héritée (pas de clés d'API).
            env = {
                "PATH": "/usr/bin:/bin",
                "LEAN_PATH": lean_path,
                "HOME": str(tmpdir),
                "LANG": "C.UTF-8",
                **cfg.extra_env,
            }
            preexec = _limits(cfg)

        t0 = time.monotonic()
        proc = subprocess.Popen(
            cmd,
            cwd=tmpdir,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            preexec_fn=preexec,
        )
        timed_out = False
        try:
            stdout, stderr = proc.communicate(timeout=cfg.wall_timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except Exception:
                proc.kill()
            stdout, stderr = proc.communicate()
        elapsed = time.monotonic() - t0

    msgs = parse_json_messages(stdout)
    ok = (not timed_out) and proc.returncode == 0 and not any(m.severity == "error" for m in msgs)
    return LeanRun(
        ok=ok,
        lean_version=version,
        returncode=proc.returncode,
        timed_out=timed_out,
        seconds=round(elapsed, 2),
        sandbox=backend,
        messages=msgs,
        stderr_tail=(stderr or "")[-2000:],
    )
