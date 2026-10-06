// Confirmation prompts on irreversible admin actions
document.addEventListener('submit', (e) => {
  const msg = e.target.dataset.confirm;
  if (msg && !confirm(msg)) e.preventDefault();
});

// Copy-to-clipboard buttons
document.querySelectorAll('[data-copy]').forEach((btn) => {
  btn.addEventListener('click', async () => {
    try { await navigator.clipboard.writeText(btn.dataset.copy); btn.textContent = 'Copied!'; }
    catch { btn.textContent = 'Select & copy'; }
    setTimeout(() => (btn.textContent = 'Copy'), 1500);
  });
});

// Sparklines
document.querySelectorAll('svg.spark').forEach((svg) => {
  const pts = JSON.parse(svg.dataset.history || '[]');
  if (pts.length < 2) pts.unshift(pts[0] || ['', 0.5]);
  const w = 120, h = 32, pad = 2;
  const xy = pts.map((p, i) => [(i / (pts.length - 1)) * w, pad + (1 - p[1]) * (h - pad * 2)]);
  const d = xy.map((p, i) => (i ? 'L' : 'M') + p[0].toFixed(1) + ' ' + p[1].toFixed(1)).join(' ');
  const up = pts[pts.length - 1][1] >= pts[0][1];
  svg.innerHTML =
    `<line x1="0" x2="${w}" y1="${h / 2}" y2="${h / 2}" class="mid"/>` +
    `<path d="${d}" class="${up ? 'up' : 'down'}" vector-effect="non-scaling-stroke"/>`;
});

// Trade forms with live quotes
const fmtMoney = (x) => '$' + x.toFixed(2);
const fmtPct = (p) => (p * 100).toFixed(p < 0.01 || p > 0.99 ? 1 : 0) + '%';

document.querySelectorAll('.contract').forEach((card) => {
  const form = card.querySelector('.trade-form');
  if (!form) return;
  const quote = form.querySelector('.quote');
  const title = form.querySelector('.tf-title');
  const amountBox = form.querySelector('.tf-amount');
  const sharesBox = form.querySelector('.tf-shares');
  const submit = form.querySelector('.tf-submit');
  let timer, seq = 0, maxShares = 0;

  function open(action, side, max) {
    form.hidden = false;
    form.action.value = action;   // hidden input named "action"
    form.side.value = side;
    amountBox.hidden = action !== 'buy';
    sharesBox.hidden = action !== 'sell';
    maxShares = parseFloat(max || 0);
    title.innerHTML = `${action === 'buy' ? 'Buy' : 'Sell'} <span class="side ${side.toLowerCase()}">${side}</span>`;
    form.classList.toggle('selling', action === 'sell');
    quote.textContent = '';
    if (action === 'sell') { form.shares.value = maxShares.toFixed(2); requestQuote(); form.shares.focus(); }
    else { form.amount.focus(); if (form.amount.value) requestQuote(); }
  }

  card.querySelectorAll('[data-open]').forEach((b) =>
    b.addEventListener('click', () => open(b.dataset.open, b.dataset.side, b.dataset.max)));
  form.querySelector('[data-cancel]').addEventListener('click', () => { form.hidden = true; });
  form.querySelector('[data-max-btn]').addEventListener('click', () => {
    form.shares.value = maxShares.toFixed(2); form.shares.dataset.all = '1'; requestQuote();
  });
  form.shares.addEventListener('input', () => delete form.shares.dataset.all);
  form.addEventListener('submit', () => {
    // "Max" sends the exact share count so the whole position closes
    if (form.action.value === 'sell' && form.shares.dataset.all) form.shares.value = String(maxShares);
    submit.disabled = true;
  });
  [form.amount, form.shares].forEach((i) => i.addEventListener('input', () => {
    clearTimeout(timer); timer = setTimeout(requestQuote, 200);
  }));

  async function requestQuote() {
    const action = form.action.value, side = form.side.value;
    const params = new URLSearchParams({ cid: form.contract_id.value, side, action });
    if (action === 'buy') {
      if (!form.amount.value.trim()) { quote.textContent = ''; return; }
      params.set('amount', form.amount.value);
    } else {
      params.set('shares', form.shares.dataset.all ? String(maxShares) : form.shares.value);
    }
    const mine = ++seq;
    const r = await fetch('/api/quote?' + params).then((r) => r.json()).catch(() => null);
    if (mine !== seq) return;
    if (!r || !r.ok) { quote.innerHTML = `<span class="neg">${r ? r.error : 'Network error'}</span>`; return; }
    if (action === 'buy') {
      quote.innerHTML =
        `Get <b>${r.shares.toFixed(2)} shares</b> at avg ${(r.avg * 100).toFixed(1)}¢ · ` +
        `pays <b>${fmtMoney(r.payout)}</b> if ${side} (profit ${fmtMoney(r.payout - r.cost)}) · ` +
        `chance moves ${fmtPct(r.price_before)} → ${fmtPct(r.price_after)}` +
        (r.over_balance ? ' · <span class="neg">more than your balance</span>' : '');
    } else {
      quote.innerHTML =
        `Receive <b>${fmtMoney(r.proceeds)}</b> (avg ${(r.avg * 100).toFixed(1)}¢) · ` +
        `chance moves ${fmtPct(r.price_before)} → ${fmtPct(r.price_after)}`;
    }
  }
});
