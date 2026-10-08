#!/usr/bin/env python3
"""Porte d'entrée HTTPS du suivi Claude Code (l'environnement cloud ne peut pas faire de SSH).

Expose EXACTEMENT les mêmes actions que la clé SSH restreinte, en déléguant à followup.sh :
  GET  /report            rapport complet (lecture seule)
  GET  /logs/<service>    journaux d'un service
  POST /deploy/<ref>      lance un déploiement sécurisé (safe_deploy.sh) en arrière-plan → 202
  GET  /deploy            avancement / résultat du dernier déploiement
  POST /notify            corps = texte du message Telegram
Authentification : en-tête « Authorization: Bearer <jeton> » (jeton dans /etc/apex/gateway.token).
Écoute sur 127.0.0.1 uniquement ; Caddy fournit le HTTPS public.
"""
import hmac
import os
import re
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FOLLOWUP = "/opt/apex/deploy/followup.sh"
DEPLOY_LOG = "/var/log/apex-gateway-deploy.log"
TOKEN = open("/etc/apex/gateway.token").read().strip()
REF_OK = re.compile(r"^[A-Za-z0-9._/-]{1,100}$")
SERVICES = {"ingestor", "features", "labeler", "learner", "supervisor", "notifier", "dashboard", "trader"}
_deploy: dict = {"proc": None}
_lock = threading.Lock()


def run(cmd: str, stdin: bytes | None = None, timeout: int = 300) -> tuple[int, bytes]:
    env = {**os.environ, "SSH_ORIGINAL_COMMAND": cmd}
    p = subprocess.run([FOLLOWUP], input=stdin, env=env, capture_output=True, timeout=timeout)
    return p.returncode, p.stdout + p.stderr


class Handler(BaseHTTPRequestHandler):
    server_version = "apex-gateway"

    def log_message(self, fmt, *args):  # journal minimal (pas de corps ni d'en-têtes)
        print(f"{self.address_string()} {self.command} {self.path.split('?')[0]}", flush=True)

    def _auth(self) -> bool:
        got = self.headers.get("Authorization", "")
        if hmac.compare_digest(got.encode(), f"Bearer {TOKEN}".encode()):
            return True
        self._send(401, b"non autorise\n")
        return False

    def _send(self, code: int, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._auth():
            return
        path = self.path.split("?")[0].rstrip("/")
        if path == "/report":
            code, out = run("report")
            return self._send(200 if code == 0 else 500, out)
        if path.startswith("/logs/") and path[6:] in SERVICES:
            return self._send(200, run(f"logs {path[6:]}")[1])
        if path == "/deploy":
            with _lock:
                p = _deploy["proc"]
                state = "aucun" if p is None else ("en cours" if p.poll() is None else f"termine (code {p.returncode})")
            tail = b""
            if os.path.exists(DEPLOY_LOG):
                with open(DEPLOY_LOG, "rb") as f:
                    tail = f.read()[-4000:]
            return self._send(200, f"etat : {state}\n".encode() + tail)
        self._send(404, b"inconnu\n")

    def do_POST(self):
        if not self._auth():
            return
        path = self.path.split("?")[0].rstrip("/")
        n = int(self.headers.get("Content-Length") or 0)
        if n > 8000:
            return self._send(413, b"trop long\n")
        body = self.rfile.read(n) if n else b""
        if path == "/notify":
            return self._send(200, run("notify", stdin=body, timeout=60)[1])
        if path.startswith("/deploy/") and REF_OK.match(path[8:]):
            with _lock:
                p = _deploy["proc"]
                if p is not None and p.poll() is None:
                    return self._send(409, b"un deploiement est deja en cours (GET /deploy)\n")
                log = open(DEPLOY_LOG, "wb")
                env = {**os.environ, "SSH_ORIGINAL_COMMAND": f"deploy {path[8:]}"}
                _deploy["proc"] = subprocess.Popen([FOLLOWUP], env=env, stdout=log, stderr=subprocess.STDOUT)
            return self._send(202, b"deploiement lance : suivre avec GET /deploy (environ 6 min)\n")
        self._send(404, b"inconnu\n")


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 8787), Handler).serve_forever()
