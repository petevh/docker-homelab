// Monthly summary of the statements the workflow filed, from the sidecar's ledger.
// Runs on the 1st (Schedule Trigger) for the month that just ended, or by hand (Manual Trigger).
// "Filed in the month" means first filed by the workflow in that month (UTC), which is not the
// statement's own date; re-uploads of an already filed name are not counted again.
const now = new Date();
const prev = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() - 1, 1));
const month = `${prev.getUTCFullYear()}-${String(prev.getUTCMonth() + 1).padStart(2, '0')}`;
const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];
const label = `${MONTHS[prev.getUTCMonth()]} ${prev.getUTCFullYear()}`;

const res = await this.helpers.httpRequest({ method: 'GET', url: `http://pdf-decrypt:8080/filed?month=${month}`, json: true });

const byAccount = {};
for (const f of res.filed) (byAccount[f.account] = byAccount[f.account] || []).push(f);
const nameOf = (key) => (res.accounts[key] && res.accounts[key].name) || key;

const lines = Object.entries(byAccount).map(([key, files]) =>
  `${nameOf(key)} (${files.length})\n` + files.map((f) => `  - ${f.file_name}\n      -> ${f.folder}`).join('\n'),
);
const missing = Object.keys(res.accounts).filter((k) => !byAccount[k]).map(nameOf).sort();

const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;
const parts = [`Statements filed in ${label}: ${plural(res.filed.length, 'file')} for ${plural(Object.keys(byAccount).length, 'account')}.`];
if (lines.length) parts.push(lines.join('\n\n'));
if (missing.length) {
  parts.push(
    `No new statement filed for ${plural(missing.length, 'account')}:\n` + missing.map((n) => `  - ${n}`).join('\n') +
      '\n(Some accounts are not monthly, or send no statements by email; check the ones you expect.)',
  );
}
return [{ json: { subject: `Bank statements: ${label}, ${plural(res.filed.length, 'file')} filed`, body: parts.join('\n\n'), month } }];
