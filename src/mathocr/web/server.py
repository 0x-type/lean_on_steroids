"""Interface web : l'utilisateur envoie la capture de l'énoncé, puis les photos de sa copie.

Serveur de la bibliothèque standard (aucune dépendance) ; chaque formalisation ou correction tourne dans un fil,
la page suit son avancement par /api/job/<id>. Lancement : `mathocr web` (écoute sur 127.0.0.1 par défaut).
"""

from __future__ import annotations

import io
import json
import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from email.parser import BytesParser
from email.policy import default as email_policy
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import Image

log = logging.getLogger("mathocr.web")

ROOT = Path(__file__).resolve().parents[3]
PAGE = Path(__file__).with_name("index.html")
PREPARED = ROOT / "examples" / "collecte" / "exercices"
MAX_UPLOAD = 40 * 1024 * 1024  # octets par requête
MAX_FILES = 8
IMAGE_FORMATS = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp", "MPO": ".jpg"}
_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@dataclass
class Job:
    id: str
    kind: str  # enonce | correction
    status: str = "en_cours"  # en_cours | termine | erreur
    stage: str = "en attente"
    started: float = field(default_factory=time.time)
    finished: float | None = None
    result: dict | None = None
    error: str | None = None

    def public(self) -> dict:
        end = self.finished or time.time()
        return {"id": self.id, "kind": self.kind, "status": self.status, "stage": self.stage,
                "seconds": round(end - self.started), "result": self.result, "error": self.error}


class App:
    def __init__(self, data_dir: Path, cfg_factory, max_parallel: int = 2):
        self.data = data_dir
        self.cfg_factory = cfg_factory  # () -> PipelineConfig
        self.jobs: dict[str, Job] = {}
        self.lock = threading.Lock()
        self.slots = threading.Semaphore(max_parallel)  # Lean est lourd : peu de corrections à la fois
        (self.data / "exercices").mkdir(parents=True, exist_ok=True)
        (self.data / "copies").mkdir(parents=True, exist_ok=True)

    # -- exercices --------------------------------------------------------------------------------------
    def exercises(self) -> list[dict]:
        out = []
        for src, folder in (("prepare", PREPARED), ("auto", self.data / "exercices")):
            for p in sorted(folder.glob("*.json")) if folder.exists() else []:
                if p.name.endswith("_reference.json"):
                    continue
                try:
                    d = json.loads(p.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if "lean_statement" not in d:
                    continue
                titre = d.get("titre") or re.sub(r"\s+", " ", re.sub(r"\$[^$]*\$", "…", d.get("statement_latex", "")))[:80]
                out.append({"id": p.stem, "titre": titre, "source": src, "valide": bool(d.get("validated_by"))})
        return out

    def exercise_path(self, ex_id: str) -> Path | None:
        if not _ID.match(ex_id or ""):
            return None
        for folder in (PREPARED, self.data / "exercices"):
            p = folder / f"{ex_id}.json"
            if p.exists():
                return p
        return None

    # -- tâches -----------------------------------------------------------------------------------------
    def start(self, kind: str, fn) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], kind=kind)
        with self.lock:
            self.jobs[job.id] = job

        def work():
            from ..llm.pricing import stage_listener
            stage_listener.set(lambda s: setattr(job, "stage", s))
            with self.slots:
                try:
                    job.result = fn(job)
                    job.status = "termine"
                except Exception as ex:  # noqa: BLE001 — l'erreur est montrée à l'utilisateur
                    log.exception("tâche %s", job.id)
                    job.error = _message(ex)
                    job.status = "erreur"
                finally:
                    job.finished = time.time()
        threading.Thread(target=work, daemon=True).start()
        return job

    def prepare(self, images: list[Path]) -> Job:
        def fn(job: Job) -> dict:
            from ..stages.exercise_from_image import prepare_exercise
            job.stage = "énoncé"
            ref = prepare_exercise(images, self.cfg_factory(), out_dir=self.data / "exercices")
            d = json.loads((self.data / "exercices" / f"{ref.exercise_id}.json").read_text(encoding="utf-8"))
            return {"id": ref.exercise_id, "titre": d.get("titre", ""), "statement_latex": ref.statement_latex,
                    "lean_statement": ref.lean_statement, "valide": bool(ref.validated_by),
                    "controle": ref.notes_latex}
        return self.start("enonce", fn)

    def correct(self, exercise: Path, images: list[Path], job_dir: Path) -> Job:
        def fn(job: Job) -> dict:
            from ..pipeline import run
            from ..stages.photo_quality import PhotoRejected
            try:
                r = run(exercise, images, job_dir, self.cfg_factory())
            except PhotoRejected as ex:
                raise UserError(str(ex)) from ex
            fb, vr = r.feedback, r.verdict
            return {"verdict": vr.verdict.value, "raisons": vr.reasons, "points_bloquants": vr.blocking_issues[:12],
                    "resume": fb.summary, "points_forts": fb.points_forts, "a_corriger": fb.points_a_corriger,
                    "redaction_parfaite": fb.pour_une_redaction_parfaite, "conseils": fb.conseils_redaction,
                    "cout": r.costs.total_usd if r.costs else None,
                    "rapport": f"/fichiers/{job_dir.name}/rapport.html",
                    "enonce_valide": bool(r.exercise.validated_by)}
        return self.start("correction", fn)


class UserError(Exception):
    pass


def _message(ex: Exception) -> str:
    if isinstance(ex, UserError):
        return str(ex)
    from ..llm.base import EngineError
    if isinstance(ex, EngineError):
        return f"Un modèle d'IA n'a pas répondu ({ex}). Réessaie dans un moment."
    return f"Erreur interne : {type(ex).__name__}. Le détail est dans le journal du serveur."


def save_images(files: list[tuple[str, bytes]], dest: Path) -> list[Path]:
    """Enregistre les images reçues sous des noms sûrs ; refuse ce qui n'est pas une image lisible."""
    if not files:
        raise UserError("Aucune image reçue.")
    if len(files) > MAX_FILES:
        raise UserError(f"{MAX_FILES} images au plus.")
    dest.mkdir(parents=True, exist_ok=True)
    out = []
    for i, (_, data) in enumerate(files, start=1):
        try:
            with Image.open(io.BytesIO(data)) as im:
                fmt = im.format
                im.verify()
        except Exception as ex:  # noqa: BLE001
            raise UserError(f"Le fichier n°{i} n'est pas une image lisible (JPEG, PNG ou WebP).") from ex
        if fmt not in IMAGE_FORMATS:
            raise UserError(f"Format {fmt} non pris en charge : envoie du JPEG, PNG ou WebP.")
        p = dest / f"page{i}{IMAGE_FORMATS[fmt]}"
        p.write_bytes(data)
        out.append(p)
    return out


def parse_multipart(content_type: str, body: bytes) -> tuple[dict[str, str], list[tuple[str, bytes]]]:
    msg = BytesParser(policy=email_policy).parsebytes(
        b"MIME-Version: 1.0\r\nContent-Type: " + content_type.encode("latin-1") + b"\r\n\r\n" + body)
    if not msg.is_multipart():
        raise UserError("Formulaire illisible.")
    fields, files = {}, []
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        data = part.get_payload(decode=True) or b""
        if part.get_filename() is not None:
            if data:
                files.append((part.get_filename(), data))
        elif name:
            fields[name] = data.decode("utf-8", "replace")
    return fields, files


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "MathOCR"

        def log_message(self, fmt, *args):  # journal discret
            log.info("%s %s", self.address_string(), fmt % args)

        def _send(self, code: int, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code: int = 200):
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            if path == "/api/exercices":
                return self._json({"exercices": app.exercises()})
            m = re.fullmatch(r"/api/job/([0-9a-f]{12})", path)
            if m:
                job = app.jobs.get(m.group(1))
                return self._json(job.public()) if job else self._json({"error": "tâche inconnue"}, 404)
            m = re.fullmatch(r"/fichiers/([0-9a-f]{12})/rapport\.html", path)
            if m:
                p = app.data / "copies" / m.group(1) / "rapport.html"
                if p.exists():
                    return self._send(200, p.read_bytes(), "text/html; charset=utf-8")
            self._json({"error": "introuvable"}, 404)

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length <= 0 or length > MAX_UPLOAD:
                    raise UserError(f"Envoi vide ou trop gros ({MAX_UPLOAD // 1024 // 1024} Mo au plus).")
                fields, files = parse_multipart(self.headers.get("Content-Type", ""), self.rfile.read(length))
                if path == "/api/enonce":
                    dest = app.data / "enonces" / uuid.uuid4().hex[:12]
                    job = app.prepare(save_images(files, dest))
                    return self._json(job.public(), 202)
                if path == "/api/correction":
                    exercise = app.exercise_path(fields.get("exercice", ""))
                    if exercise is None:
                        raise UserError("Choisis d'abord l'énoncé de l'exercice.")
                    job_dir = app.data / "copies" / uuid.uuid4().hex[:12]
                    job = app.correct(exercise, save_images(files, job_dir / "photos"), job_dir)
                    return self._json(job.public(), 202)
                self._json({"error": "introuvable"}, 404)
            except UserError as ex:
                self._json({"error": str(ex)}, HTTPStatus.BAD_REQUEST)

    return Handler


def serve(host: str, port: int, cfg_factory, data_dir: Path | None = None) -> None:
    app = App(data_dir or ROOT / "runs" / "web", cfg_factory)
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    print(f"MathOCR : http://{host}:{port}/  (Ctrl+C pour arrêter)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
