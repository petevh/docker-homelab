"""PDF decryption sidecar for n8n.

n8n's hardened image has no package manager, so qpdf lives here instead.
The per-account PDF passwords stay in this container: n8n only ever sends an
account key, so passwords never show up in n8n execution data.

  GET  /health                 liveness
  GET  /accounts               {key: {match, onedrive_folder}} -- no secrets
  POST /decrypt?account=KEY    body = PDF, response = decrypted PDF
                               (unencrypted PDFs are returned unchanged)
"""

import json
import os
import re
import subprocess
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/config/private-config.json")
PASSWORDS_PATH = os.environ.get("PASSWORDS_PATH", "/config/statement-passwords.json")
PORT = int(os.environ.get("PORT", "8080"))
MAX_BODY = 50 * 1024 * 1024


def slug(text):
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def load_passwords():
    """{account_key: password} from the separate passwords file; {} if absent."""
    try:
        with open(PASSWORDS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def load_banks():
    """Return {account_key: {decryption_password, onedrive_folder, match}}.

    Reads banks.<bank>.accounts[].statements from the v2.0 config. The key is
    the account's "key" if set, else "<bank>.<slug(name)>". Passwords come from
    the separate passwords file (keyed by account key) so the main config holds
    no secrets; a statements.decryption_password still in the main config is
    the fallback. Re-read on every request so edits apply without a restart.
    """
    with open(CONFIG_PATH, encoding="utf-8") as f:
        banks = json.load(f).get("banks", {})
    passwords = load_passwords()
    out = {}
    for bank_id, bank in banks.items():
        for acct in bank.get("accounts", []):
            st = acct.get("statements", {})
            key = acct.get("key") or f"{bank_id}.{slug(acct.get('name', ''))}"
            out[key] = {
                "decryption_password": passwords.get(key, st.get("decryption_password", "")),
                "onedrive_folder": st.get("onedrive_folder", ""),
                "match": st.get("match", {}),
            }
    return out


def qpdf(*args):
    return subprocess.run(["qpdf", *args], capture_output=True, text=True, timeout=60)


class DecryptError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def decrypt(pdf, password):
    """Return (pdf_bytes, was_encrypted)."""
    if b"%PDF-" not in pdf[:1024]:
        raise DecryptError(400, "not a PDF")
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "in.pdf")
        dst = os.path.join(tmp, "out.pdf")
        pwfile = os.path.join(tmp, "pw")
        with open(src, "wb") as f:
            f.write(pdf)

        # --is-encrypted: exit 0 = encrypted, 2 = not encrypted
        if qpdf("--is-encrypted", src).returncode != 0:
            return pdf, False

        # Password goes via a file so it never appears in the process list.
        with open(pwfile, "w", encoding="utf-8") as f:
            f.write(password)
        result = qpdf(f"--password-file={pwfile}", "--decrypt", src, dst)
        # exit 3 = succeeded with warnings
        if result.returncode not in (0, 3) or not os.path.exists(dst):
            if "invalid password" in result.stderr:
                raise DecryptError(422, "invalid password for this account")
            raise DecryptError(500, "qpdf failed: " + result.stderr.strip()[-300:])
        with open(dst, "rb") as f:
            return f.read(), True


class Handler(BaseHTTPRequestHandler):
    def send_json(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            self.send_json(200, {"status": "ok"})
        elif path == "/accounts":
            try:
                banks = load_banks()
            except (OSError, ValueError) as e:
                self.send_json(500, {"error": f"cannot load config: {e}"})
                return
            self.send_json(200, {
                key: {
                    "match": bank.get("match", {}),
                    "onedrive_folder": bank.get("onedrive_folder", ""),
                }
                for key, bank in banks.items()
            })
        else:
            self.send_json(404, {"error": "not found"})

    def do_POST(self):
        url = urlparse(self.path)
        if url.path != "/decrypt":
            self.send_json(404, {"error": "not found"})
            return
        account = parse_qs(url.query).get("account", [""])[0]
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > MAX_BODY:
            self.send_json(413 if length > MAX_BODY else 400, {"error": "bad body size"})
            return
        pdf = self.rfile.read(length)
        try:
            banks = load_banks()
            if account not in banks:
                raise DecryptError(404, f"unknown account '{account}'")
            out, was_encrypted = decrypt(pdf, banks[account].get("decryption_password", ""))
        except DecryptError as e:
            self.send_json(e.status, {"error": e.message, "account": account})
            return
        except (OSError, ValueError, subprocess.TimeoutExpired) as e:
            self.send_json(500, {"error": str(e), "account": account})
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Length", str(len(out)))
        self.send_header("X-Was-Encrypted", "true" if was_encrypted else "false")
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} {fmt % args}", flush=True)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
