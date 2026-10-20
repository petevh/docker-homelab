// Before filing: the exact size of each decrypted PDF, and the URL that says whether a file with
// the target name is already in OneDrive (Check Existing asks Graph for it). The binary is passed
// on, because the HTTP node that follows drops it.
const items = $input.all();
const out = [];
for (let i = 0; i < items.length; i++) {
  const bytes = await this.helpers.getBinaryDataBuffer(i, 'data');
  const ctx = $('Fetch Linked PDFs').itemMatching(i).json;
  out.push({
    json: { decryptedSize: bytes.length, checkUrl: ctx.uploadUrl.replace(/:\/content$/, '') },
    binary: items[i].binary,
    pairedItem: { item: i },
  });
}
return out;
