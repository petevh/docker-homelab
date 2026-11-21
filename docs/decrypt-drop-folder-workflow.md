# Decrypt drop folder (n8n)

Standalone workflow: drop a password-protected PDF into a OneDrive folder and a decrypted copy
appears next to it a couple of minutes later. Independent of the bank-statements workflow
(`docs/bank-statements-workflow.md`), but it shares the `pdf-decrypt` sidecar and the same
passwords file.

- Workflow: `n8n/workflows/decrypt-drop-folder.json` (n8n id `DropDecrypt001`); Code node source `n8n/workflows/src/pick-drop-files.js`
- Credential: "Onedrive - Pete" (same as the statements workflow)
- Watched folder: `_decrypt` (OneDrive root). Change it in the **Config** node (`drop`).

## Behaviour

Every 2 minutes (or **Manual Run**): list the drop folder → for each PDF (skipping files modified in
the last 60 s, so half-synced uploads are left for the next poll) → get the file's download link → download →
`POST /decrypt-any` on the sidecar → file the result → move the original to `Originals/`.
The Graph calls are copied from the working "Statement drop folder" workflow (`StmtDropFolder01`): list with no
`$select`, download URL from `GET items/{id}`, download with no credential, folders created up front, PATCH to move.

| Outcome | Result file | Original |
|---|---|---|
| Decrypted (a stored password worked) | `_decrypt/Decrypted/<name>` | moved to `Originals/` |
| Not encrypted | copied unchanged to `Decrypted/` | moved to `Originals/` |
| No stored password works / not a valid PDF | original copied to `_decrypt/Failed/<name>` | moved to `Originals/` |

- The original is moved only after the upload succeeded; if upload or move fails the file stays and
  is retried on the next poll. Nothing is deleted.
- Name clashes in the output folder are renamed by OneDrive, never overwritten.
- Non-PDFs and subfolders in the drop folder are ignored.
- `Failed/` is not announced anywhere: check it, or add a password to the passwords file and move the
  file back into the drop folder.

## Passwords

`/decrypt-any` tries no password first, then **every** password in `statement-passwords.json` (all
accounts, de-duplicated, in file order). Passwords never leave the sidecar; the response only says
`X-Decrypt-Status: decrypted | not-encrypted | failed`. The file is re-read per request, so adding a
password applies immediately. (The file is `statement-passwords.json`, with a hyphen.)

## Deploy

```
cd n8n && docker compose up -d --build --no-deps pdf-decrypt     # sidecar with /decrypt-any
```
The workflow itself is changed in the n8n editor by the user (new nodes, connections, settings, Publish).
Do **not** use `n8n import:workflow` over a stored workflow: it drops credential links and Settings and
resets the Config node (it once reset the drop folder). The repo's `decrypt-drop-folder.json` is a reference copy;
what runs is the stored workflow. Rules for touching the live n8n: `bank-automation/docs/N8N-DEPLOYMENT.md`
(API only, never the database). Code-node edits can go through that doc's `n8n-api-update.py`.

Gotcha: n8n 2.x runs the **published** version on the schedule but **Manual Run** uses the draft, so a
change that works by hand does nothing on the schedule until it is published.
