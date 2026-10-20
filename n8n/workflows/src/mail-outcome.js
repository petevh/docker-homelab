// One item per email that was handled, with how it ended, so the mailbox it came from can be
// marked (and, where the account asks for it, cleaned up). All the rows of the run arrive here:
// filed, skipped (already in OneDrive), and every kind of failure.
//   outcome 'filed'  = every PDF of the email we handled is in OneDrive (uploaded now or already there)
//   outcome 'failed' = at least one did not make it (decrypt, upload, a clashing file)
//   purge            = filed AND every account involved has statements.purge_email AND the email
//                      holds no PDF that matched no account (it may be a receipt or something
//                      else; such an email is labelled, never deleted automatically)
const groups = new Map();
for (const row of $input.all().map((i) => i.json)) {
  if (!row.messageId) continue;
  const mailbox = row.mailbox || 'statements';
  const key = `${mailbox}|${row.messageId}`;
  const g = groups.get(key) || { mailbox, messageId: row.messageId, failed: false, purge: true };
  if (row.failure) g.failed = true;
  if (!row.purge || row.hasUnmatchedPdf) g.purge = false;
  groups.set(key, g);
}
return [...groups.values()].map((g) => ({
  json: { mailbox: g.mailbox, messageId: g.messageId, outcome: g.failed ? 'failed' : 'filed', purge: !g.failed && g.purge },
}));
