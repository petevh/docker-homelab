// Before filing: the exact size of each decrypted PDF, and the request that says whether a file with
// the target name is already in OneDrive (Check Existing sends it to Graph). One $batch call asks
// about both the target name and its " (2)" variant, which Decide uses when a different file
// already holds the target name. The binary is passed on, because the HTTP node that follows
// drops it.
const SELECT = '?$select=id,name,size,file,folder,parentReference';
const batchFor = (uploadUrl) => {
  const path = uploadUrl.replace(/:\/content$/, '').replace(/^https:\/\/graph\.microsoft\.com\/v1\.0/, '');
  return {
    requests: [
      { id: '1', method: 'GET', url: path + SELECT },
      { id: '2', method: 'GET', url: path.replace(/(\.pdf)$/i, '%20(2)$1') + SELECT },
    ],
  };
};
const items = $input.all();
const out = [];
for (let i = 0; i < items.length; i++) {
  const bytes = await this.helpers.getBinaryDataBuffer(i, 'data');
  const ctx = $('Fetch Linked PDFs').itemMatching(i).json;
  out.push({
    json: { decryptedSize: bytes.length, batchBody: batchFor(ctx.uploadUrl) },
    binary: items[i].binary,
    pairedItem: { item: i },
  });
}
return out;
