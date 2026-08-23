// Telehack 共通ユーティリティ
async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  if (res.status === 401) { location.href = '/'; throw new Error('unauthorized'); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw Object.assign(new Error(data.detail || 'error'), {detail: data.detail, status: res.status});
  return data;
}
const jpost = (p, b) => api(p, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: b ? JSON.stringify(b) : undefined});
const jpatch = (p, b) => api(p, {method: 'PATCH', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(b)});
async function logout() { await fetch('/api/logout', {method: 'POST'}); location.href = '/'; }
const esc = s => { const d = document.createElement('span'); d.textContent = s ?? ''; return d.innerHTML.replace(/"/g, '&quot;').replace(/'/g, '&#39;'); };

function fmtCountdown(sec) {
  sec = Math.max(0, Math.floor(sec));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return (h ? h + ':' : '') + String(m).padStart(2, '0') + ':' + String(s).padStart(2, '0');
}

// --- トースト通知 (お知らせ) ---
function toast(text, ms = 8000) {
  let box = document.getElementById('toastBox');
  if (!box) {
    box = document.createElement('div');
    box.id = 'toastBox';
    document.body.appendChild(box);
  }
  const el = document.createElement('div');
  el.className = 'toast';
  el.textContent = text;
  box.appendChild(el);
  setTimeout(() => el.classList.add('show'), 30);
  setTimeout(() => { el.classList.remove('show'); setTimeout(() => el.remove(), 400); }, ms);
}

// --- ドラムロール (WebAudio 合成、外部ファイル不要) ---
function playDrumroll(durationMs = 2800) {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const noise = ctx.createBuffer(1, ctx.sampleRate * 0.06, ctx.sampleRate);
    const d = noise.getChannelData(0);
    for (let i = 0; i < d.length; i++) d[i] = (Math.random() * 2 - 1) * Math.exp(-i / (d.length / 4));
    let t = ctx.currentTime + 0.05, interval = 0.1;
    const end = ctx.currentTime + durationMs / 1000;
    while (t < end) {
      const src = ctx.createBufferSource(); src.buffer = noise;
      const bp = ctx.createBiquadFilter(); bp.type = 'bandpass'; bp.frequency.value = 220 + Math.random() * 80;
      const g = ctx.createGain(); g.gain.value = 0.5;
      src.connect(bp).connect(g).connect(ctx.destination);
      src.start(t);
      t += interval; interval = Math.max(0.028, interval * 0.96);
    }
    // フィニッシュのシンバル
    const cy = ctx.createBuffer(1, ctx.sampleRate * 1.2, ctx.sampleRate);
    const cd = cy.getChannelData(0);
    for (let i = 0; i < cd.length; i++) cd[i] = (Math.random() * 2 - 1) * Math.exp(-i / (cd.length / 6));
    const src = ctx.createBufferSource(); src.buffer = cy;
    const hp = ctx.createBiquadFilter(); hp.type = 'highpass'; hp.frequency.value = 4000;
    const g = ctx.createGain(); g.gain.value = 0.6;
    src.connect(hp).connect(g).connect(ctx.destination);
    src.start(end);
  } catch (e) { /* 音が出なくても発表は続行 */ }
}

// --- 結果発表セレモニー ---
function ceremony(rank, title, team, votes) {
  let ov = document.getElementById('ceremonyOverlay');
  if (ov) ov.remove();
  ov = document.createElement('div');
  ov.id = 'ceremonyOverlay';
  ov.innerHTML = `
    <div class="ceremony-inner">
      <div class="ceremony-rank">第 ${rank} 位</div>
      <div class="ceremony-drum">🥁 ドルルルルル…</div>
      <div class="ceremony-body" style="display:none">
        <div class="ceremony-title"></div>
        <div class="ceremony-team"></div>
        <div class="ceremony-votes"></div>
        <button class="secondary" onclick="document.getElementById('ceremonyOverlay').remove()">閉じる</button>
      </div>
    </div>`;
  ov.querySelector('.ceremony-title').textContent = title;
  ov.querySelector('.ceremony-team').textContent = team ? `by ${team}` : '';
  ov.querySelector('.ceremony-votes').textContent = votes != null ? `${votes} 票` : '';
  document.body.appendChild(ov);
  playDrumroll(2800);
  setTimeout(() => {
    ov.querySelector('.ceremony-drum').style.display = 'none';
    ov.querySelector('.ceremony-body').style.display = '';
    ov.classList.add('revealed');
  }, 3000);
}
