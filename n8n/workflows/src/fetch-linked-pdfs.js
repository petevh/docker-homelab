// For statements that arrive as a download link: fetch the PDF so the rest of the
// chain (decrypt -> OneDrive) sees it like an attachment. Other items pass through.
// Safety: Match only passes links on the account's allowed hosts; here a download
// is accepted only if it really is a PDF and within the size limit. A link email
// that yields no PDF fails the run loudly instead of being skipped.
const MAX_BYTES = 50 * 1024 * 1024;
// (The Code node sandbox has no URL class, so the host is read with a pattern.)
const hostOf = (u) => {
  const m = String(u).match(/^https?:\/\/(?:[^\/?#@]*@)?([^\/?#:]+)/i);
  return m ? m[1] : '?';
};

const out = [];
const items = $input.all();
for (let i = 0; i < items.length; i++) {
  const item = items[i];
  if (!item.json.viaLink) {
    out.push({ json: item.json, binary: item.binary, pairedItem: { item: i } });
    continue;
  }

  let pdf = null;
  const tried = [];
  for (const url of item.json.linkCandidates) {
    try {
      const res = await this.helpers.httpRequest({
        method: 'GET',
        url,
        encoding: 'arraybuffer',
        returnFullResponse: true,
        ignoreHttpStatusCode: true,
        timeout: 30000,
        headers: { 'User-Agent': 'Mozilla/5.0' },
      });
      const buf = Buffer.from(res.body);
      if (res.statusCode === 200 && buf.length <= MAX_BYTES && buf.subarray(0, 5).toString() === '%PDF-') {
        pdf = buf;
        break;
      }
      tried.push(`${hostOf(url)} -> HTTP ${res.statusCode}, not a usable PDF`);
    } catch (e) {
      tried.push(`${hostOf(url)} -> ${e.message}`);
    }
  }
  if (!pdf) {
    throw new Error(`No PDF found behind any link in "${item.json.subject}" for ${item.json.account} (tried: ${tried.join('; ')})`);
  }
  const binary = await this.helpers.prepareBinaryData(pdf, item.json.fileName, 'application/pdf');
  out.push({ json: item.json, binary: { data: binary }, pairedItem: { item: i } });
}
return out;
