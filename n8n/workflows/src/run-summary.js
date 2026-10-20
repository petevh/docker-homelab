// Runs once, after everything else: files filed (Record Folder), files skipped because they were
// already in OneDrive, upload and decrypt failures, and files that clash with a different file of
// the same name all arrive here through the Collect Results merge, which waits for every branch.
//
// An email is sent when something failed (always) or when this was a manual scan (so the full
// result of a scan you started is in your inbox). A scheduled/hourly run where everything went
// fine stays silent; the files it filed show up in the monthly summary. Nothing to report
// (e.g. a scan that matched no statements) stops here.
const rows = $input.all().map((i) => i.json);
const byName = (a, b) => (a.account + a.fileName).localeCompare(b.account + b.fileName);
const filed = rows.filter((j) => j.filed).sort(byName);
const skipped = rows.filter((j) => j.skipped).sort(byName);
const failures = rows.filter((j) => j.failure && j.line);
const manual = ['Manual Scan', 'Manual Scan Pete', 'Manual Scan Julia'].some((name) => $(name).isExecuted);
const marked = $('Mail Outcome').isExecuted ? $('Mail Outcome').all().map((i) => i.json) : [];
if (failures.length === 0 && !manual) return [];

const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;
const head =
  (manual ? 'Manual scan finished: ' : 'Statement run finished: ') +
  `${plural(filed.length, 'file')} filed, ${skipped.length} skipped (already in OneDrive)` +
  (failures.length ? `, ${failures.length} NOT filed.` : '.');

const parts = [head];
if (failures.length) parts.push('NOT FILED\n\n' + failures.map((j) => j.line).join('\n\n'));
if (marked.length) {
  const ok = marked.filter((m) => m.outcome === 'filed');
  const purged = ok.filter((m) => m.purge).length;
  parts.push(
    `Emails: ${ok.length} labelled Statements/Filed` + (purged ? ` (${purged} of them moved to Trash: purge_email is set)` : '') +
      `, ${marked.length - ok.length} labelled Statements/Failed.`,
  );
}
if (filed.length) parts.push('FILED\n' + filed.map((j) => `- ${j.account}: ${j.fileName}\n    -> ${j.folder}`).join('\n'));
if (skipped.length) {
  parts.push('SKIPPED (identical file already in the folder)\n' + skipped.map((j) => `- ${j.account}: ${j.fileName}`).join('\n'));
}
if (filed.some((j) => j.ledgerError)) parts.push('Note: the filed-statements ledger could not be updated, so the monthly summary will miss these files.');

const subject = failures.length
  ? `Bank statements: ${plural(failures.length, 'file')} not filed`
  : `Bank statements: manual scan, ${filed.length} filed, ${skipped.length} skipped`;

return [
  {
    json: {
      subject,
      body: parts.join('\n\n'),
      failed: failures.length,
      failureText: failures.map((j) => j.line).join('\n\n'),
    },
  },
];
