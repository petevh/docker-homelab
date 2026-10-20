// Upload failures, one input item per failed file (the error output of Upload to OneDrive).
// Collapsed into a single item with the text of one alert email, so a moved folder produces
// one mail and one failure at the end of the run instead of one per file.
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
  const status = Number(err.httpCode) || Number((String(err.message).match(/\b([45]\d{2})\b/) || [])[1]) || 0;
  const gone = ctx.folderSource !== 'path' && (status === 404 || status === 410);
  if (gone) {
    lines.push(
      `${ctx.account}: "${ctx.fileName}" was not filed. The ${ctx.folderSource === 'config' ? 'configured folder_id' : 'recorded folder id'} ` +
        `${ctx.folderId} no longer exists (HTTP ${status}). Configured path: ${ctx.folder}. ` +
        `Last known location: ${ctx.recordedPath || 'unknown'}. ` +
        `Fix: set statements.folder_id to the folder's current id, or delete this account's entry in state/statement-folder-ids.json ` +
        `to look the path up again, then run Manual Scan (re-uploads replace same-named files).`,
    );
  } else {
    lines.push(
      `${ctx.account}: "${ctx.fileName}" was not filed. OneDrive returned ${status || 'an error'} (${folderWhere(ctx)}): ${err.message || JSON.stringify(err)}. ` +
        `This is an access or service problem, not a missing folder. Run Manual Scan once it is fixed.`,
    );
  }
}
function folderWhere(ctx) {
  return ctx.folderSource === 'path' ? `by path ${ctx.folder}` : `folder id ${ctx.folderId}`;
}
const count = lines.length;
return [
  {
    json: {
      subject: `Bank statements: ${count} file${count === 1 ? '' : 's'} not filed`,
      body: lines.join('\n\n'),
      count,
    },
  },
];
