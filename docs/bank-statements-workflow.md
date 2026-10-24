# Bank & statement filing workflow (n8n)

Files every emailed statement (bank, card, property agent, municipality, medical aid) into
OneDrive, decrypting PDFs on the way. One mailbox in, one folder tree out, driven by a config
file. It does **not** (yet) read the contents of the PDFs or touch YNAB.

- Workflow: `n8n/workflows/bank-statements.json` (n8n id `BankStmtGmail001`)
- Sidecar: `n8n/pdf-decrypt/` (container `n8n-pdf-decrypt`)
- Code of the two Code nodes: `n8n/workflows/src/*.js` (edit these, then run the build script)
- Config **schema and field reference**: `bank-automation/docs/statements-config.md`
  (bank-automation owns the config; this repo only deploys it)

## 1. How it fits together

```
 statements@vanheerden.ch (Gmail)
        │  hourly poll                         manual: click "Execute workflow"
        ▼                                              │
  Gmail Trigger ───────────────┐          Manual Scan → Scan Inbox (last 30 days)
                               ▼                       │
                        Match Statements ◄─────────────┘
              (rules from  GET pdf-decrypt:8080/accounts)
                               ▼
                       Fetch Linked PDFs      (only for "link" statements, e.g. ReMax)
                               ▼
                         Decrypt PDF ──error──► Decrypt Error ─────────────┐
                               ▼              POST pdf-decrypt:8080/decrypt?account=KEY
                  Measure → Check Existing → Decide → Needs Upload?   (is a file with this name already in the folder?)
                               ▼ new                         └ exists: skipped (same size) / flagged (different)
                      Upload to OneDrive      (Graph PUT into the folder by id, else by path; never overwrites)
                       │            └─error─► Folder Error ──────────────┤
                       ▼
                     Record Folder ─────────┤ (remembers the folder id via POST pdf-decrypt:8080/folder-id)
                                            ▼
   Collect Results → Run Summary → Email Summary → Any Failures? → Run Failed   (one email; run fails only if something failed)
```

| Piece | Role |
|---|---|
| **Gmail Trigger** | Polls hourly. Query: `{(has:attachment filename:pdf) from:pete@vanheerden.ch from:weconnectu.co.za}` (any PDF mail, anything forwarded by Pete, and ReMax link mails). Credential *Gmail - statements*. |
| **Manual Scan / Scan Inbox** | Same query plus `newer_than:30d`, up to 100 emails. Works whether or not the workflow is published. Edit `newer_than` to widen the window. |
| **Match Statements** | Decides which account each PDF belongs to and what to call it. Fetches the account list itself from the sidecar. |
| **Fetch Linked PDFs** | For statements delivered as a download link: follows the link, keeps only a real PDF. Other items pass through. |
| **Decrypt PDF** | Sends the PDF + account key to the sidecar. Its error output goes to **Decrypt Error**. |
| **Upload to OneDrive** | `PUT .../items/<folder id>:/<file>:/content` when a folder id is known, else `PUT /me/drive/root:/<folder>/<file>:/content` (Graph creates missing folders). `conflictBehavior=fail` (never overwrites), 5 tries 10 s apart. Credential *Onedrive - Pete*. See 2.6. |
| **Record Folder** | After a successful upload: add the file to the ledger (`state/filed-log.json`, via the sidecar's `POST /filed`) and record the folder id (and where the folder currently is). |
| **Decrypt Error / Folder Error** | One failure line per file that could not be decrypted / uploaded. The rest of the run carries on. |
| **Mail Outcome → Mailbox? → Prepare/Create Labels/List Labels/Expand/Mark/Purge?/Trash (per mailbox)** | Runs before the summary (n8n runs the node placed higher on the canvas first, so keep it above). Labels each handled email and trashes it when the account has `purge_email`. See 2.9. |
| **Collect Results → Run Summary → Email Summary → Any Failures? → Run Failed** | The Merge waits for every branch, so this runs once at the end. It emails (*Gmail - statements* → pete@vanheerden.ch) when something failed, or after any **manual scan** (full list of what was filed, marked new or replaced, plus failures). A scheduled/hourly run where everything worked sends nothing. If anything failed the run then ends red (**Run Failed**), manual or scheduled. A scan that matches no statements sends nothing. |
| **Bank statements: monthly summary** (separate workflow, `workflows/monthly-summary.json`, id `BankStmtMonthly001`) | On the 1st at 08:00 it emails the statements first filed in the previous month, per account, and the accounts that had none. **Manual Run** sends the same for the previous month on demand. Reads the ledger from the sidecar (`GET /filed?month=YYYY-MM`). |
| **pdf-decrypt sidecar** | qpdf over HTTP. Holds the passwords so they never appear in n8n execution data. Internal only (no Traefik labels), runs read-only as uid 3000. |

n8n itself cannot read the config files (NAS permissions are 770 for `pvh`, n8n runs as uid 1000),
which is why the sidecar serves the account list.

## 2. What happens to an email

### 2.1 Matching
Each account has `statements.match`: one rule `{from, to, subject, filename}` or a **list** of rules.

- `from`, `to`: case-insensitive *substring* of the sender / the To header.
- `label`: a Gmail label on the email, whole name, case-insensitive (e.g. `Rob`). For mail that nothing in the headers
  can tell apart (FNB credit-card statements for two people sent to one address). The scan paths of **Manual Scan** and
  **Manual Scan Pete/Julia** look the label names up first (the *Labels …* node); the hourly Gmail Trigger does not,
  so a `label` rule never matches there (use a `to` address and a forwarding filter for ongoing mail).
  Typical use: `[ {from, to: "rob@", filename}, {from, label: "Rob", filename} ]`, any rule may match.
- `by_content: "regex"` (on a rule): a last resort for accounts nothing in the email can tell apart, such as two
  people's `SAVINGS ACCOUNT n.pdf`. It is only used when no ordinary rule of any account matched. If one account's
  `by_content` rule matches the email, that account gets the file. If several do, the matcher sends the PDF to the
  sidecar (`POST /identify`), which reads the PDF's text and answers with the account whose `by_content` regex is
  found in it (case-insensitive), typically the account holder's name as printed on the statement. If none, or more
  than one, is found, the PDF stays unmatched. The PDF is decrypted again afterwards, as usual, by the account it
  was given to; this happens only for the files that fall through to this rule.
  Typical use: add `{from, filename, by_content: "HOLDER NAME"}` to each of the accounts that share the file name.
- `subject`, `filename`: case-insensitive *regular expression*.
- Empty fields are ignored; every non-empty field of a rule must match; a rule with no fields never matches.
- With a list, any rule may match. The account with the **most matching fields** wins; ties go to
  the one earlier in the config.
- Emails matching nothing are skipped silently (receipts, tax invoices, marketing).
- **Attachment names are what n8n/Gmail report**, which differs from the name in the mail client:
  a `/` in a name truncates it to the part after the slash (the Amber levy file arrives as
  ` Van Heerden R._AND_J.pdf`). Check real names with the run tool (section 6) before writing a `filename` rule.
  This is what `filename` rules match against. The saved name's `{original}` is rebuilt from n8n's `directory` +
  `fileName` (n8n puts the part before the last `/` in `directory`), so it keeps the whole name with `/` shown as
  `_` (`Mortgage Loans Statement - 05/09/2026.pdf` → `Mortgage Loans Statement - 05_09_2026`).

### 2.2 Forwarded emails
For testing and for senders that deliver to another mailbox, a forward is matched as if it were the
original. Forwards from **trusted forwarders** (`TRUSTED_FORWARDERS` at the top of
`workflows/src/match-statements.js`: `pete@vanheerden.ch`, `julia@vanheerden.ch`) are unwrapped: the
original sender, subject, date and recipient are read from the quoted block (Gmail
"Forwarded message" or Outlook `From/Sent/To/Subject`, including wrapped addresses).

- **Authentication is required.** A forward is only unwrapped if Google's own
  `Authentication-Results` header shows an *aligned* pass for the trusted domain: SPF passed with an
  envelope sender in that domain, or DKIM passed signed by that domain. This matters because the
  domain's DMARC policy is currently `p=none` (nothing enforces it), so DNS alone does not stop a
  spoofed `From: julia@…`. A failed or missing check means "not trusted", and the mail goes unmatched.
- Both recipients are kept: the forward's own To (so `to: statements+rob@` works on a forward sent
  to that address) and the original To.
- Gmail filter **auto-forwarding** keeps the original headers; no unwrapping is needed for those.

### 2.3 Link statements
ReMax sends a mail with a download link instead of an attachment. For an account with
`statements.link = {enabled: true, allow_hosts: [...]}` and no matching attachment:

1. Links are taken from the mail (HTML hrefs and plain-text URLs), keeping only hosts on `allow_hosts`
   (max 15).
2. **Fetch Linked PDFs** tries each; the first response that is HTTP 200, ≤ 50 MB and starts with
   `%PDF-` is used. If none qualifies the run **fails loudly** (it names the account and what was tried).
3. `match` rules with a `filename` cannot apply to link mails (there is no attachment).

The link in ReMax mails is a SendGrid click-tracker that redirects to a PDF; it needs no login and is
reportedly valid for about a month (one older link was seen expiring the same day, so don't rely on it
lasting).

### 2.4 Decryption
The sidecar tries, in order:

1. **No password**, if `qpdf --requires-password` says the file opens without one. Many "encrypted"
   statements only carry owner restrictions (FNB cards). This matters because qpdf rejects a *wrong*
   password even when none would have worked.
2. Each password for that account from `statement-passwords.json`, in list order.

If none works: HTTP 422 `invalid password for this account (tried N)` → **Decrypt Error**. That file is not
filed and you get an alert email; the other files in the run are still processed and the run ends marked failed.
Fix the password (or pause the account's rule), then run Manual Scan.
Unencrypted PDFs pass through unchanged.

**Telling accounts apart by content** (`POST /identify?accounts=A,B`): for files nothing in the email can
attribute (two people's `SAVINGS ACCOUNT n.pdf`), see `by_content` in 2.1. The sidecar decrypts the PDF and
searches its text (the literal strings of the page content; works for FNB's statements, not for PDFs with custom
glyph encodings) for each candidate's `by_content` regex, and answers with the one that is found (`account`), or an
empty answer if none or several are. Each call logs `identify A,B -> KEY|nobody`. Trying the accounts' passwords
instead does not work for FNB savings: those PDFs open without any password.

Response header `X-Password-Index`: `-1` not encrypted, `-2` encrypted but needed no password,
`0..n` index of the password list entry that worked. It never contains the password.

### 2.5 Naming
Saved as `<onedrive_folder>/<template>.pdf`. Dates in file names use periods (`2026.10.07`), not hyphens. Default template: `{date}_{key}_{original}`.

`{date}` = the **statement's own date** if the original file name (or, for link statements, the
subject) contains one, otherwise the **email date**. For a forward the email date is when the *original*
was sent, not when it was forwarded.

| In the name | `{date}` |
|---|---|
| `0000000000_20261007.PDF`, `2026-10-02`, `25092026` (day-month-year), `18 SEP 2026` | that day, `2026.10.07` … |
| `01Jul2026-31Jul2026`, `[2026-07-01 to 2026-10-01]` (a range) | the **later** date (`2026.10.01` …) |
| `Oct_2026`, `SEPTEMBER 2026`, `202610` (month only) | `2026.10` (no day) |
| nothing recognisable | email date, unless the account has a `sequence` (below) |

**Sequence numbers** (`statements.sequence`): FNB online-banking backlog statements carry a running number
and no date (`FNB PREMIER CREDIT 566.pdf`). One known point fixes the rest, one statement per month:
`"sequence": { "anchor": { "number": 566, "month": "2026-09" } }` (optional `"regex"`, default: the number at the
end of the name). The month is the one the statement is filed under: a credit card statement issued in the
first week of October covers, and is named for, September; current and savings statements issued near month end
are named for that month. `{date}` becomes `2026.09`. If the bank skips a month the numbering drifts from the calendar.

Per account: `statements.month_offset: -1` shifts the resolved month back (or forward) by that many months and
drops the day, for statements issued after the period they cover (ABSA: issued in September, covers August);
`statements.date_source: "email"` forces the email date; `statements.filename_template`
replaces the whole pattern. Tokens: `{date} {emaildate} {year} {month} {key} {name} {subject} {original}`
(`{original}` has no extension; `.pdf` is always appended). Use `{subject}` when attachments have generic
names (`Attachment.pdf`). Keep `{original}` or something equally unique: two files with the same final name
clash (see 2.8). Characters illegal in OneDrive names (`\ / : * ? " < > | # %`) become `_`.

### 2.6 Folders and ids (moving or reorganising folders)
Files are filed by **folder id**, so you can move or rename the folders in OneDrive without touching the
config. Per account, in this order:

1. `statements.folder_id` (and `drive_id` for someone else's drive) in `private-config.json`: always wins.
2. The id the workflow recorded for the account, kept in `bank-automation/state/statement-folder-ids.json`
   (written by the sidecar, git-ignored, **back it up with the config**). It is only used while it was
   recorded for the `onedrive_folder` currently configured, so changing `onedrive_folder` on purpose
   triggers a fresh lookup.
3. Otherwise the `onedrive_folder` path. The first upload by path finds an existing folder (or creates a
   missing one) and the reply names it, so the next upload uses its id. Existing folders are adopted, not duplicated.

After each upload the sidecar also stores `seen_path`, the folder's current path, so the state file tells you
where a moved folder ended up. `onedrive_folder` in the config can then go stale: it is only the
starting point and the label for the first lookup, so update it when convenient.

If the id returns **404/410** (folder deleted, or moved to another drive, which changes ids) that file is not
filed and the path is **not** recreated. The rest of the run carries on, you get one email naming the
account, the id, the configured path and the last known location, and the run ends marked failed. Fix it by
setting `folder_id` to the folder's new id, or by deleting that account's entry in `statement-folder-ids.json`
(next upload looks the path up again), then run Manual Scan. 401/403/429/5xx are reported the same way but described as
access/service problems and never clear an id. The Gmail trigger does not re-offer an email it already saw;
that is why the fix is followed by Manual Scan.

**Finding a folder's id.** Normally you never need to: it is recorded in `state/statement-folder-ids.json`. To look one up
(or check that an id is still alive), open the workflow **Find OneDrive folder id (helper)** (`workflows/find-folder-id.json`,
id `FindFolderId001`, never activated), fill one field in its **Input** node, and click **Execute workflow**:

| Fill in | You get |
|---|---|
| `account` (key from `private-config.json`) | the id of that account's `onedrive_folder` |
| `path` (from the OneDrive root) | the id of that folder |
| `folder_id` (+ `drive_id` for someone else's drive) | whether it still exists and where it is now |

The **Report** node prints `folder_id`, `drive_id`, the current path and a `statements` snippet to paste into the config.
It uses the *Onedrive - Pete* credential and only reads. Paths are looked up in your own drive.

Folder moves and id changes are logged by the sidecar: `docker logs n8n-pdf-decrypt | grep folder`
(`folder recorded`, `folder moved`, `folder id changed`).

### 2.8 Files that already exist
Before each upload the workflow asks OneDrive whether a file with the target name is already in the folder
(**Measure**: exact byte size of the decrypted PDF; **Check Existing**: Graph lookup by id/path; **Decide**):

| In OneDrive | What happens |
|---|---|
| nothing with that name | uploaded (never with overwrite: `conflictBehavior=fail`) |
| same name, **same size** | **skipped**, listed under SKIPPED in the manual-scan email; not added to the ledger |
| same name, **different size** | the existing file is left alone and the new one is filed as **`<name> (2).pdf`**, with a note under FILED in the summary email ("a different file already had the original name": compare the two). The `(2)` name is judged the same way: free → uploaded, same size → skipped (so a re-scan makes no third copy), a third, different file → **not filed**, listed as a failure. |
| could not check (timeout/error, 5 tries) | upload is tried anyway; if the name turns out to exist, OneDrive refuses ("The specified item name already exists") and the file is listed as **skipped (exists; the check timed out so it was not compared)**, not as a failure; its email counts as filed |

This applies to hourly runs and manual scans alike, so a bank re-sending a statement does not create copies and a
corrected statement is never silently overwritten or lost. Size, not a hash, is compared (n8n's Code node cannot
hash); the same statement decrypts to the same bytes. One Graph `$batch` call (**Check Existing**) asks about both
the name and its `(2)` variant, so the extra check costs no extra request.

HTTP timeouts (so a stalled connection fails and retries instead of hanging for minutes): Check Existing 30 s,
Upload to OneDrive 60 s, Decrypt PDF 90 s (3 tries), the Gmail label/mark/trash calls 30 s (Mark: 3 tries).

### 2.9 Marking and cleaning up the emails
After every run **Mail Outcome** looks at each email that was handled and marks it in the mailbox it came from
(Gmail API, labels created on first use):

| Outcome | Label | Trash? |
|---|---|---|
| every PDF of it is in OneDrive (uploaded now, or an identical file was already there) | `Statements/Filed` | only if **every** account involved has `statements.purge_email: true` and the email has no PDF that matched no account |
| at least one PDF failed (decrypt, upload, a clashing file) | `Statements/Failed` | never |
| matched nothing | untouched | never |

A later successful run swaps `Failed` for `Filed`. "Trash" is Gmail's Trash: recoverable for 30 days, then purged.
Your own label-based clean-up rules can take over from `Statements/Filed`. Re-running a manual scan also labels
mail that was filed before this existed. Marking and trashing never fail the run; if they do not work the
Run Summary email still counts what was intended. Nothing is deleted unless `purge_email` is set per account.

**Other mailboxes (temporary).** The workflow has two extra, normally idle entry points: **Manual Scan Pete** and
**Manual Scan Julia** (each: trigger → Scan → Tag → Match). To use one:
1. **Pete:** the nodes already use the credential *Gmail - pete@vanheerden.ch*. **Julia:** create a Gmail OAuth2 credential signed in as Julia, and select it on **Scan Julia** and the *Julia* nodes (Create/List Labels, Mark, Trash); they have no credential until you do.
2. Adjust the search on the Scan node. Default: PDF attachments, not yet labelled `statements-filed`, from the senders (and Gmail labels `Julia`/`Rob`) used in the `match` rules, max 500 messages (Pete: last 365 days, Julia: last 180). The sender list is a one-off snapshot for catching up on history: new senders go into the account's `match` rule and the Gmail Trigger query, not into these scans.
3. Open the **Manual Scan Pete** node and execute from it. Results go through the same matching, filing, skipping and summary email as a normal scan; emails are labelled (and trashed where `purge_email` is set) **in that user's mailbox**.
4. When finished: delete the credential in n8n (if it was only for this) and revoke the app in that Google account (myaccount.google.com → Security → third-party apps). Google refresh tokens do not expire on their own, so revoking is what ends the access.
`julia` works the same way.

Each failure line in the alert email also names the **original email** (subject, sender, date, mailbox and a Gmail link; the link opens in the right account via `authuser=`).

### 2.7 Notifications and the ledger
- **Failure** (undecryptable file, missing folder id, OneDrive error): one email per run listing every failed file; the run is marked failed.
- **Manual scan**: always one email with the full result (`3 filed, 14 skipped`, each file and its folder).
- **Normal hourly runs**: silent unless something failed.
- **Monthly**: the summary workflow, 1st of the month. "Filed in a month" means first filed by the workflow in that
  month (UTC), not the statement's own date; a re-upload of the same file name is not counted again. The ledger starts
  when it was introduced, so the first manual scan counts every file in its window as new.
- The ledger is `bank-automation/state/filed-log.json` (git-ignored with the rest of `state/`; back it up with the config).

## 3. Passwords

- Stored in `bank-automation/statement-passwords.json`: `{ "<account key>": "password" }`, where the value
  can also be a **list** `["first", "second"]` (tried in order) for periods with a wrong password on file,
  rotation, or one account covering several family members (Discovery uses each member's ID number).
- Mounted into the sidecar only (`BANK_PASSWORDS_FILE`, default
  `../../bank-automation/statement-passwords.json`); git-ignored; **must exist before `docker compose up`**
  or Docker creates a directory with that name.
- Edits apply immediately (re-read on every request).
- Claude Code is denied from reading/editing it (`~/.claude/settings.json`). That blocks the obvious routes
  only (the Read/Edit tools and common shell commands naming the file), not a determined one. For a hard
  guarantee use the sandbox filesystem deny or move the secrets to 1Password (not done yet).
- `bank-automation/scripts/split-passwords.py` moves legacy `statements.decryption_password` values out of the
  main config (it prints counts only). `bank-automation/scripts/find-eib-password.py` recovers a
  `NAME4 + 4-digit` password when the bank holds a wrong date of birth; **run it in your own terminal, not
  through Claude Code**, so the result stays private.

## 4. Deploying and operating

### Services
`cd n8n && docker compose up -d --build pdf-decrypt` rebuilds/redeploys just the sidecar (do this after
editing `pdf-decrypt/server.py`). The sidecar has one writable mount, `BANK_STATE_DIR` (default
`../../bank-automation/state`, mounted at `/state`); create it first (`mkdir`, owned by uid 3000/`pvh`) or Docker makes it as root. Config and passwords are read live; no restart needed for edits. The
config is a single-file bind mount: edit it in place (an editor that replaces the file leaves the
container looking at the old inode → `docker compose restart pdf-decrypt`).

### Changing the workflow code
1. Edit `n8n/workflows/src/match-statements.js` / `fetch-linked-pdfs.js`.
2. `python3 n8n/tools/build-workflow.py` embeds them into the workflow JSON
   (`--check` verifies they match, `--extract` goes the other way).
3. Import:
   ```
   docker exec -i n8n sh -c 'cat > ~/.n8n/wf.json' < n8n/workflows/bank-statements.json \
     && docker exec n8n n8n import:workflow --input=/home/node/.n8n/wf.json
   ```
4. **Import deactivates the workflow.** In n8n 2.x re-activate with **Publish** (top right of the editor).
   (CLI alternative: `n8n update:workflow --id=BankStmtGmail001 --active=true` + restart n8n, which also
   restarts the other active workflows.)
5. **Restart n8n after Publish** (`docker restart n8n`). Without it, polling triggers registered by earlier
   imports and publishes can stay alive in the running process and fire with old copies of the workflow, all at
   once, on the same emails (seen on 23 Oct 2026: four overlapping runs, three of them old versions, failing with
   connection resets and filing files under the wrong names). Check afterwards that each run's snapshot is the
   version you published.

### Running it
- **Hourly**: published workflow, Gmail Trigger.
- **On demand**: open the workflow → **Execute workflow** (starts at Manual Scan). Re-running is safe: files that
  are already in OneDrive are skipped (2.8), nothing is overwritten. If naming rules changed since the first upload,
  the files are filed again under the new names and the old-named copies stay next to them.
- **Bulk runs** (a rescan after removing labels, or a first run over a large mailbox) send ~100 items through every
  step at once, which used to fail with `write ECONNRESET` (sidecar), "too many requests" (Gmail) and "connection
  aborted" (Graph) even though each node retried. So the HTTP nodes are paced (Options → Batching: Decrypt PDF 8 per
  0.5 s, Check Existing 5 and Upload 3 per second, Mark/Trash 10 per second), the Mark nodes retry 5× with a 15 s
  wait, and the sidecar has a 256-connection accept queue (`LISTEN_BACKLOG`) and at most 4 concurrent qpdf runs
  (`QPDF_CONCURRENCY`). A big run is slower but should complete. Do not start a second run while one is going: both
  share the Gmail quota.

### Credentials that expire
| Credential | Symptom | Cause / fix |
|---|---|---|
| *Gmail - statements* | trigger error `invalid_grant` | Google refresh token expired. Reconnect in n8n. If the Google Cloud OAuth app is in *Testing*, tokens last 7 days: set it to *In production* (or *Internal* on Workspace). |
| *Onedrive - Pete* | `AADSTS7000222` client secret expired | New client secret on the Entra app registration (personal-only app; sign in at entra.microsoft.com in a **private window** if you get `state_mismatch`), paste the **Value** into the credential, reconnect. Max lifetime 24 months; set a reminder. |

### Adding a sender / account
1. Get one real example and read the sender, subject and the attachment name **as n8n sees it** (section 6).
2. Add the account to `private-config.json` (`bank-automation/docs/statements-config.md`) with a `match` rule,
   a folder, and (YNAB) the budget/account/payee IDs.
3. If PDFs are protected, add a password entry for its key in `statement-passwords.json` *first* (a missing one fails the run).
4. Make sure mail reaches `statements@vanheerden.ch` (or is auto-forwarded in a way that keeps the sender).
5. Run Manual Scan and check with `last-run.js`.

## 5. Mail delivery
Everything must end up in `statements@vanheerden.ch`, the mailbox the credential reads. Banks and agents
that mail another address need that address changed, or a Gmail filter that forwards (which keeps the
original sender). Plus-addresses (`statements+rob@`, `statements+julia@`, `statements+remax@`) are
supported through the `to` rule; some banks reject `+` in an email field.

## 6. Troubleshooting

**See what a run did** (metadata only: statuses, matches, file names, OneDrive confirmations, failures,
unmatched PDFs):

```
docker exec -i n8n node - < n8n/tools/last-run.js        # latest run
docker exec -i n8n node - 357 < n8n/tools/last-run.js    # a given execution id
```

| Symptom | Likely cause |
|---|---|
| Statement not filed, listed under "NOT MATCHED" | No rule for that sender, or the rule's `filename` doesn't match what n8n reports (note the `/` truncation). |
| Forwarded mail not matched | Sender not in `TRUSTED_FORWARDERS`, or Google's authentication header didn't show an aligned pass, or the mail has no `Authentication-Results` (forwards must arrive through Gmail). |
| Link statement not fetched | Account lacks `link.enabled`, link host not in `allow_hosts`, or the response wasn't a PDF (the error lists each attempt). |
| Alert email: `could not be decrypted: this PDF needs a password but none is set…` | The account has no entry in `statement-passwords.json`. |
| Alert email: `could not be decrypted … invalid password (tried N)` | Missing/wrong password for that account. Only that file is skipped; fix `statement-passwords.json`, then Manual Scan. |
| Savings (or other `by_content`) files not filed; sidecar log says `identify … -> nobody` | The regex is not found in the PDF's text (the name is printed differently, or the PDF's text cannot be read), or it is found for both accounts. |
| Zero matches although emails arrive | Check the sidecar is up (`docker ps`) — Match fetches `/accounts` itself and fails loudly if it can't. |
| `PUT` to OneDrive fails | OneDrive credential (secret expired / needs reconnect). A 404 on a folder id: see 2.6. |
| Sidecar can't read config | The bind mount points at a replaced file → restart `pdf-decrypt`. |

**Traps that cost time — read before changing code:**
- n8n's Code node sandbox has **no `URL` class** (and `new URL()` failures were silently caught, which made
  every link look disallowed). Parse hosts with a regular expression. Test code in the real sandbox, not only in plain Node.
- Don't look up other nodes by name with `$('Node')` for data that may not exist in a run; pass data as input.
- Run state from the editor's manual runs is saved (`execution_entity`), so `last-run.js` works for them too.
- Testing a code change in n8n without touching the real workflow: import a throwaway workflow with a Code
  node that emits sample emails → the Match code → a summary node, then
  `docker exec -e N8N_RUNNERS_BROKER_PORT=5690 n8n n8n execute --id=<id> --rawOutput` (the CLI needs a free
  task-broker port because the server holds 5679). There is no CLI delete, so remove throwaway workflows in the UI.
  One such workflow, "TEMP fetch-test (safe to delete)", currently exists and can be deleted.

## 7. Security notes
- Passwords are only ever in `statement-passwords.json` and the sidecar's memory; the response to `/accounts`
  never includes them.
- The workflow only follows links on a per-account host allowlist, accepts only a real PDF, and caps size.
- A trusted forwarder must pass Google's authentication (2.2). Strengthening the domain would help further:
  turn on DKIM for `vanheerden.ch` in the Workspace admin console (outgoing mail is currently signed with
  Google's fallback domain), add a DMARC `rua=` address, then move `p=none` → `quarantine`; review the SPF
  includes (Microsoft 365, Mailchimp) and consider `-all`.
- Real statements and the real config never go to git. In bank-automation `*.pdf`,
  `private-config*.json` (except the template) and the passwords file are ignored.

## 8. Known limitations
- Unmatched mail is skipped without a notification.
- Only the three kinds of Discovery mail seen so far are configured; new senders need a rule.
- Statement contents are not parsed yet (needed for the YNAB import and the medical-claims web app).
- Wio current/savings accounts, FNB/Nedbank savings, ENBD Fi and ABSA have no rules yet (no mail seen).
