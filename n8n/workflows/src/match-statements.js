// One output item per PDF attachment that matches an account's `match` rules, plus
// one per email for accounts with `link.enabled` whose statement arrives as a
// download link instead of an attachment (fetched by the next node).
//
// `match` is a rule object or a list of them (any rule may match). Per rule, all
// case-insensitive; empty fields are ignored and every non-empty field must match:
//   from     – substring of the sender address / display name
//   to       – substring of the To header (e.g. statements+julia@ plus-addressing)
//   subject  – regex against the subject
//   filename – regex against the attachment filename (not usable for link accounts)
//   label    – a Gmail label on the email, by name, whole name, e.g. "Rob" (set by hand to tell apart
//              mail that nothing in the headers can: the scan paths of Manual Scan / Pete / Julia add
//              the label names; the hourly trigger does not)
// A rule with no fields never matches. If several accounts match, the one with the
// most matching fields wins. Emails that match nothing are dropped.
//
// A rule may also carry "by_content": "regex". It is a last resort for accounts that
// nothing in the email can tell apart (two people's SAVINGS ACCOUNT n.pdf): it is only
// used when no ordinary rule of any account matched. If one account's by_content rule
// matches the email, that account gets the file; if several do, the PDF is sent to the
// sidecar's /identify and the account whose by_content regex is found in the PDF's text
// (e.g. the holder's name) gets the file. If that does not settle on exactly one account
// the PDF is treated as unmatched.
//
// Forwards from TRUSTED_FORWARDERS are unwrapped: the original sender, subject and
// recipient are read from the "Forwarded message" block, so a one-off manual forward
// is matched exactly as the original email would have been.
const TRUSTED_FORWARDERS = ['pete@vanheerden.ch', 'julia@vanheerden.ch'];
const MAX_LINKS = 15;

// Account list (match rules, folders, link settings; no secrets) from the sidecar.
// Fetched here, and the emails arrive as this node's input, so there is no lookup of
// other nodes by name and a failure is loud instead of silently matching nothing.
const accounts = await this.helpers.httpRequest({ method: 'GET', url: 'http://pdf-decrypt:8080/accounts', json: true });
const FIELDS = ['from', 'to', 'subject', 'filename', 'label'];

const asRules = (m) => (Array.isArray(m) ? m : m ? [m] : []);
const fieldsOf = (r = {}) => FIELDS.filter((k) => (r[k] || '').trim() !== '');

function ruleScore(r, mail) {
  const keys = fieldsOf(r);
  if (keys.length === 0) return 0;
  const ok = keys.every((k) => {
    if (k === 'label') return (mail.labels || []).some((l) => String(l).toLowerCase() === r[k].trim().toLowerCase());
    if (k === 'from' || k === 'to') return mail[k].toLowerCase().includes(r[k].trim().toLowerCase());
    return new RegExp(r[k], 'i').test(mail[k]);
  });
  return ok ? keys.length : 0;
}

// Best score across an account's rules (0 = no match). For link emails there is
// no attachment, so a rule that needs a filename cannot apply.
const score = (m, mail, noFilename) =>
  Math.max(0, ...asRules(m).map((r) => (r.by_content || (noFilename && (r.filename || '').trim()) ? 0 : ruleScore(r, mail))));
// The accounts with a by_content rule that matches this mail.
const contentCandidates = (mail) =>
  Object.entries(accounts)
    .filter(([, a]) => asRules(a.match).some((r) => r.by_content && ruleScore(r, mail) > 0))
    .map(([k]) => k);

const hdr = (v, alt) =>
  [
    ...((v && v.value) || []).map((x) => `${x.name || ''} <${x.address || ''}>`),
    (v && v.text) || '',
    typeof v === 'string' ? v : '',
    alt || '',
  ].join(' ');

const stripTags = (h) =>
  h
    .replace(/<br\s*\/?>/gi, '\n')
    .replace(/<\/(p|div|tr|li)>/gi, '\n')
    .replace(/<[^>]+>/g, ' ')
    .replace(/&nbsp;/g, ' ')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&amp;/g, '&');

// Reads the quoted header block of a forward. Handles Gmail ("---------- Forwarded
// message ---------" then From:/Date:/Subject:/To:) and Outlook ("From: / Sent: / To:
// / Subject:" at the top of the body, labels often wrapped as *From:*), including
// values that wrap onto the next line (e.g. a long address after "<").
// A trusted address is only believed if the receiving server (Google) recorded an
// aligned authentication pass for that address's domain: SPF passed with a matching
// envelope sender, or DKIM passed for the domain. A forged "From: julia@..." from
// outside fails this, so its forwarded block is not trusted (and the mail goes unmatched).
function authenticated(headers, domain) {
  const raw = (headers || {})['authentication-results'];
  const ours = (Array.isArray(raw) ? raw : [raw])
    .flatMap((h) => String(h || '').split(/(?=authentication-results:)/i))
    .find((h) => /mx\.google\.com/i.test(h)); // the first header is the one our own server added
  if (!ours) return false;
  const ar = ours.toLowerCase();
  const d = domain.toLowerCase().replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  // SPF passed for an envelope sender in the trusted domain (exact domain, not a look-alike)
  const spf = ar.includes('spf=pass') && new RegExp(`smtp\\.mailfrom=(?:[^\\s;@]+@)?${d}(?=[\\s;]|$)`).test(ar);
  // ...or DKIM passed with a signing identity in the trusted domain
  const dkim = new RegExp(`dkim=pass[^;]*header\\.[id]=@?${d}(?=[\\s;]|$)`).test(ar);
  return spf || dkim;
}

function unwrapForward(mail, body, headers) {
  const forwarder = TRUSTED_FORWARDERS.find((f) => mail.from.toLowerCase().includes(f.toLowerCase()));
  if (!forwarder || !authenticated(headers, forwarder.split('@')[1])) return mail;
  const marker = body.match(/(?:Forwarded message|Original Message)\s*-{3,}/i);
  const from0 = marker ? marker.index : 0;
  const win = body.slice(from0, from0 + 3000);
  const start = win.search(/\*?From\s*:\*?/i);
  if (start < 0 || start > (marker ? 200 : 600)) return mail;

  const labels = [...win.slice(start).matchAll(/\*?\b(From|Sent|Date|To|Cc|Bcc|Subject)\s*:\*?/gi)];
  const got = {};
  for (let i = 0; i < labels.length; i++) {
    const key = labels[i][1].toLowerCase();
    if (key in got) break;
    const valueStart = labels[i].index + labels[i][0].length;
    const rest = win.slice(start);
    let value;
    if (key === 'subject' || i === labels.length - 1) {
      value = rest.slice(valueStart).split('\n')[0];
    } else {
      value = rest.slice(valueStart, labels[i + 1].index);
    }
    got[key] = value.replace(/\s+/g, ' ').replace(/<\s+/g, '<').replace(/\s+>/g, '>').trim();
    if (key === 'subject') break;
  }
  return {
    from: got.from || mail.from,
    // keep BOTH recipients: the forward's own To (e.g. statements+rob@) selects the
    // account for plus-addressed rules, the original To is what the sender used.
    to: `${mail.to} ${got.to || ''}`.trim(),
    subject: got.subject || mail.subject,
    // when the ORIGINAL email was sent (Gmail "Date:", Outlook "Sent:"), not when it was forwarded
    date: dateFromText(got.date || got.sent || ''),
  };
}

// (The Code node sandbox has no URL class, so the host is read with a pattern.)
const hostOf = (u) => {
  const m = String(u).match(/^https?:\/\/(?:[^\/?#@]*@)?([^\/?#:]+)/i);
  return m ? m[1].toLowerCase() : '';
};

function linksOf(html, text, allow) {
  const urls = new Set();
  for (const m of (html || '').matchAll(/href\s*=\s*["']([^"']+)["']/gi)) urls.add(m[1].replace(/&amp;/g, '&'));
  for (const m of (text || '').matchAll(/https?:\/\/[^\s<>"')\]]+/g)) urls.add(m[0]);
  return [...urls].filter((u) => {
    const h = hostOf(u);
    return /^https?:/i.test(u) && allow.some((a) => h === a || h.endsWith('.' + a));
  });
}

const safeName = (s) => s.replace(/[\\/:*?"<>|#%]/g, '_').trim();

// The statement's own date, read from text such as the original file name.
// Recognised: 2026-10-02 / 2026_10_02, 20261007, 25092026 (DDMMYYYY), 18 SEP 2026,
// 01Jul2026-31Jul2026 (a range: the later date), and month-only forms (Oct_2026,
// SEPTEMBER 2026, 202610 -> "2026-10"). Returns "YYYY-MM-DD", "YYYY-MM" or ''.
const MONTHS = { jan: 1, feb: 2, mar: 3, apr: 4, may: 5, jun: 6, jul: 7, aug: 8, sep: 9, oct: 10, nov: 11, dec: 12 };
const daysIn = (y, m) => new Date(Date.UTC(y, m, 0)).getUTCDate();
function dateFromText(text) {
  const t = String(text || '');
  const full = [];
  const monthOnly = [];
  const add = (list, y, m, d) => {
    y = +y; m = +m; d = d == null ? null : +d;
    if (y < 2000 || y > 2100 || m < 1 || m > 12) return;
    if (d !== null && (d < 1 || d > daysIn(y, m))) return;
    list.push([y, m, d || 0]);
  };
  for (const m of t.matchAll(/(?<!\d)(20\d{2})[-_./](\d{2})[-_./](\d{2})(?!\d)/g)) add(full, m[1], m[2], m[3]);
  for (const m of t.matchAll(/(?<!\d)(\d{8})(?!\d)/g)) {
    const s = m[1];
    const before = full.length;
    if (s.startsWith('20')) add(full, s.slice(0, 4), s.slice(4, 6), s.slice(6, 8)); // YYYYMMDD
    if (full.length === before) add(full, s.slice(4, 8), s.slice(2, 4), s.slice(0, 2)); // DDMMYYYY
  }
  for (const m of t.matchAll(/(?<!\d)(\d{1,2})[ _-]?([A-Za-z]{3,9})[ _-]?(20\d{2})(?!\d)/g)) {
    const mo = MONTHS[m[2].slice(0, 3).toLowerCase()];
    if (mo) add(full, m[3], mo, m[1]);
  }
  // "Oct 14, 2026" / "Wed, Oct 14, 2026 at 10:08 PM" (Gmail forward headers)
  for (const m of t.matchAll(/(?<![A-Za-z])([A-Za-z]{3,9})\.? (\d{1,2}),? (20\d{2})(?!\d)/g)) {
    const mo = MONTHS[m[1].slice(0, 3).toLowerCase()];
    if (mo) add(full, m[3], mo, m[2]);
  }
  for (const m of t.matchAll(/(?<!\d)([A-Za-z]{3,9})[ _-]?(20\d{2})(?!\d)/g)) {
    const mo = MONTHS[m[1].slice(0, 3).toLowerCase()];
    if (mo) add(monthOnly, m[2], mo, null);
  }
  for (const m of t.matchAll(/(?<!\d)(20\d{2})(0[1-9]|1[0-2])(?!\d)/g)) add(monthOnly, m[1], m[2], null);
  const latest = (l) => l.sort((a, b) => a[0] - b[0] || a[1] - b[1] || a[2] - b[2]).pop();
  const p2 = (n) => String(n).padStart(2, '0');
  if (full.length) { const [y, mo, d] = latest(full); return `${y}-${p2(mo)}-${p2(d)}`; }
  if (monthOnly.length) { const [y, mo] = latest(monthOnly); return `${y}-${p2(mo)}`; }
  return '';
}

// Saved file name. Per-account `statements.filename_template` overrides the default.
// Tokens: {date} the statement's own date when its file name (or, for link statements,
// the subject) contains one, otherwise the email date (set `statements.date_source` to
// "email" to always use the email date); {emaildate} always the email date; {year},
// {month} from {date}; {key} account key; {name} account name; {subject} the email
// subject; {original} the original file name without its .pdf extension (the email
// subject for link statements). Keep {original} (or something equally unique) in a
// template: same-named files replace each other. ".pdf" is always appended.
//
// `statements.sequence` is for statements whose file name has a running number and no
// date (FNB online banking: "FNB PREMIER CREDIT 566.pdf"). One known point fixes the
// rest, one statement per month: { "anchor": { "number": 566, "month": "2026-09" },
// "regex": "(\\d+)\\.pdf$" } (regex optional, default: the number at the end of the name). The month
// is the one the statement is filed under (a card statement issued in the first week of
// October covers, and is named for, September), so {date} becomes "2026.09" and {year} and
// {month} follow. It takes the place of the date found in the name or the email date.
//
// `statements.month_offset` (integer, e.g. -1) moves whatever month {date} resolved to by that
// many months, for statements issued in the month after the one they cover (ABSA: issued
// in September, covers August). The day is dropped, so {date} becomes "2026.08". It applies
// to the month from any source: file name, email date or sequence.
const DEFAULT_TEMPLATE = '{date}_{key}_{original}';
function dateFromSequence(seq, originalName) {
  const a = seq && seq.anchor;
  const base = a && /^(\d{4})-(\d{2})$/.exec(String(a.month || ''));
  if (!base || !Number.isInteger(a.number)) return '';
  let n;
  try {
    n = new RegExp(seq.regex || '(\\d+)(?:\\.pdf)?$', 'i').exec(String(originalName || ''));
  } catch (e) {
    return '';
  }
  if (!n || !/^\d+$/.test(n[1])) return '';
  const idx = +base[1] * 12 + (+base[2] - 1) + (parseInt(n[1], 10) - a.number);
  const y = Math.floor(idx / 12);
  if (y < 2000 || y > 2100) return '';
  return `${y}-${String((idx % 12) + 1).padStart(2, '0')}`;
}
function tokensFor(cfg, account, emailDate, originalName, subject) {
  const found = dateFromSequence(cfg.sequence, originalName) || (cfg.date_source === 'email' ? '' : dateFromText(originalName));
  let date = found || emailDate;
  const off = cfg.month_offset;
  if (Number.isInteger(off) && off !== 0 && /^\d{4}-\d{2}/.test(date)) {
    const idx = +date.slice(0, 4) * 12 + (+date.slice(5, 7) - 1) + off;
    date = `${Math.floor(idx / 12)}-${String((idx % 12) + 1).padStart(2, '0')}`;
  }
  return {
    // file names use periods (2026.10.07); year/month are cut from the ISO form first
    date: date.replace(/-/g, '.'), emaildate: emailDate.replace(/-/g, '.'), year: date.slice(0, 4), month: date.slice(5, 7),
    key: account, name: cfg.name || account, subject: safeName(subject), original: safeName(originalName).replace(/\.pdf$/i, ''),
  };
}
function nameFor(template, tokens) {
  const s = safeName(
    (template || DEFAULT_TEMPLATE).replace(/\{(\w+)\}/g, (m, k) => (k in tokens ? tokens[k] : m)),
  );
  return /\.pdf$/i.test(s) ? s : `${s}.pdf`;
}
const out = [];

// Emails from the hourly Gmail Trigger or the manual Scan Inbox (whichever ran).
const emails = $input.all();

for (const [index, item] of emails.entries()) await (async () => {
  const j = item.json;
  const body = j.text || stripTags(j.html || j.textAsHtml || '');
  const mail0 = unwrapForward(
    { from: hdr(j.from, j.From), to: hdr(j.to, j.To), subject: j.subject || j.Subject || '' },
    body,
    j.headers,
  );
  mail0.labels = Array.isArray(j.labels) ? j.labels : [];
  const parsed = new Date(j.date || j.Date || Number(j.internalDate) || Date.now());
  const receivedDay = (isNaN(parsed) ? new Date() : parsed).toISOString().slice(0, 10);
  // for a forward, the date the original email was sent (if it could be read), else when it arrived
  const day = /^\d{4}-\d{2}-\d{2}$/.test(mail0.date || '') ? mail0.date : receivedDay;
  const folderOf = (cfg, account) => {
    const folder = (cfg.onedrive_folder || '').replace(/^\/+|\/+$/g, '');
    if (!folder) throw new Error(`Account ${account} has no onedrive_folder in private-config.json`);
    return folder;
  };
  const pathOf = (folder, fileName) => [...folder.split('/'), fileName].map(encodeURIComponent).join('/');
  // Where to PUT the file. A folder id wins over the path, so folders can be moved or
  // renamed in OneDrive: first a `folder_id` set in the config, then the id the workflow
  // recorded earlier for this path, else the path itself (Graph creates missing folders and
  // the response tells the next node the id to record). `drive_id` only matters for a folder
  // in someone else's drive.
  const targetOf = (cfg, folder, fileName) => {
    const recorded = cfg.recorded || {};
    const source = cfg.folder_id ? 'config' : recorded.folder_id ? 'recorded' : 'path';
    const folderId = source === 'config' ? cfg.folder_id : recorded.folder_id || '';
    const driveId = source === 'config' ? cfg.drive_id || '' : recorded.drive_id || '';
    const drive = driveId ? `drives/${encodeURIComponent(driveId)}` : 'me/drive';
    const uploadUrl = folderId
      ? `https://graph.microsoft.com/v1.0/${drive}/items/${encodeURIComponent(folderId)}:/${encodeURIComponent(fileName)}:/content`
      : `https://graph.microsoft.com/v1.0/me/drive/root:/${pathOf(folder, fileName)}:/content`;
    return { uploadUrl, folderSource: source, folderId, driveId, recordedPath: recorded.seen_path || '' };
  };

  // Which mailbox the email came from (the Scan Pete/Julia branches tag it) and whether the account
  // wants its emails deleted once filed. An email with a PDF that matched no account must never be
  // purged: it may hold something else (a receipt, a tax invoice), so Mail Outcome checks the flag.
  const mailbox = j.mailbox || 'statements';
  // Says which email a file came from, for the failure/summary mails (open it in the right account)
  const MAILBOXES = { statements: 'statements@vanheerden.ch', pete: 'pete@vanheerden.ch', julia: 'julia@vanheerden.ch' };
  const emailInfo =
    `"${(mail0.subject || '(no subject)').trim().slice(0, 90)}" from ${mail0.from.trim().slice(0, 80)}, ${receivedDay}, ${mailbox} mailbox ` +
    `https://mail.google.com/mail/?authuser=${MAILBOXES[mailbox] || ''}#all/${j.id}`;
  let attached = false;
  let pdfCount = 0;
  const emitted = [];
  for (const [binKey, bin] of Object.entries(item.binary || {})) {
    const original = bin.fileName || 'statement.pdf';
    // judge by the attachment's real type/name: `original` falls back to 'statement.pdf' for unnamed
    // attachments (inline images), which must not count as a PDF
    const isPdf = bin.mimeType === 'application/pdf' || /\.pdf$/i.test(bin.fileName || '');
    if (!isPdf) continue;
    pdfCount++;

    const mail = { ...mail0, filename: original };
    let hit = Object.entries(accounts)
      .map(([k, a]) => [k, a, score(a.match, mail, false)])
      .filter((x) => x[2] > 0)
      .sort((a, b) => b[2] - a[2])[0];
    if (!hit) {
      const candidates = contentCandidates(mail);
      let owner = candidates.length === 1 ? candidates[0] : '';
      if (candidates.length > 1) {
        const res = await this.helpers.httpRequest({
          method: 'POST',
          url: `http://pdf-decrypt:8080/identify?accounts=${candidates.map(encodeURIComponent).join(',')}`,
          // base64 text: a Buffer body does not survive the hop out of the Code node sandbox
          body: (await this.helpers.getBinaryDataBuffer(index, binKey)).toString('base64'),
          headers: { 'Content-Type': 'text/plain' },
          json: false,
          timeout: 30000,
        });
        // the helper may hand back text, bytes or an already parsed object
        const answer = typeof res === 'string' ? JSON.parse(res) : Buffer.isBuffer(res) ? JSON.parse(res.toString()) : res;
        owner = (answer && answer.account) || '';
      }
      if (!owner) continue;
      hit = [owner, accounts[owner], 1];
    }

    const [account, cfg] = hit;
    const folder = folderOf(cfg, account);
    // Banks reuse the same attachment name every month, so the template carries the date.
    const fileName = nameFor(cfg.filename_template, tokensFor(cfg, account, day, original, mail0.subject));
    attached = true;
    const o = {
      json: { account, folder, fileName, uploadPath: pathOf(folder, fileName), ...targetOf(cfg, folder, fileName), messageId: j.id, mailbox, emailInfo, purge: !!cfg.purge_email, subject: mail0.subject, from: mail0.from.trim() },
      binary: { data: bin },
      pairedItem: { item: index },
    };
    emitted.push(o);
    out.push(o);
  }

  if (attached) {
    emitted.forEach((o) => { o.json.hasUnmatchedPdf = pdfCount > emitted.length; });
    return;
  }

  // No matching attachment: look for accounts whose statement is a download link.
  const hit = Object.entries(accounts)
    .filter(([, a]) => a.link && a.link.enabled)
    .map(([k, a]) => [k, a, score(a.match, mail0, true)])
    .filter((x) => x[2] > 0)
    .sort((a, b) => b[2] - a[2])[0];
  if (!hit) return;

  const [account, cfg] = hit;
  const urls = linksOf(j.html || j.textAsHtml, j.text, cfg.link.allow_hosts || []).slice(0, MAX_LINKS);
  if (urls.length === 0) return;

  const folder = folderOf(cfg, account);
  const fileName = nameFor(cfg.filename_template, tokensFor(cfg, account, day, mail0.subject || 'statement', mail0.subject));
  out.push({
    json: { account, folder, fileName, uploadPath: pathOf(folder, fileName), ...targetOf(cfg, folder, fileName), messageId: j.id, mailbox, emailInfo, purge: !!cfg.purge_email, hasUnmatchedPdf: pdfCount > 0, subject: mail0.subject, from: mail0.from.trim(), viaLink: true, linkCandidates: urls },
    pairedItem: { item: index },
  });
})();

return out;
