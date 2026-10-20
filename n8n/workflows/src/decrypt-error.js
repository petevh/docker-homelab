// Decrypt failures, one input item per failed file (the error output of Decrypt PDF).
// Turned into one failure line each; Failure Summary collects them with the upload
// failures and sends a single alert, so one bad password no longer stops the rest of the run.
// The sidecar's message names the account and says how many passwords it tried; it never
// contains a password.
const out = [];
const items = $input.all();
for (let i = 0; i < items.length; i++) {
  const ctx = $('Fetch Linked PDFs').itemMatching(i).json;
  const err = items[i].json.error || items[i].json;
  const why = err.description || err.message || JSON.stringify(err);
  out.push({
    json: {
      failure: 'decrypt',
      messageId: ctx.messageId,
      mailbox: ctx.mailbox,
      line:
        `${ctx.account}: "${ctx.fileName}" could not be decrypted and was not filed: ${why}. ` +
        `Check this account's entry in statement-passwords.json (missing, wrong or changed password), ` +
        `then run Manual Scan (re-uploads replace same-named files).`,
    },
    pairedItem: { item: i },
  });
}
return out;
