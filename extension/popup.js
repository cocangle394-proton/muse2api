/* Muse2API Cookie Import — read muse.ai cookies and POST them to the muse2api service.
 *
 * Key point: use chrome.cookies instead of document.cookie.
 * The key muse.ai session cookies (hatch_sess / hatch_vml) are httpOnly,
 * so web-page JS cannot read them; only the browser extension's cookies API can.
 * hatch_gw is a re-issuable routing cache and should not block account import.
 */

const $ = (id) => document.getElementById(id);
const STORE = 'muse2api_ext_cfg';

const ESSENTIAL = ['hatch_sess'];

/* ================= UI language =================
 * English is the source language; the dictionary below maps source strings to their display text.
 * When adding new copy, add its English text to I18N_EN (whole sentences, may contain {name} placeholders) or I18N_EN_PATTERNS (server-returned strings with variables). */
const LANG_STORE = 'muse2api_lang';
const LANG = (() => {
  try { const v = localStorage.getItem(LANG_STORE); if (v === 'zh' || v === 'en') return v; } catch (e) {}
  return /^zh\b/i.test(navigator.language || '') ? 'zh' : 'en';
})();
function setLang(v) {
  try { localStorage.setItem(LANG_STORE, v); } catch (e) {}
  location.reload();
}

/* Whole sentences: source text -> display text */
const I18N_EN = {
  // popup.html static copy
  'Muse2API Cookie Import': 'Muse2API Cookie Import',
  'Sync the muse.ai login session from this browser to your muse2api service in one click.':
    'Sync the muse.ai login session from this browser to your muse2api service in one click.',
  'Service URL (BASE URL)': 'Service URL (BASE URL)',
  'Account label (optional)': 'Account label (optional)',
  'e.g. muse-main': 'e.g. muse-main',
  'Read and import': 'Read and import',
  // popup.js log / status
  'Please enter the service URL first': 'Please enter the service URL first',
  'Please enter the API Key first': 'Please enter the API Key first',
  'Reading muse.ai cookies…': 'Reading muse.ai cookies…',
  'Cannot identify the current window\'s cookie store. Open the extension in the window signed in to Muse, and allow it in Incognito/InPrivate mode if needed.':
    'Cannot identify the current window\'s cookie store. Open the extension in the window signed in to Muse, and allow it in Incognito/InPrivate mode if needed.',
  'Reading muse.ai cookies from the current {window} window…': 'Reading muse.ai cookies from the current {window} window…',
  'Incognito/InPrivate': 'Incognito/InPrivate',
  'regular': 'regular',
  '\nCookie source: current {window} window. Import only saves the session; test the account on the admin page.':
    '\nCookie source: current {window} window. Import only saves the session; test the account on the admin page.',
  'No muse.ai cookies found.\nOpen and log in to https://muse.ai/ in this browser first, then click again.':
    'No muse.ai cookies found.\nOpen and log in to https://muse.ai/ in this browser first, then click again.',
  'Read {n} cookies, but missing core items such as: {missing}\nThe login session could not be read. Check this browser\'s login and extension permissions.':
    'Read {n} cookies, but missing core items such as: {missing}\nThe login session could not be read. Check this browser\'s login and extension permissions.',
  'Read {n} cookies, uploading to {base} …': 'Read {n} cookies, uploading to {base} …',
  'Invalid API Key (the service returned 401).\nCopy the correct API Key from the top of the "Account Pool" tab on the admin page.':
    'Invalid API Key (the service returned 401).\nCopy the correct API Key from the top of the "Account Pool" tab on the admin page.',
  'Import failed: HTTP {status}\n{body}': 'Import failed: HTTP {status}\n{body}',
  '(auto)': '(auto)',
  'unknown': 'unknown',
  '✓ Imported successfully\nAccount label: {label}\nAccount ID: {id}\nCookie count: {count}\nValid until: {expires}\n':
    '✓ Imported successfully\nAccount label: {label}\nAccount ID: {id}\nCookie count: {count}\nValid until: {expires}\n',
  '\nNote: {warning}': '\nNote: {warning}',
  'Something went wrong: {msg}\n\nCommon causes:\n· The service URL is wrong or the service is not running\n· The URL is not https (or the certificate is not trusted)\n· The browser blocked the cross-origin request':
    'Something went wrong: {msg}\n\nCommon causes:\n· The service URL is wrong or the service is not running\n· The URL is not https (or the certificate is not trusted)\n· The browser blocked the cross-origin request',
  // Whole sentences the server (/admin/accounts) may return
  'No cookies could be parsed; please check the format': 'No cookies could be parsed; please check the format',
  'Missing Authorization: Bearer <key>': 'Missing Authorization: Bearer <key>',
  'Invalid API key': 'Invalid API key',
};

/* Rich-text blocks: data-i18n="key" -> English innerHTML */
const I18N_EN_HTML = {
  steps: '<b>Before you start, check two things:</b><br>\n'
    + '    1. You are logged in to <a href="https://muse.ai/" target="_blank"\n'
    + '       style="color:#4f8cff">muse.ai</a> in <b>this browser</b> (you can see the chat interface).<br>\n'
    + '    2. The service URL and API Key above are filled in (you can copy them from the top of the "Account Pool" tab on the muse2api admin page).',
  foot: 'This extension does only one thing: it uses the browser\'s <code>chrome.cookies</code> API to read muse.ai\n'
    + '    cookies (including the httpOnly ones) and POSTs them to the service URL you entered.\n'
    + '    Nothing is collected or uploaded to any third-party server.',
};

/* Server-returned strings with variables: [regex, replacement] */
const I18N_EN_PATTERNS = [
  [/^Missing core cookies: (.*); this account may not be able to generate$/s, 'Missing core cookies: $1; this account may not be able to generate'],
];

function t(s, vars) {
  let out = s;
  if (LANG === 'en' && typeof s === 'string') {
    if (Object.prototype.hasOwnProperty.call(I18N_EN, s)) out = I18N_EN[s];
    else for (const [re, rep] of I18N_EN_PATTERNS) if (re.test(s)) { out = s.replace(re, rep); break; }
  }
  if (vars) out = out.replace(/\{(\w+)\}/g, (m, k) => (k in vars ? String(vars[k]) : m));
  return out;
}

function applyLang() {
  $('langSel').value = LANG;
  if (LANG !== 'en') return;
  document.documentElement.lang = 'en';
  document.title = t(document.title);
  document.querySelectorAll('[data-i18n]').forEach((el) => {
    const html = I18N_EN_HTML[el.dataset.i18n];
    if (html) el.innerHTML = html;
  });
  document.querySelectorAll('[data-i18n-text]').forEach((el) => {
    el.textContent = t(el.textContent.trim());
  });
  document.querySelectorAll('[placeholder]').forEach((el) => {
    el.placeholder = t(el.placeholder);
  });
}

function log(html, cls) {
  const el = $('log');
  el.className = 'show';
  el.innerHTML = cls ? `<span class="${cls}">${html}</span>` : html;
}

/* Normalize the service URL: strip the trailing slash and /v1 suffix */
function normBase(v) {
  let s = (v || '').trim();
  if (!s) return '';
  if (!/^https?:\/\//i.test(s)) s = 'https://' + s;
  s = s.replace(/\/+$/, '');
  s = s.replace(/\/v1$/i, '');
  return s;
}

async function loadCfg() {
  const o = await chrome.storage.local.get(STORE);
  const c = o[STORE] || {};
  if (c.base) $('base').value = c.base;
  if (c.key) $('key').value = c.key;
  if (c.label) $('label').value = c.label;
  // If never configured, try guessing from the current tab (when the user is on the admin page)
  if (!c.base) {
    try {
      const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
      const u = tab && tab.url ? new URL(tab.url) : null;
      if (u && /\/admin/.test(u.pathname)) {
        $('base').value = u.origin;
        const k = new URLSearchParams(u.search).get('key');
        if (k) $('key').value = k;
      }
    } catch (e) { /* Ignore */ }
  }
}

async function saveCfg() {
  await chrome.storage.local.set({
    [STORE]: {
      base: normBase($('base').value),
      key: $('key').value.trim(),
      label: $('label').value.trim(),
    },
  });
}

async function grabCookies() {
  // A spanning extension shares its regular context with incognito windows.
  // Select the active tab's store explicitly; never guess or fall back.
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  const stores = await chrome.cookies.getAllCookieStores();
  const store = tab && stores.find((s) => s.tabIds.includes(tab.id));
  if (!store) {
    throw new Error(t('Cannot identify the current window\'s cookie store. Open the extension in the window signed in to Muse, and allow it in Incognito/InPrivate mode if needed.'));
  }
  const windowKind = t(tab.incognito ? 'Incognito/InPrivate' : 'regular');
  log(t('Reading muse.ai cookies from the current {window} window…', { window: windowKind }));
  // URL filtering excludes unrelated subdomains and paths. Chrome orders
  // matches by longest path, then earliest creation time (as cookies.get does).
  const all = await chrome.cookies.getAll({ url: 'https://muse.ai/api/session', storeId: store.id });
  const out = Object.create(null), exp = Object.create(null);
  for (const c of all) {
    if (Object.prototype.hasOwnProperty.call(out, c.name)) continue;
    out[c.name] = c.value;
    if (c.expirationDate) exp[c.name] = Math.floor(c.expirationDate);
  }
  return { cookies: out, expires: exp, windowKind };
}

async function run() {
  const base = normBase($('base').value);
  const key = $('key').value.trim();
  const label = $('label').value.trim();

  if (!base) return log(t('Please enter the service URL first'), 'bad');
  if (!key) return log(t('Please enter the API Key first'), 'bad');

  $('go').disabled = true;
  log(t('Reading muse.ai cookies…'));

  try {
    const { cookies, expires, windowKind } = await grabCookies();
    const names = Object.keys(cookies);
    if (!names.length) {
      return log(t('No muse.ai cookies found.\nOpen and log in to '
                 + 'https://muse.ai/ in this browser first, then click again.'), 'bad');
    }
    const missing = ESSENTIAL.filter((n) => !cookies[n]);
    if (missing.length) {
      log(t('Read {n} cookies, but missing core items such as: {missing}\n'
          + 'The login session could not be read. Check this browser\'s login and extension permissions.',
          { n: names.length, missing: missing.join(', ') }), 'warn');
      return;
    }

    log(t('Read {n} cookies, uploading to {base} …', { n: names.length, base }));

    const r = await fetch(base + '/admin/accounts', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': 'Bearer ' + key,
      },
      body: JSON.stringify({ label, cookies, expires }),
    });

    const text = await r.text();
    let data;
    try { data = JSON.parse(text); } catch (e) { data = { raw: text }; }

    if (r.status === 401) {
      return log(t('Invalid API Key (the service returned 401).\n'
                 + 'Copy the correct API Key from the top of the "Account Pool" tab on the admin page.'), 'bad');
    }
    if (!r.ok) {
      const body = typeof data.detail === 'string' ? t(data.detail) : text.slice(0, 300);
      return log(t('Import failed: HTTP {status}\n{body}', { status: r.status, body }), 'bad');
    }

    const a = (data.added && data.added[0]) || {};
    await saveCfg();
    log(t('✓ Imported successfully\nAccount label: {label}\nAccount ID: {id}\nCookie count: {count}\nValid until: {expires}\n', {
          label: a.label || label || t('(auto)'),
          id: a.id || '?',
          count: a.cookie_count || names.length,
          expires: a.expires_at ? new Date(a.expires_at * 1000).toLocaleString() : t('unknown'),
        })
        + t('\nCookie source: current {window} window. Import only saves the session; test the account on the admin page.', { window: windowKind })
        + (data.warning ? t('\nNote: {warning}', { warning: t(data.warning) }) : ''), 'ok');
  } catch (e) {
    log(t('Something went wrong: {msg}\n\nCommon causes:\n'
        + '· The service URL is wrong or the service is not running\n'
        + '· The URL is not https (or the certificate is not trusted)\n'
        + '· The browser blocked the cross-origin request', { msg: e && e.message ? e.message : String(e) }), 'bad');
  } finally {
    $('go').disabled = false;
  }
}

applyLang();
$('langSel').addEventListener('change', (e) => setLang(e.target.value));
$('go').addEventListener('click', run);
loadCfg();
