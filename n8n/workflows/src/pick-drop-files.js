// Drop-folder decrypt: choose which files in the OneDrive drop folder to process this run.
// Input: the Graph "list children" response of the drop folder. Output: one item per PDF.
// Skipped: subfolders, non-PDFs (left alone), empty files, and files changed in the last
// 60 s (OneDrive may still be syncing them; the next poll picks them up).
// The download URL is NOT in the listing: the next node (Get Link) fetches it per file.
const MIN_AGE_MS = 60 * 1000;
const drop = $('Config').first().json.drop;
const children = $input.first().json.value || [];
const now = Date.now();

const out = [];
for (const c of children) {
  if (!c.file || !/\.pdf$/i.test(c.name || '')) continue;
  if (!c.size) continue;
  if (now - Date.parse(c.lastModifiedDateTime) < MIN_AGE_MS) continue;
  out.push({ json: { id: c.id, name: c.name, size: c.size, drop } });
}
return out;
