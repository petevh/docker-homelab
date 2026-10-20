// Runs once, after everything else: decrypt failures, upload failures and successful
// uploads all arrive here through the Merge node, which waits for every branch. If any
// file failed, one item with the text of one alert email goes on to be sent and to fail
// the run; if nothing failed, nothing continues.
const lines = $input.all().map((i) => i.json).filter((j) => j.failure && j.line);
if (lines.length === 0) return [];
const count = lines.length;
return [
  {
    json: {
      subject: `Bank statements: ${count} file${count === 1 ? '' : 's'} not filed`,
      body: lines.map((j) => j.line).join('\n\n'),
      count,
    },
  },
];
