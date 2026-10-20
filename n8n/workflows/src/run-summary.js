// Runs once, after everything else: successful uploads (Record Folder), upload failures and
// decrypt failures all arrive here through the Collect Results merge, which waits for every branch.
//
// An email is sent when something failed (always) or when this was a manual scan (so the full
// result of a scan you started is in your inbox). A scheduled/hourly run where everything
// went fine stays silent; those files show up in the monthly summary instead. Nothing to
// report (e.g. a scan that matched no statements) stops here.
const rows = $input.all().map((i) => i.json);
const filed = rows.filter((j) => j.filed).sort((a, b) => (a.account + a.fileName).localeCompare(b.account + b.fileName));
const failures = rows.filter((j) => j.failure && j.line);
const manual = $('Manual Scan').isExecuted;
if (failures.length === 0 && !manual) return [];

const newCount = filed.filter((j) => j.isNew).length;
const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;
const head =
  (manual ? 'Manual scan finished: ' : 'Statement run finished: ') +
  `${plural(filed.length, 'file')} filed (${newCount} new, ${filed.length - newCount} already filed earlier and replaced)` +
  (failures.length ? `, ${failures.length} NOT filed.` : '.');

const parts = [head];
if (failures.length) parts.push('NOT FILED\n\n' + failures.map((j) => j.line).join('\n\n'));
if (filed.length) {
  parts.push(
    'FILED\n' + filed.map((j) => `- ${j.account}: ${j.fileName}\n    -> ${j.folder}${j.isNew ? '  (new)' : ''}`).join('\n'),
  );
}
if (filed.some((j) => j.ledgerError)) parts.push('Note: the filed-statements ledger could not be updated, so the monthly summary will miss these files.');

const subject = failures.length
  ? `Bank statements: ${plural(failures.length, 'file')} not filed`
  : `Bank statements: manual scan, ${plural(filed.length, 'file')} filed`;

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
