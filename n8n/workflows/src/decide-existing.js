// Per file, after Check Existing asked Graph about the target name:
//   404                         -> not there yet: upload it (Upload uses conflictBehavior=fail, so it
//                                  can never overwrite anything)
//   200, same size              -> already filed: skip (listed as skipped in the summary)
//   200, different size         -> a DIFFERENT file with this name exists: do not overwrite, do not
//                                  guess; report it as a failure so you decide (delete or rename the
//                                  old one, then Manual Scan)
//   anything else / no answer   -> could not check: try the upload anyway; if the name exists after
//                                  all, Graph refuses (409) and that is reported
// Size is compared because the Code node cannot hash files; the same statement decrypts to the same
// bytes, so an identical statement has an identical size.
const items = $input.all();
const out = [];
for (let i = 0; i < items.length; i++) {
  const m = $('Measure').itemMatching(i);
  const ctx = $('Fetch Linked PDFs').itemMatching(i).json;
  const res = items[i].json;
  const status = Number(res.statusCode) || 0;
  const body = res.body && typeof res.body === 'object' ? res.body : {};
  const decryptedSize = m.json.decryptedSize;
  const base = { ...ctx, decryptedSize };
  const paired = { item: i };

  if (status === 200 && body.file) {
    if (body.size === decryptedSize) {
      out.push({ json: { ...base, action: 'skip', skipped: true, existingSize: body.size }, binary: m.binary, pairedItem: paired });
    } else {
      out.push({
        json: {
          ...base,
          action: 'conflict',
          failure: 'exists-different',
          line:
            `${ctx.account}: "${ctx.fileName}" already exists in OneDrive (${ctx.folder}) with different content ` +
            `(existing ${body.size} bytes, new ${decryptedSize} bytes), so the new file was NOT filed and the existing one was left alone. ` +
            `If the new one is the right one, delete or rename the old file in OneDrive and run Manual Scan.\n  Email: ${ctx.emailInfo}`,
        },
        pairedItem: paired,
      });
    }
  } else {
    out.push({ json: { ...base, action: status === 404 ? 'new' : 'unchecked' }, binary: m.binary, pairedItem: paired });
  }
}
return out;
