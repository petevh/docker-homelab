// After a successful upload: remember which OneDrive folder the file landed in, so later
// uploads address it by id and survive the folder being moved or renamed. Graph's reply
// to the PUT names the parent folder (id, drive, current path).
//
// Records when the upload went by path (first run, or onedrive_folder was changed) and
// updates the last-seen path when a recorded folder has moved. A folder id set in the
// config is never touched. Failing to record is not fatal: the file is already filed and
// the next run simply looks the folder up by path again.
const seenPathOf = (ref) => {
  const m = String(ref.path || '').match(/root:(.*)$/);
  return m ? m[1].replace(/^\/+|\/+$/g, '') : '';
};

const out = [];
const items = $input.all();
for (let i = 0; i < items.length; i++) {
  const ctx = $('Fetch Linked PDFs').itemMatching(i).json;
  const ref = items[i].json.parentReference || {};
  const seenPath = seenPathOf(ref);
  const result = { account: ctx.account, fileName: ctx.fileName, folderSource: ctx.folderSource, recorded: false };

  const moved = ctx.folderSource === 'recorded' && seenPath && seenPath !== ctx.recordedPath;
  if (moved) result.movedTo = seenPath;
  if ((ctx.folderSource === 'path' || moved) && ref.id) {
    try {
      await this.helpers.httpRequest({
        method: 'POST',
        url: `http://pdf-decrypt:8080/folder-id?account=${encodeURIComponent(ctx.account)}`,
        body: ctx.folderSource === 'path'
          ? { folder_id: ref.id, drive_id: ref.driveId || '', seen_path: seenPath }
          : { folder_id: ctx.folderId, drive_id: ctx.driveId, seen_path: seenPath },
        json: true,
      });
      result.recorded = true;
    } catch (e) {
      result.recordError = e.message;
    }
  }
  out.push({ json: result, pairedItem: { item: i } });
}
return out;
