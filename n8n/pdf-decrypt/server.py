"""PDF decryption sidecar for n8n.

n8n's hardened image has no package manager, so qpdf lives here instead.
The per-account PDF passwords stay in this container: n8n only ever sends an
account key, so passwords never show up in n8n execution data.

  GET  /health                 liveness
  GET  /accounts               {key: {match, onedrive_folder, folder_id, drive_id,
                               recorded}} -- no secrets
  POST /filed                  JSON {entries: [{account, file_name, folder}]}: add the files just
                               filed to the ledger -> {recorded, new: ["account|file_name", ...]}
  GET  /filed?month=YYYY-MM    files first filed in that month (UTC) + the account list, for
                               the monthly summary
  POST /folder-id?account=KEY  JSON {folder_id, drive_id, seen_path}: remember the OneDrive
                               folder id the workflow resolved for the account's
                               current onedrive_folder (state file, see below)
  POST /decrypt?account=KEY    body = PDF, response = decrypted PDF
                               (unencrypted PDFs are returned unchanged)
  POST /identify?accounts=A,B  body = PDF -> {owners: [keys whose by_content regex matches],
                               account: the key if exactly one, else ""}. Used by the
                               matcher to tell accounts apart when nothing else can: the key
                               whose `by_content` regex is found in the PDF's text. The body may be
                               base64 text (Content-Type: text/plain) instead of raw bytes.
"""

import base64
import json
import os
import re
import subprocess
import tempfile
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/config/private-config.json")
PASSWORDS_PATH = os.environ.get("PASSWORDS_PATH", "/config/statement-passwords.json")
# Folder ids the workflow has resolved, {account key: {path, folder_id, drive_id, seen_path,
# updated}}. Lives in its own writable directory (the config is mounted read-only) so
# the file can be replaced atomically.
FOLDER_IDS_PATH = os.environ.get("FOLDER_IDS_PATH", "/state/statement-folder-ids.json")
# Ledger of every file the workflow has filed, {"account|file_name": {account, file_name, folder,
# first_filed, last_filed, count}}. A re-upload of the same name only bumps last_filed/count,
# so the monthly summary counts a statement once. Same writable directory as the folder ids.
FILED_PATH = os.environ.get("FILED_PATH", "/state/filed-log.json")
PORT = int(os.environ.get("PORT", "8080"))
MAX_BODY = 50 * 1024 * 1024
# A bulk scan sends ~100 PDFs at once. The default accept queue (5) drops connections under
# that burst, so the client sees a reset or a timeout; keep it larger than any burst.
LISTEN_BACKLOG = int(os.environ.get("LISTEN_BACKLOG", "256"))
# qpdf runs per request thread; cap them so a burst queues instead of exhausting CPU/memory.
QPDF_SLOTS = threading.BoundedSemaphore(int(os.environ.get("QPDF_CONCURRENCY", "4")))
MAX_JSON_BODY = 16 * 1024
MAX_FILED_BODY = 256 * 1024
MAX_FILED_ENTRIES = 500
MONTH_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
ID_PATTERN = re.compile(r"^[A-Za-z0-9!_=.$-]{1,256}$")
state_lock = threading.Lock()


def slug(text):
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def load_passwords():
    """{account_key: password or [passwords]} from the separate file; {} if absent."""
    try:
        with open(PASSWORDS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def clean_folder(path):
    return (path or "").strip().strip("/")


def load_folder_ids():
    """{account key: entry} from the state file; {} if absent or unreadable."""
    try:
        with open(FOLDER_IDS_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_folder_id(account, onedrive_folder, folder_id, drive_id, seen_path):
    """Record the id resolved for `account`, keyed to the folder path configured now.

    Written to a temporary file in the same directory and renamed over the old one, so
    a crash never leaves a half-written file. Raises OSError if the state directory is
    not writable. Returns the entry it replaced ({} if there was none).
    """
    with state_lock:
        data = load_folder_ids()
        previous = data.get(account) if isinstance(data.get(account), dict) else {}
        data[account] = {
            "path": clean_folder(onedrive_folder),
            "folder_id": folder_id,
            "drive_id": drive_id,
            "seen_path": seen_path,
            "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        write_json_atomic(FOLDER_IDS_PATH, data)
    return previous


def write_json_atomic(path, data):
    """Write to a temporary file next to `path`, then rename over it: never half-written."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def load_filed():
    try:
        with open(FILED_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def record_filed(entries):
    """Add files to the ledger; return the keys that had not been filed before."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    new = []
    with state_lock:
        data = load_filed()
        for e in entries:
            key = f"{e['account']}|{e['file_name']}"
            old = data.get(key)
            if old:
                old.update(last_filed=now, count=old.get("count", 1) + 1, folder=e["folder"] or old.get("folder", ""))
            else:
                data[key] = {**e, "first_filed": now, "last_filed": now, "count": 1}
                new.append(key)
        write_json_atomic(FILED_PATH, data)
    return new


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
                "folder_id": st.get("folder_id", ""),
                "drive_id": st.get("drive_id", ""),
                "match": st.get("match", {}),
                "link": st.get("link", {}),
                "filename_template": st.get("filename_template", ""),
                "date_source": st.get("date_source", ""),
                "month_offset": st.get("month_offset", 0),
                "sequence": st.get("sequence", {}),
                "purge_email": bool(st.get("purge_email", False)),
                "has_password": any(as_list(passwords.get(key, st.get("decryption_password", "")))),
                "name": acct.get("name", ""),
            }
    return out


def qpdf(*args):
    with QPDF_SLOTS:
        return subprocess.run(["qpdf", *args], capture_output=True, text=True, timeout=60)


class DecryptError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def as_list(passwords):
    """A password entry may be one string or a list tried in order (never empty)."""
    if isinstance(passwords, str):
        passwords = [passwords]
    return [p for p in passwords if isinstance(p, str)] or [""]


def decrypt(pdf, passwords):
    """Return (pdf_bytes, was_encrypted, index_of_password_that_worked).

    Tries each password in order, so a statement period with a wrong password on
    file, a rotated password, or alternatives the bank uses all work.
    """
    if b"%PDF-" not in pdf[:1024]:
        raise DecryptError(400, "not a PDF")
    candidates = as_list(passwords)
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "in.pdf")
        dst = os.path.join(tmp, "out.pdf")
        pwfile = os.path.join(tmp, "pw")
        with open(src, "wb") as f:
            f.write(pdf)

        # --is-encrypted: exit 0 = encrypted, 2 = not encrypted
        if qpdf("--is-encrypted", src).returncode != 0:
            return pdf, False, -1

        # Many "encrypted" statements only carry owner restrictions and open with no
        # password at all. qpdf rejects a *wrong* password even then, so try none first.
        # --requires-password exit: 0 = a password is needed, 3 = opens without one.
        if qpdf("--requires-password", src).returncode == 3:
            result = qpdf("--decrypt", src, dst)
            if result.returncode in (0, 3) and os.path.exists(dst):
                with open(dst, "rb") as f:
                    return f.read(), True, -2

        for index, password in enumerate(candidates):
            # Password goes via a file so it never appears in the process list.
            with open(pwfile, "w", encoding="utf-8") as f:
                f.write(password)
            if os.path.exists(dst):
                os.remove(dst)
            result = qpdf(f"--password-file={pwfile}", "--decrypt", src, dst)
            # exit 3 = succeeded with warnings
            if result.returncode in (0, 3) and os.path.exists(dst):
                with open(dst, "rb") as f:
                    return f.read(), True, index
            if "invalid password" not in result.stderr:
                raise DecryptError(500, "qpdf failed: " + result.stderr.strip()[-300:])
        if not any(candidates):
            raise DecryptError(422, "this PDF needs a password but none is set for this account in statement-passwords.json")
        raise DecryptError(422, f"invalid password for this account (tried {len(candidates)})")


def pdf_text(pdf):
    """Text shown in a (decrypted) PDF, one string per text-showing operator; '' if none found.

    Reads the literal strings of Tj/TJ operators from the uncompressed form qpdf writes. That is
    enough for statements whose fonts use plain character codes (FNB's); PDFs with custom glyph
    encodings give no text, and the caller then cannot tell the accounts apart.
    """
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "in.pdf")
        dst = os.path.join(tmp, "out.pdf")
        with open(src, "wb") as f:
            f.write(pdf)
        if qpdf("--qdf", "--object-streams=disable", src, dst).returncode not in (0, 3):
            return ""
        with open(dst, "rb") as f:
            data = f.read()
    lines = []
    for arr in re.findall(rb"\[(.*?)\]\s*TJ", data, re.S) + re.findall(rb"(\((?:\\.|[^\\()])*\))\s*Tj", data):
        parts = re.findall(rb"\(((?:\\.|[^\\()])*)\)", arr)
        lines.append(b"".join(parts).decode("latin-1"))
    return "\n".join(lines)


def identify(pdf, banks, candidates):
    """Which of `candidates` (account keys) does this PDF belong to?

    Decrypts the PDF with the candidates' passwords (statements that open without one are
    fine), reads its text and returns the keys with a `by_content` regex in their match rules
    that is found in it (case-insensitive), e.g. the account holder's name. The caller treats
    anything but exactly one key as "cannot tell".
    """
    opened = None
    for key in candidates:
        try:
            opened = decrypt(pdf, banks[key].get("decryption_password", ""))[0]
            break
        except DecryptError as e:
            if e.status != 422:
                raise
    if opened is None:
        return []
    text = pdf_text(opened)
    owners = []
    for key in candidates:
        rules = banks[key].get("match", {})
        for rule in rules if isinstance(rules, list) else [rules]:
            pattern = rule.get("by_content") if isinstance(rule, dict) else None
            if pattern and re.search(pattern, text, re.I):
                owners.append(key)
                break
    return owners


def log_folder_change(account, previous, folder_id, seen_path):
    """One line per change worth knowing about, in `docker logs n8n-pdf-decrypt`."""
    old_path = previous.get("seen_path", "")
    if not previous.get("folder_id"):
        print(f"folder recorded: {account} -> '{seen_path}' ({folder_id})", flush=True)
    elif previous["folder_id"] != folder_id:
        print(f"folder id changed: {account} '{old_path}' ({previous['folder_id']}) -> '{seen_path}' ({folder_id})", flush=True)
    elif old_path != seen_path:
        print(f"folder moved: {account} '{old_path}' -> '{seen_path}'", flush=True)


def recorded_for(entry, onedrive_folder):
    if isinstance(entry, dict) and entry.get("folder_id") and entry.get("path") == clean_folder(onedrive_folder):
        return {k: entry.get(k, "") for k in ("folder_id", "drive_id", "seen_path")}
    return {}


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
            recorded = load_folder_ids()
            self.send_json(200, {
                key: {
                    "match": bank.get("match", {}),
                    "onedrive_folder": bank.get("onedrive_folder", ""),
                    "folder_id": bank.get("folder_id", ""),
                    "drive_id": bank.get("drive_id", ""),
                    # the id found earlier, but only while it was recorded for the folder
                    # path configured now: editing onedrive_folder triggers a fresh lookup
                    "recorded": recorded_for(recorded.get(key), bank.get("onedrive_folder", "")),
                    "link": bank.get("link", {}),
                    "filename_template": bank.get("filename_template", ""),
                    "date_source": bank.get("date_source", ""),
                    "month_offset": bank.get("month_offset", 0),
                    "sequence": bank.get("sequence", {}),
                    "purge_email": bank.get("purge_email", False),
                    "has_password": bank.get("has_password", False),  # whether one is on file, never the value
                    "name": bank.get("name", ""),
                }
                for key, bank in banks.items()
            })
        elif path == "/filed":
            month = parse_qs(urlparse(self.path).query).get("month", [""])[0]
            if not MONTH_PATTERN.match(month):
                self.send_json(400, {"error": "month must be YYYY-MM"})
                return
            try:
                banks = load_banks()
            except (OSError, ValueError) as e:
                self.send_json(500, {"error": f"cannot load config: {e}"})
                return
            filed = sorted(
                (e for e in load_filed().values() if str(e.get("first_filed", "")).startswith(month)),
                key=lambda e: (e["account"], e["first_filed"]),
            )
            self.send_json(200, {
                "month": month,
                "filed": filed,
                "accounts": {k: {"name": b.get("name", ""), "onedrive_folder": b.get("onedrive_folder", "")} for k, b in banks.items()},
            })
        else:
            self.send_json(404, {"error": "not found"})

    def do_POST(self):
        url = urlparse(self.path)
        if url.path == "/filed":
            self.record_filed_files()
            return
        if url.path == "/folder-id":
            self.record_folder_id(parse_qs(url.query).get("account", [""])[0])
            return
        if url.path == "/identify":
            self.identify_owner(parse_qs(url.query).get("accounts", [""])[0])
            return
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
            out, was_encrypted, used = decrypt(pdf, banks[account].get("decryption_password", ""))
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
        # which entry of the account's password list worked (-1: not encrypted,
        # -2: encrypted but needed no password)
        self.send_header("X-Password-Index", str(used))
        self.end_headers()
        self.wfile.write(out)

    def identify_owner(self, accounts):
        keys = [k for k in accounts.split(",") if k]
        length = int(self.headers.get("Content-Length", "0"))
        if not 2 <= len(keys) <= 8:
            self.send_json(400, {"error": "accounts must list 2 to 8 account keys"})
            return
        if length <= 0 or length > MAX_BODY:
            self.send_json(413 if length > MAX_BODY else 400, {"error": "bad body size"})
            return
        pdf = self.rfile.read(length)
        if self.headers.get("Content-Type", "").startswith("text/plain"):
            # n8n's Code node cannot pass binary through its request helper, so it sends base64 text
            try:
                pdf = base64.b64decode(pdf, validate=True)
            except ValueError:
                self.send_json(400, {"error": "body is not valid base64"})
                return
        try:
            banks = load_banks()
            unknown = [k for k in keys if k not in banks]
            if unknown:
                raise DecryptError(404, f"unknown account '{unknown[0]}'")
            owners = identify(pdf, banks, keys)
        except DecryptError as e:
            self.send_json(e.status, {"error": e.message})
            return
        except (OSError, ValueError, subprocess.TimeoutExpired) as e:
            self.send_json(500, {"error": str(e)})
            return
        print(f"identify {','.join(keys)} -> {','.join(owners) or 'nobody'}", flush=True)
        self.send_json(200, {"owners": owners, "account": owners[0] if len(owners) == 1 else ""})

    def record_filed_files(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > MAX_FILED_BODY:
            self.send_json(400, {"error": "bad body size"})
            return
        try:
            raw = json.loads(self.rfile.read(length)).get("entries", [])
            if not isinstance(raw, list) or len(raw) > MAX_FILED_ENTRIES:
                raise ValueError("entries must be a list of at most %d" % MAX_FILED_ENTRIES)
            banks = load_banks()
            entries = []
            for e in raw:
                account, name, folder = e.get("account"), e.get("file_name"), e.get("folder", "")
                if account not in banks or not isinstance(name, str) or not name or len(name) > 300:
                    raise ValueError(f"bad entry for account '{account}'")
                entries.append({"account": account, "file_name": name, "folder": str(folder)[:1024]})
            new = record_filed(entries)
        except (AttributeError, TypeError, ValueError) as e:
            self.send_json(400, {"error": str(e)})
            return
        except OSError as e:
            self.send_json(500, {"error": f"cannot save ledger: {e}"})
            return
        self.send_json(200, {"recorded": len(entries), "new": new})

    def record_folder_id(self, account):
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > MAX_JSON_BODY:
            self.send_json(400, {"error": "bad body size"})
            return
        try:
            body = json.loads(self.rfile.read(length))
            folder_id = body.get("folder_id", "")
            drive_id = body.get("drive_id", "")
            seen_path = str(body.get("seen_path", ""))[:1024]
            if not ID_PATTERN.match(folder_id) or (drive_id and not ID_PATTERN.match(drive_id)):
                raise ValueError("folder_id/drive_id are not valid OneDrive ids")
            banks = load_banks()
            if account not in banks:
                self.send_json(404, {"error": f"unknown account '{account}'"})
                return
            previous = save_folder_id(account, banks[account].get("onedrive_folder", ""), folder_id, drive_id, seen_path)
            log_folder_change(account, previous, folder_id, seen_path)
        except (AttributeError, TypeError, ValueError) as e:
            self.send_json(400, {"error": str(e)})
            return
        except OSError as e:
            self.send_json(500, {"error": f"cannot save folder id: {e}"})
            return
        self.send_json(200, {"recorded": account})

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} {fmt % args}", flush=True)


class Server(ThreadingHTTPServer):
    request_queue_size = LISTEN_BACKLOG
    daemon_threads = True


if __name__ == "__main__":
    Server(("0.0.0.0", PORT), Handler).serve_forever()
