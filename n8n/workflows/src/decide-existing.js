// Per file, after Check Existing asked Graph about the target name and its " (2)" variant (one
// $batch call, answers 1 and 2):
//   404                         -> not there yet: upload it (Upload uses conflictBehavior=fail, so it
//                                  can never overwrite anything)
//   200, same size              -> already filed: skip (listed as skipped in the summary)
//   200, different size         -> a DIFFERENT file with this name exists. It is never overwritten; the
//                                  new file is filed as "name (2).pdf" instead, judged the same way:
//                                    (2) not there            -> upload it as "name (2).pdf"
//                                    (2) there, same size     -> already filed: skip
//                                    (2) there, other size    -> report it as a failure so you decide
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
  const answers = {};
  for (const r of (res.body && Array.isArray(res.body.responses) ? res.body.responses : [])) answers[String(r.id)] = r;
  const first = answers['1'] || {};
  const status = Number(first.status) || 0;
  const body = first.body && typeof first.body === 'object' ? first.body : {};
  const decryptedSize = m.json.decryptedSize;
  const base = { ...ctx, decryptedSize };
  const paired = { item: i };

  if (status === 200 && body.file) {
    if (body.size === decryptedSize) {
      out.push({ json: { ...base, action: 'skip', skipped: true, existingSize: body.size }, binary: m.binary, pairedItem: paired });
    } else {
      // A different file holds the name: file this one as "name (2).pdf".
      const second = answers['2'] || {};
      const vStatus = Number(second.status) || 0;
      const vBody = second.body && typeof second.body === 'object' ? second.body : {};
      const fileName = ctx.fileName.replace(/\.pdf$/i, ' (2).pdf');
      const uploadUrl = ctx.uploadUrl.replace(/(\.pdf)(:\/content)$/i, '%20(2)$1$2');
      const variant = { ...base, fileName, uploadUrl, variant: 2, originalSize: body.size };
      if (vStatus === 200 && vBody.file) {
        if (vBody.size === decryptedSize) {
          out.push({ json: { ...variant, action: 'skip', skipped: true, existingSize: vBody.size }, binary: m.binary, pairedItem: paired });
        } else {
          out.push({
            json: {
              ...base,
              action: 'conflict',
              failure: 'exists-different',
              line:
                `${ctx.account}: "${ctx.fileName}" and "${fileName}" both already exist in OneDrive (${ctx.folder}) with different content ` +
                `(${body.size} and ${vBody.size} bytes; the new file is ${decryptedSize} bytes), so the new file was NOT filed and the existing ones were left alone. ` +
                `Check the files in OneDrive, delete or rename the wrong one, then run Manual Scan.\n  Email: ${ctx.emailInfo}`,
            },
            pairedItem: paired,
          });
        }
      } else {
        // 404: free, upload it. Anything else: could not tell, upload anyway (refused if it exists).
        out.push({ json: { ...variant, action: vStatus === 404 ? 'new' : 'unchecked' }, binary: m.binary, pairedItem: paired });
      }
    }
  } else {
    out.push({ json: { ...base, action: status === 404 ? 'new' : 'unchecked' }, binary: m.binary, pairedItem: paired });
  }
}
return out;
