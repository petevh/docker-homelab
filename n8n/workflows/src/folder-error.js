// Upload failures, one input item per failed file (the error output of Upload to OneDrive).
// Turned into one failure line each; Failure Summary collects them with the decrypt failures,
// so a moved folder produces one mail and one failure at the end of the run, not one per file.
//
// 404/410 on a folder id (from the config or recorded earlier) means the folder is gone:
// never silently recreate the path, tell the user which account, id and path. Other errors
// (401/403 access, 429/5xx service) are reported as such; no recorded id is ever cleared
// because of them.
const lines = [];
const items = $input.all();
for (let i = 0; i < items.length; i++) {
  const ctx = $('Fetch Linked PDFs').itemMatching(i).json;
  const err = items[i].json.error || items[i].json;
  // the HTTP node does not always carry a status code, so fall back to the message text
  const status =
    Number(err.httpCode) ||
    Number((String(err.message).match(/\b([45]\d{2})\b/) || [])[1]) ||
    (/could not be found|not found/i.test(String(err.message)) ? 404 : 0);
  const gone = ctx.folderSource !== 'path' && (status === 404 || status === 410);
  if (gone) {
    lines.push(
      `${ctx.account}: "${ctx.fileName}" was not filed. The ${ctx.folderSource === 'config' ? 'configured folder_id' : 'recorded folder id'} ` +
        `${ctx.folderId} no longer exists (HTTP ${status}). Configured path: ${ctx.folder}. ` +
        `Last known location: ${ctx.recordedPath || 'unknown'}. ` +
        `Fix: set statements.folder_id to the folder's current id, or delete this account's entry in state/statement-folder-ids.json ` +
        `to look the path up again, then run Manual Scan (re-uploads replace same-named files).`,
    );
  } else if (status === 409) {
    // the pre-check could not see the file, but the name exists: Upload never overwrites
    lines.push(
      `${ctx.account}: "${ctx.fileName}" was not filed because a file with that name already exists in the folder ` +
        `and could not be compared beforehand. It was left alone; if the new one is the right one, delete or rename the old file and run Manual Scan.`,
    );
  } else if (!status && /timed? ?out|abort|econn|etimedout|enotfound|socket|network|offline/i.test(String(err.message))) {
    // no HTTP answer at all: the connection to OneDrive dropped or timed out (already retried by the node)
    lines.push(
      `${ctx.account}: "${ctx.fileName}" was not filed. The connection to OneDrive failed (${err.message}). ` +
        `This is usually a temporary network or OneDrive problem, not a missing folder: run Manual Scan again, ` +
        `and look closer only if the same file fails twice.`,
    );
  } else if (status === 429 || status >= 500) {
    lines.push(
      `${ctx.account}: "${ctx.fileName}" was not filed. OneDrive returned ${status} (${folderWhere(ctx)}): ${err.message || JSON.stringify(err)}. ` +
        `OneDrive is throttling or having trouble, which is usually temporary: run Manual Scan again.`,
    );
  } else {
    lines.push(
      `${ctx.account}: "${ctx.fileName}" was not filed. OneDrive returned ${status || 'an error'} (${folderWhere(ctx)}): ${err.message || JSON.stringify(err)}. ` +
        `This is an access problem, not a missing folder. Check the OneDrive credential and the folder's sharing, then run Manual Scan.`,
    );
  }
}
function folderWhere(ctx) {
  return ctx.folderSource === 'path' ? `by path ${ctx.folder}` : `folder id ${ctx.folderId}`;
}
return lines.map((line, i) => {
  const ctx = $('Fetch Linked PDFs').itemMatching(i).json;
  return { json: { failure: 'folder', line: `${line}\n  Email: ${ctx.emailInfo}`, messageId: ctx.messageId, mailbox: ctx.mailbox }, pairedItem: { item: i } };
});
