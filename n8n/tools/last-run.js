// Summarise a run of the bank-statements workflow from n8n's own database (read-only).
// Prints node statuses, which statements matched which account, what OneDrive confirmed,
// decrypt failures, and PDFs that matched no account. Metadata only: subjects, file
// names, sizes -- never email bodies, PDF contents or passwords.
//
//   docker exec -i n8n node - < n8n/tools/last-run.js          # latest run
//   docker exec -i n8n node - 357 < n8n/tools/last-run.js      # a specific execution id
//
// Runs inside the n8n container (it uses n8n's bundled sqlite3 and flatted).
const base = '/usr/local/lib/node_modules/n8n';
const req = (m) => require(require.resolve(m, { paths: [base] }));
const sqlite3 = req('sqlite3');
const flatted = req('flatted');
const WORKFLOW = 'BankStmtGmail001';
const wanted = process.argv[2];

const db = new sqlite3.Database('/home/node/.n8n/database.sqlite', sqlite3.OPEN_READONLY);
const sql = wanted
  ? ['select id,status,mode,startedAt from execution_entity where id=?', [wanted]]
  : ['select id,status,mode,startedAt from execution_entity where workflowId=? order by id desc limit 1', [WORKFLOW]];

db.get(sql[0], sql[1], (e, ex) => {
  if (e || !ex) return console.log('no execution found', e ? e.message : '');
  console.log(`execution ${ex.id}: ${ex.mode}, ${ex.status}, started ${ex.startedAt} (UTC)`);
  db.get('select data from execution_data where executionId=?', [ex.id], (e2, d) => {
    if (e2 || !d) return console.log('no stored data for this execution', e2 ? e2.message : '');
    const rd = flatted.parse(d.data).resultData;
    if (rd.error) console.log('RUN ERROR:', String(rd.error.message).slice(0, 400));

    console.log('\nNODES');
    for (const [name, runs] of Object.entries(rd.runData)) {
      const run = runs[0];
      const outs = (run.data && run.data.main ? run.data.main : []).map((a) => (a ? a.length : 0));
      console.log(`  ${name.padEnd(20)} ${String(run.executionStatus).padEnd(8)} out=${JSON.stringify(outs)}${run.error ? '  ERROR: ' + String(run.error.message).slice(0, 200) : ''}`);
    }

    const out = (n, i = 0) => ((rd.runData[n] || [{ data: { main: [[]] } }])[0].data.main[i]) || [];
    const source = out('Scan Inbox').length ? out('Scan Inbox') : out('Gmail Trigger');
    const matched = out('Match Statements');

    console.log(`\nMATCHED (${matched.length} of ${source.length} emails)`);
    matched.forEach((i) => console.log(`  ${i.json.account.padEnd(32)} ${i.json.viaLink ? '[link] ' : ''}${i.json.fileName}`));

    const uploads = out('Upload to OneDrive');
    console.log(`\nONEDRIVE CONFIRMED (${uploads.length})`);
    uploads.forEach((i) => console.log(`  ${String(i.json.size).padStart(8)} B  ${(i.json.parentReference && i.json.parentReference.path || '').replace('/drive/root:', '')}/${i.json.name}`));

    const failed = out('Decrypt PDF', 1);
    if (failed.length) {
      console.log(`\nDECRYPT FAILED (${failed.length})`);
      failed.forEach((i) => console.log(`  ${i.json.account}  ${i.json.fileName}  ${String((i.json.error && i.json.error.message) || '').slice(0, 120)}`));
    }

    const used = new Set(matched.map((i) => i.pairedItem && i.pairedItem.item));
    const unmatched = [];
    source.forEach((it, idx) => {
      if (used.has(idx)) return;
      const pdfs = Object.values(it.binary || {}).map((b) => b.fileName).filter((f) => /\.pdf$/i.test(f || ''));
      if (pdfs.length || /Owner Statement \[/.test(it.json.subject || '')) unmatched.push(`${JSON.stringify(it.json.subject || '').slice(0, 70)} | ${pdfs.join('; ').slice(0, 70) || '(no PDF attached)'}`);
    });
    if (unmatched.length) {
      console.log(`\nNOT MATCHED to any account (${unmatched.length}) -- expected for receipts/invoices; a new sender needs a rule`);
      [...new Set(unmatched)].forEach((u) => console.log('  ' + u));
    }
  });
});
