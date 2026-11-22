/* 对话页：接引擎，管语言、音乐、附件、右栏 */
const $ = (s) => document.querySelector(s);

const PROV_LABEL = {};
async function loadLabels() {
  const d = await getJSON('/providers');
  ((d && d.items) || []).forEach((x) => { PROV_LABEL[x.name] = x.label || x.name; });
}
function plabel(name) { return PROV_LABEL[name] || name || ''; }

let pending = null, expanded = false, latencies = [];

/* ---- 主动追问：隔一阵来问一句 ---- */
/* 打开预览器后，自己读那一页，把结论说出来 */
async function readAfterSearch(q) {
  try {
    if (!(window.xingzi && window.xingzi.previewRead)) return;
    const wait = (ms) => new Promise((r) => setTimeout(r, ms));
    addSys(lang() === 'zh' ? '搜索结果已打开，等它加载完' : 'search page opened, waiting');
    await wait(5000);
    let d = await window.xingzi.previewRead();
    if (!d || !d.ok || !d.text) { addSys(lang() === 'zh' ? '这一页读不出来' : 'cannot read page'); return; }
    addSys((lang() === 'zh' ? '读到 ' : 'read ') + d.text.length + (lang() === 'zh' ? ' 字' : ' chars'));
    // 把看到的头两句当“思考”露出来，让人知道它在读什么
    const lines0 = d.text.split('\n').map((x) => x.trim()).filter((x) => x.length > 6).slice(0, 2);
    if (lines0.length) addSys((lang() === 'zh' ? '看到：' : 'saw: ') + lines0.join(' / ').slice(0, 60));
    // 搜索结果页通常只有标题，没有具体价格：点进第一条像“航班/机票”的结果再读一次
    const fewNumbers = (d.text.match(/\d{2,5}/g) || []).length < 8;
    if (fewNumbers && window.xingzi.previewLinks) {
      const L = await window.xingzi.previewLinks();
      const links = (L && L.links) || [];
      const key = /航班|机票|价格|报价|预订|订票|去哪儿|携程|同程|飞猪/;
      const hit = links.find((x) => key.test(x.t) && !/baidu\.com\/(s|link)/.test(x.h));
      if (hit) {
        addSys((lang() === 'zh' ? '这里看不到具体价格，点进「' : 'no prices here, opening ') + hit.t.slice(0, 18) + (lang() === 'zh' ? '」' : ''));
        try { window.xingzi.previewNav && window.xingzi.previewNav(hit.h); } catch (e) {}
        await wait(6000);
        const d2 = await window.xingzi.previewRead();
        if (d2 && d2.ok && d2.text && d2.text.length > 400) d = d2;
      }
    }
    addSys(lang() === 'zh' ? '整理价格…' : 'summarising…');
    const base = await findAPI();
    const r = await fetch(base + '/summarize', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: d.text, q })
    }).then((x) => x.json()).catch(() => null);
    if (r && r.ok && r.say) {
      addMsg(r.say, 'her');
      try {
        const base1 = await findAPI();
        await fetch(base1 + '/chat/note', { method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ who: 'me', text: String(q || '') }) });
        await fetch(base1 + '/chat/note', { method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ who: 'her', text: r.say }) });
      } catch (e) {}
    }
    else addSys(lang() === 'zh' ? '没整理出来' : 'nothing to report');
  } catch (e) {}
}

async function pollProactive() {
  try {
    const d = await getJSON('/proactive');
    if (d && d.ask) {
      addMsg('her', d.ask);
      if (typeof speak === 'function') { /* 不自动朗读，按需 */ }
    }
  } catch (e) {}
}

/* ---- 名字：界面上一律用身份档里的称呼 ---- */
/* ---- 点头像换照片 ---- */
async function wireFacePick() {
  const img = document.getElementById('faceImg');
  const inp = document.getElementById('facePick');
  if (!img || !inp) return;
  img.addEventListener('click', () => inp.click());
  inp.addEventListener('change', async () => {
    const f = inp.files && inp.files[0];
    if (!f) return;
    const rd = new FileReader();
    rd.onload = async () => {
      const base = await findAPI();
      const r = await fetch(base + '/avatar', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ data: rd.result }) }).then((x) => x.json()).catch(() => null);
      if (r && r.ok) { img.src = base + '/img?f=' + encodeURIComponent(r.path) + '&t=' + Date.now(); }
    };
    rd.readAsDataURL(f);
  });
}

async function applyNames() {
  const d = await getJSON('/identity');
  if (!d || !d.ok) return;
  const her = (d.assistant || '').trim() || (lang() === 'zh' ? '未命名' : 'Unnamed');
  ['#herName', '#railName', '#pvTitle'].forEach((sel) => {
    const el = document.querySelector(sel);
    if (el) el.textContent = her;
  });
  document.title = her;
}

/* ---- 左栏导航 ---- */
const NAV = {chat:'index.html', role:'role.html', trip:'trips.html', work:'work.html', mem:'memory.html', skill:'skills.html', code:'code.html', browser:'javascript:void(0)', debate:'debate.html', set:'settings.html'};
document.querySelectorAll('.nav a[data-nav]').forEach((a) => {
  a.addEventListener('click', (e) => {
    e.preventDefault();
    if (a.dataset.nav === 'browser' && window.xingzi && window.xingzi.openPreview) {
      window.xingzi.openPreview(); return;
    }
    location.href = NAV[a.dataset.nav] || 'index.html';
  });
});

/* ---- 语言 ---- */
function paintLang() {
  document.documentElement.lang = lang() === 'zh' ? 'zh-CN' : 'en';
  applyI18n();
  const ph = $('#text');
  if (ph) ph.placeholder = ph.getAttribute('data-' + lang() + '-ph') || '';
  $('#langBtn').textContent = lang() === 'zh' ? 'EN' : '中文';
}
$('#langBtn').onclick = () => {
  setLang(lang() === 'zh' ? 'en' : 'zh');
  paintLang(); paintIdeas(); paintDate(); paintPerm(); loadRail();
};
const PERM_LABEL = {
  1: ['只读', 'Read only'],
  2: ['工作区内可修改', 'Edit in workspace'],
  3: ['完全权限', 'Full access']
};
function permLevel() {
  const v = Number(localStorage.getItem('aster.perm'));
  return (v >= 1 && v <= 3) ? v : 2;
}
function paintPerm() {
  const lv = permLevel();
  const label = PERM_LABEL[lv] || PERM_LABEL[2];
  $('#permText').textContent = (lang() === 'zh' ? '权限 · ' : 'Access · ') + label[lang() === 'zh' ? 0 : 1];
}
/* 点开一个三项菜单，不再轮流切 */
function openPermMenu() {
  let menu = document.getElementById('permMenu');
  if (menu) { menu.remove(); return; }
  menu = document.createElement('div');
  menu.id = 'permMenu';
  menu.style.cssText = 'position:fixed;z-index:200;background:#fff;border:1px solid var(--line2);' +
    'border-radius:14px;box-shadow:var(--shadow);padding:6px;min-width:184px';
  menu.innerHTML = [1, 2, 3].map((lv) => {
    const l = PERM_LABEL[lv];
    const on = lv === permLevel();
    return '<div data-lv="' + lv + '" style="padding:8px 10px;border-radius:9px;font-size:12.5px;cursor:pointer;' +
      (on ? 'background:#fdeef0;color:var(--pink);' : 'color:var(--ink2);') + '">' +
      (lang() === 'zh' ? l[0] : l[1]) + '</div>';
  }).join('');
  const _r = $('#permBtn').getBoundingClientRect();
  menu.style.top = (_r.bottom + 6) + 'px';
  menu.style.left = Math.max(8, Math.min(window.innerWidth - 200, _r.right - 184)) + 'px';
  document.body.appendChild(menu);
  menu.querySelectorAll('[data-lv]').forEach((el) => {
    el.onclick = () => {
      const lv = Number(el.dataset.lv);
      localStorage.setItem('aster.perm', String(lv));
      paintPerm(); postJSON('/permissions', {level: lv}); menu.remove();
    };
  });
}
$('#permBtn').onclick = openPermMenu;
// 兜一层：捕获阶段也接一次，防止别的监听把它吞掉
document.addEventListener('click', (e) => {
  const b = e.target.closest && e.target.closest('#permBtn');
  if (b && !document.getElementById('permMenu')) { e.stopPropagation(); openPermMenu(); }
}, true);
document.addEventListener('click', (e) => {
  const m = document.getElementById('permMenu');
  if (m && !m.contains(e.target) && !$('#permBtn').contains(e.target)) m.remove();
});
function paintDate() {
  const d = new Date();
  const wdZh = ['日','一','二','三','四','五','六'][d.getDay()];
  const wdEn = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'][d.getDay()];
  $('#dayLine').textContent = lang() === 'zh'
    ? (d.getMonth()+1)+' 月 '+d.getDate()+' 日 · 周'+wdZh
    : d.toLocaleDateString('en-US', {month:'short', day:'numeric'}) + ' · ' + wdEn;
}

/* ---- 音乐开关 ---- */
let bgm = null;
function musicOn() { return localStorage.getItem('aster.music') === '1'; }
function paintMusic() {
  $('#musicBtn').classList.toggle('off', !musicOn());
  $('#musicBtn').title = musicOn() ? '音乐：开' : '音乐：关';
}
$('#musicBtn').onclick = async () => {
  localStorage.setItem('aster.music', musicOn() ? '0' : '1');
  paintMusic();
  if (musicOn()) {
    if (!bgm) {
      bgm = new Audio('../audio/open.m4a');
      bgm.loop = true; bgm.volume = 0.35;
    }
    try { await bgm.play(); } catch (e) {}
  } else if (bgm) { bgm.pause(); }
};

/* ---- 聊天 / 动手 两种模式 ---- */
let mode3 = localStorage.getItem('aster.mode') || 'chat';   // chat | work | debate
let workMode = mode3 === 'work';
function paintMode() {
  const b = document.getElementById('modeChip');
  if (!b) return;
  const zh = lang() === 'zh';
  b.textContent = mode3 === 'work' ? (zh ? '动手' : 'do') : mode3 === 'debate' ? (zh ? '议事' : 'debate')
    : (zh ? '聊天' : 'chat');
  b.style.color = mode3 === 'chat' ? 'var(--ink2)' : 'var(--pink)';
  b.style.borderColor = mode3 === 'chat' ? 'var(--line2)' : 'rgba(232,23,61,.4)';
}
document.addEventListener('click', (e) => {
  if (e.target && e.target.id === 'modeChip') {
    mode3 = mode3 === 'chat' ? 'work' : mode3 === 'work' ? 'debate' : 'chat';
    workMode = mode3 === 'work';
    localStorage.setItem('aster.mode', mode3); paintMode();
  }
  if (e.target && e.target.dataset && e.target.dataset.stopJob) {
    postJSON('/code/stop', {id: e.target.dataset.stopJob});
    e.target.textContent = lang() === 'zh' ? '已叫停' : 'stopped';
  }
});

function searchWords(t) {
  // 洗掉闲聊与虚词；词太少就接上上一轮的话题，避免把整句话丢给搜索引擎
  const drop = /(我|想|要|买|一个|一下|帮|帮我|请|麻烦|你|看|看看|对比|比较|比一下|价格|多少钱|便宜|哪个|哪家|平台|最近|怎么样|推荐|合适|时间|时间段|具体|都可以|不知道|也行|的|了|吗|呢|吧|啊|嘛|个|只|款|件|台|支|把|和|跟|从|到|去|在|有点|几个|几点|哪里|哪儿|怎么|什么|，|。|？|\?|、|！)/g;
  let w = (t || '').replace(drop, ' ').replace(/\s+/g, ' ').trim();
  const last = (window.__lastTopic || '').trim();
  const short = w.replace(/\s/g, '').length < 4;
  if (short && last) w = (last + ' ' + w).trim();
  if (!w) w = last || (t || '').trim();
  // 记下这一轮的话题，供下一轮接续
  const keep = w.replace(drop, ' ').replace(/\s+/g, ' ').trim();
  if (keep) window.__lastTopic = keep.slice(0, 12);
  return w;
}

/* ---- 侧边预览：像豆包那样，边干活边给你看 ---- */
let pvOpen = false;
function paintPvTab() {
  const t = document.getElementById('pvTab');
  if (t) t.style.display = pvOpen ? 'none' : 'block';
}

function openPreview(url) {
  const u = url ? (/^https?:\/\//i.test(url) ? url : 'https://www.baidu.com/s?wd=' + encodeURIComponent(url)) : '';
  if (window.xingzi && window.xingzi.openPreview) {
    // 直接把地址交给主进程开窗，省掉“先开窗再导航”的时序问题
    window.xingzi.openPreview(u);
    if (u) setTimeout(() => readAfterSearch(url), 4200);
    return;
  }
  const pane = document.getElementById('previewPane');
  if (!pane) return;
  pane.style.display = 'flex'; pvOpen = true; paintPvTab(); document.body.classList.add('pv-open');
  if (u) {
    document.getElementById('pvAddr').value = u;
    document.getElementById('pvView').setAttribute('src', u);
  }
}

function closePreview() {
  const pane = document.getElementById('previewPane');
  if (pane) pane.style.display = 'none';
  pvOpen = false; paintPvTab(); document.body.classList.remove('pv-open');
}
document.addEventListener('click', (e) => {
  if (e.target && e.target.id === 'pvTab') { openPreview(''); return; }
  if (e.target && e.target.id === 'previewBtn') {
    const q0 = (document.getElementById('input') || {}).value || '';
    const u0 = q0.trim();
    openPreview(u0);
  }
  if (e.target && e.target.id === 'pvClose') closePreview();
  if (e.target && e.target.id === 'pvGo') {
    const q = document.getElementById('pvAddr').value.trim();
    if (!q) return;
    if (window.xingzi && window.xingzi.openPreview) {
      window.xingzi.openPreview();
      setTimeout(() => {
        const tab = q;
        window.xingzi.previewNav && window.xingzi.previewNav(/^https?:\/\//i.test(tab) ? tab : 'https://www.baidu.com/s?wd=' + encodeURIComponent(tab));
        readAfterSearch(tab);
      }, 900);
    } else { openPreview(q); }
  }
});

/* ---- 用应用自己的内核打开页面，读价格 ---- */
function fetchByWebview(url, waitMs) {
  return new Promise((resolve) => {
    const wv = document.createElement('webview');
    wv.setAttribute('src', url);
    wv.style.cssText = 'position:fixed;left:-9999px;top:0;width:1200px;height:900px;opacity:0';
    document.body.appendChild(wv);
    let done = false;
    const finish = (v) => { if (done) return; done = true; try { wv.remove(); } catch (e) {} resolve(v); };
    const timer = setTimeout(() => finish({ ok: false, error: '页面打开超时' }), waitMs || 15000);
    wv.addEventListener('did-finish-load', () => {
      setTimeout(async () => {
        try {
          const out = await wv.executeJavaScript(`(() => {
            const t = document.title || '';
            const txt = document.body ? document.body.innerText : '';
            const nums = [];
            (txt.match(/¥\\s?[0-9]+(\\.[0-9]{1,2})?/g) || []).slice(0, 8).forEach((x) => {
              const v = parseFloat(x.replace(/[^0-9.]/g, '')); if (v > 0.5 && v < 200000) nums.push(v);
            });
            return { title: t, prices: Array.from(new Set(nums)).slice(0, 5) };
          })()`);
          clearTimeout(timer);
          finish(out && out.prices && out.prices.length
            ? { ok: true, title: out.title, prices: out.prices } : { ok: false, title: out && out.title, error: '页面里没读到价格' });
        } catch (e) {
          clearTimeout(timer); finish({ ok: false, error: String(e).slice(0, 60) });
        }
      }, 1200);
    });
    wv.addEventListener('did-fail-load', (e) => { clearTimeout(timer); finish({ ok: false, error: '打不开：' + (e.errorDescription || '') }); });
  });
}

/* ---- 动手：后台跑编码工作台，把过程写进对话 ---- */
let workJob = null, workTimer = null;
/* 2026-11-22：亲密的话常被当成「派活」。判活要两条同时成立：
   有干的动作 + 有活的对象，而且不能是亲密或情绪的句子。 */
const WORK_VERB = /(做|写|改|建|生成|开发|搞|部署|测试|整理|翻译|总结|查|找|排版|出一版|改一版)/;
const WORK_OBJ = /(文件|文档|代码|脚本|程序|接口|功能|页面|网页|网站|方案|表格|报告|简历|合同|邮件|PPT|ppt|图|视频|字幕|清单|计划书|README|readme|bug|数据|爬)/;
const INTIMATE = ['抱','亲','睡','想你','喜欢','身体','舒服','躺','脱','摸','吻','宝宝','宝贝','老公','老婆','疼','爱你','要你','上来','进来','湿','硬'];
function looksLikeWork(t) {
  const x = (t || '').trim();
  if (x.length < 3) return false;
  if (INTIMATE.some((w) => x.includes(w))) return false;
  if (!WORK_OBJ.test(x) || !WORK_VERB.test(x)) return false;
  if (/[？?]|吗[。！!～~]*$/.test(x) && !/(帮我|给我|替我|你来|你去|动手)/.test(x)) return false;
  return true;
}
async function runWork(task) {
  const pv = await getJSON('/providers');
  const usable = ((pv && pv.items) || []).filter((x) => x.has_key).slice(0, 2);
  if (usable.length < 2) { addSys(lang() === 'zh' ? '至少要两家可用模型' : 'need two usable models'); return; }
  const seats = usable.map((x, i) => ({provider: x.name, role: i === 0 ? '作者' : '评审'}));
  await postJSON('/chat/note', {who: 'me', text: task});
  const r = await postJSON('/code/start', {task, seats, rounds: 2, budget: Number(localStorage.getItem('aster.budget') || 0)});
  if (!r || !r.ok) { addSys((lang() === 'zh' ? '起不来：' : 'failed: ') + ((r && r.error) || '')); return; }
  workJob = r.id; localStorage.setItem('aster.workJob', r.id); pollWork._seen = 0;
  const line = document.createElement('p');
  line.className = 'sys';
  line.innerHTML = (lang() === 'zh' ? '开工 · ' : 'started · ') + usable.map((x) => x.label).join(' / ') +
    ' · <a data-stop-job="' + r.id + '" style="color:var(--pink);cursor:pointer">' +
    (lang() === 'zh' ? '叫停' : 'stop') + '</a>';
  $('#log').appendChild(line);
  workTimer = setInterval(pollWork, 2000);
}
let debateJob = null, debateTimer = null, debateSeen = 0;
async function runDebate(topic) {
  const pv = await getJSON('/providers');
  const usable = ((pv && pv.items) || []).filter((x) => x.has_key).slice(0, 2);
  if (usable.length < 2) { addSys(lang() === 'zh' ? '要两家可用模型' : 'need two models'); return; }
  await postJSON('/chat/note', {who: 'me', text: topic});
  const seats = usable.map((x) => ({provider: x.name}));
  const r = await postJSON('/debate/start', {mode: 'debate', topic, seats, rounds: 2});
  if (!r || !r.ok) { addSys((lang() === 'zh' ? '起不来：' : 'failed: ') + ((r && r.error) || '')); return; }
  debateJob = r.id; debateSeen = 0; localStorage.setItem('aster.debateJob', r.id);
  if (!document.getElementById('debateSay')) {
    const box = document.createElement('div');
    box.id = 'debateSay';
    box.style.cssText = 'margin:0 14px 8px;padding:9px 12px;background:#fff;border:1px solid var(--line2);' +
      'border-radius:12px;display:flex;gap:8px';
    box.innerHTML = '<input id="dsInput" style="flex:1;border:0;outline:none;font-size:13px" placeholder="' +
      (lang() === 'zh' ? '插一句：下一轮他们必须回应' : 'say something they must answer') + '">' +
      '<button class="send" id="dsGo" style="height:28px">' + (lang() === 'zh' ? '送到' : 'send') + '</button>';
    $('.compose').parentElement.insertBefore(box, $('.compose'));
    box.querySelector('#dsGo').onclick = async () => {
      const v = box.querySelector('#dsInput').value.trim();
      if (!v || !debateJob) return;
      await postJSON('/debate/say', {id: debateJob, text: v});
      box.querySelector('#dsInput').value = '';
      addSys(lang() === 'zh' ? '（你插了一句：' + v + '）' : '(you: ' + v + ')');
    };
  }
  addSys((lang() === 'zh' ? '开局 · ' : 'started · ') + usable.map((x) => x.label).join(' / ') +
    ' · <a data-stop-debate="' + r.id + '" style="color:var(--pink);cursor:pointer">' +
    (lang() === 'zh' ? '叫停' : 'stop') + '</a>', true);
  if (!debateTimer) debateTimer = setInterval(pollDebate, 2500);
  pollDebate();
}
async function pollDebate() {
  if (!debateJob) return;
  const st = await getJSON('/debate/status?id=' + debateJob);
  if (!st || !st.ok) return;
  (st.log || []).slice(debateSeen).forEach((x) => {
    debateSeen++;
    if (x.kind === 'speak') addSys('【第' + x.round + '轮 · ' + plabel(x.who) + '】' + (x.text || '').slice(0, 200));
    else if (x.kind === 'note') addSys(lang() === 'zh' ? '（你插了一句：' + x.text + '）' : '(you: ' + x.text + ')');
  });
  if (st.state === 'running') return;
  clearInterval(debateTimer); debateTimer = null;
  const r = st.result || {};

  if (r.verdict) addMsg(r.verdict, 'her');
  addSys((lang() === 'zh' ? '结束 · ' : 'done · ') + (r.calls || 0) + (lang() === 'zh' ? ' 次调用 · ¥' : ' calls · ¥') + Number(r.cost || 0).toFixed(4));
  debateJob = null; localStorage.removeItem('aster.debateJob');
  const b = document.getElementById('debateSay'); if (b) b.remove();
}
document.addEventListener('click', (e) => {
  const d = e.target && e.target.dataset && e.target.dataset.stopDebate;
  if (d) { postJSON('/debate/stop', {id: d}); e.target.textContent = lang() === 'zh' ? '已叫停' : 'stopped'; }
});

let lastJobId = null;
let lastArtifact = null;
let lastTopic = '';
let pendingPlanJob = null;   // 出过方案、等一句「动手」   // {dir, files} 刚才干完的那件
document.addEventListener('click', async (e) => {
  const r = e.target && e.target.dataset && e.target.dataset.resumeJob;
  if (!r) return;
  const res = await postJSON('/code/resume', {id: r});
  e.target.textContent = res && res.ok ? (lang() === 'zh' ? '已继续' : 'resumed') : (lang() === 'zh' ? '起不来' : 'failed');
  if (res && res.ok) {
    workJob = r; pollWork._seen = 0;
    if (!workTimer) workTimer = setInterval(pollWork, 2000);
  }
});

async function pollWork() {
  if (!workJob) return;
  lastJobId = workJob;
  const st = await getJSON('/code/status?id=' + workJob);
  if (!st || !st.ok) return;
  const steps = st.steps || [];
  while ((pollWork._seen || 0) < steps.length) {
    const x = steps[pollWork._seen++];
    addSys('#' + x.n + ' ' + (x.role || '') + ' · ' + x.who +
      (x.digest ? ' · ' + x.digest : '') + ' · ' +
      (((x.tokens || {}).in || 0) + ((x.tokens || {}).out || 0)) + ' tokens');
  }
  if (st.stage === 'plan') pendingPlanJob = workJob;
  const gb = document.getElementById('goBox');
  if (gb && st.stage !== 'plan') gb.remove();
  if (st.state === 'interrupted') {
    clearInterval(workTimer); workTimer = null;
    addSys((lang() === 'zh' ? '这件被中断了，步骤都在上面 · ' : 'interrupted · ') +
      '<a data-resume-job="' + lastJobId + '" style="color:var(--pink);cursor:pointer">' +
      (lang() === 'zh' ? '继续跑' : 'resume') + '</a>', true);
    workJob = null; return;
  }
  if (st.state !== 'running') {
    clearInterval(workTimer); workTimer = null;
    const tk = st.tokens || {};
    addSys((lang() === 'zh' ? '做完了 · ' : 'done · ') + steps.length + (lang() === 'zh' ? ' 步 · ' : ' steps · ') +
      ((tk.in || 0) + (tk.out || 0)) + ' tokens · ¥' + Number(st.cost || 0).toFixed(4) +
      (st.stop_reason ? ' · ' + st.stop_reason : ''));
    if ((st.files || []).length) {
      lastArtifact = {dir: st.workdir, files: st.files};
      addSys((lang() === 'zh' ? '文件在这儿：' : 'files: ') + st.workdir + '\n　· ' + st.files.join('\n　· '));
    }
    if ((st.plan || []).length) {
      addMsg((lang() === 'zh' ? '方案清单（勾选在「编码」页）\n' : 'Plan (see Coding)\n') +
        st.plan.map((x, i) => (i + 1) + '. ' + x.text).join('\n'), 'her');
    }
    workJob = null; pollWork._seen = 0;
  }
}

/* ---- 朗读：把一条回复念出来 ---- */
let curAudio = null;
async function speak(text) {
  if (!text) return;
  const base = await findAPI();
  const r = await fetch(base + '/tts?text=' + encodeURIComponent(text.slice(0, 300))).then((x) => x.json()).catch(() => null);
  if (!r || !r.ok || !r.path) { addSys(lang() === 'zh' ? '念不出来' : 'tts failed'); return; }
  if (curAudio) { try { curAudio.pause(); } catch (e) {} }
  curAudio = new Audio(base + '/audio?f=' + encodeURIComponent(r.path));
  curAudio.play().catch(() => addSys(lang() === 'zh' ? '播放被拦' : 'play blocked'));
}
document.addEventListener('click', (e) => {
  const b = e.target.closest && e.target.closest('[data-speak]');
  if (b) { speak(b.dataset.speak); return; }
  const last = e.target.closest && e.target.closest('#speakLast');
  if (last) {
    const nodes = document.querySelectorAll('#log .t.her p');
    if (nodes.length) speak(nodes[nodes.length - 1].textContent);
  }
});

/* ---- 消息 ---- */
function nowHM() {
  const d = new Date();
  return String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
}
function saveChat(who, text, at) {
  try {
    const k = 'aster.chat';
    const list = JSON.parse(localStorage.getItem(k) || '[]');
    list.push({who, text, at});
    localStorage.setItem(k, JSON.stringify(list.slice(-200)));
  } catch (e) {}
}

function addMsg(text, who) {
  const el = document.createElement('div');
  el.className = 't ' + who;
  el.innerHTML = '<p></p><span class="at"></span>';
  el.querySelector('p').textContent = text;
  el.querySelector('.at').textContent = nowHM();
  if (who === 'her' && text) {
    const sp = document.createElement('a');
    sp.dataset.speak = text.slice(0, 300);
    sp.textContent = lang() === 'zh' ? '朗读' : 'Read';
    sp.style.cssText = 'margin-left:8px;color:var(--dim);cursor:pointer;font-size:10px';
    el.querySelector('.at').appendChild(sp);
  }
  $('#log').appendChild(el);
  if (!el.dataset.quiet) saveChat(who, text, el.querySelector('.at').textContent);
  $('#log').scrollTop = $('#log').scrollHeight;
  $('#emptyBlock').style.display = 'none';
  return el;
}
function addSys(text, html) {
  const el = document.createElement('p');
  el.className = 'sys';
  if (html) el.innerHTML = nowHM() + ' · ' + text; else el.textContent = nowHM() + ' · ' + text;
  $('#log').appendChild(el);
}

/* ---- 发消息 ---- */
/* 先想一句：这句话到底是在问什么。只用来把判断说出口，好让人看到她在想。 */
function thinkLine(t) {
  const x = (t || '').trim();
  if (!x) return '';
  if (/(比价|价格|多少钱|便宜|哪个合适|推荐哪个|航班|机票|几点|时间段)/.test(x)) return '这是在问行情，我得去查，不能凭印象说。';
  if (/(怎么做|怎么办|为什么|是不是|你觉得|要不要|好不好)/.test(x)) return '这是在问我的看法，我直接说判断。';
  if (/(好|嗯|行|可以|收到|谢谢)/.test(x) && x.length <= 6) return '这是在应我，接一句就好。';
  if (/(做|写|改|建|生成|开发|搞).*(一个|个|一份|一下|套)/.test(x)) return '这是要我动手，先出方案再干。';
  if (/(累|困|烦|难受|开心|高兴|害怕|焦虑)/.test(x)) return '他这会儿在说自己的状态，先接住情绪。';
  return '先当聊天接，别急着动工具。';
}

async function send() {
  const t = $('#text').value.trim();
  if (!t && !pending) return;
  let ask = t;
  if (pending) {
    ask = (t ? t + '\n\n' : '') + '【' + (lang() === 'zh' ? '我发来的文件' : 'file') + '：' + pending.name + '】\n' + (pending.text || '');
    pending = null; renderFile();
  }
  addMsg(t || ('[' + (lang() === 'zh' ? '文件' : 'file') + ']'), 'me');
  $('#text').value = ''; $('#text').style.height = '34px';
  // 先想一句：优先让本地小模型判，判不出再退回规则
  (async () => {
    let line = '';
    try {
      const base = await findAPI();
      const d = await fetch(base + '/think', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: t, last: window.__lastTopic || '' }) }).then((x) => x.json());
      line = (d && d.ok && d.say) ? d.say : '';
    } catch (e) {}
    addSys((lang() === 'zh' ? '在想：' : 'thinking: ') + (line || thinkLine(t)));
  })();
  if (!workMode && mode3 === 'chat' && t && looksLikeWork(t)) {
    // 像「做点东西」的请求：不装作排上了，先出方案
    addSys(lang() === 'zh' ? '这像是要我动手，先出方案' : 'looks like real work — plan first');
    mode3 = 'work'; workMode = true; localStorage.setItem('aster.mode', 'work'); paintMode();
    runWork(t); return;
  }
  if (/^(预览|打开预览|把预览打开|打开侧边|把页面打开)/.test(t || '')) {
    addMsg(t, 'me'); $('#text').value = '';
    openPreview(); addSys(lang() === 'zh' ? '预览开着了' : 'preview opened');
    return;
  }
  const shoppingQuery = /(比价|比一下价|便宜|哪个平台|多少钱|价格)/.test(t || '') && !/https?:\/\//.test(t || '');
  if (shoppingQuery) {
    addMsg(t, 'me'); $('#text').value = '';
    const kw = searchWords(t);
    openPreview('https://www.baidu.com/s?wd=' + encodeURIComponent(kw + ' 价格'));
    addSys(lang() === 'zh' ? '去搜一下，我读给你' : 'searching…');
    return;
  }
  const linkFound = (t || '').match(/https?:\/\/[^\s，。]+/);
  if (linkFound && /(比价|多少钱|价格|贵不贵|值不值)/.test(t || '')) {
    addMsg(t, 'me'); $('#text').value = '';
    addSys(lang() === 'zh' ? '用它自己的内核打开页面…' : 'opening page…');
    openPreview(linkFound[0]);
    let r0 = await fetchByWebview(linkFound[0], 15000);
    if (!r0 || !r0.ok) {
      const base0 = await findAPI();
      const r1 = await fetch(base0 + '/price?url=' + encodeURIComponent(linkFound[0])).then((x) => x.json()).catch(() => null);
      if (r1 && r1.ok) r0 = r1;
    }
    if (r0 && r0.ok) {
      addMsg((r0.title ? r0.title + '\n' : '') + (lang() === 'zh' ? '页面上的价格：' : 'prices: ') +
        r0.prices.map((x) => '¥' + x).join(' / '), 'her');
    } else {
      addMsg(lang() === 'zh' ? ('读不到价格：' + ((r0 && r0.error) || '页面打不开')) : ('cannot read price'), 'her');
    }
    return;
  }
  const verbHit = /(打开|放桌面|放到桌面|发到桌面|念一遍|读一遍|再来一版|重做一版|继续跑)/.test(t);
  const pointsToIt = /(它|这个|那个|刚才|刚刚|这份|那份|上面)/.test(t);
  const short = (t || '').length <= 30;
  if (/^(动手|开始做|开干|做吧|搞吧|可以了)/.test(t || '')) {
    addMsg(t, 'me'); $('#text').value = '';
    if (pendingPlanJob) {
      await postJSON('/code/go', {id: pendingPlanJob});
      addSys(lang() === 'zh' ? '放行，开始写' : 'go');
      workJob = pendingPlanJob; pendingPlanJob = null; pollWork._seen = 0;
      if (!workTimer) workTimer = setInterval(pollWork, 2000);
    } else if (lastTopic) {
      workMode = true; mode3 = 'work'; paintMode();
      runWork(lastTopic);
    } else {
      addSys(lang() === 'zh' ? '还没说要做什么' : 'nothing to work on');
    }
    return;
  }
  if (/^(出个方案|先出方案|给个方案)/.test(t || '')) {
    addMsg(t, 'me'); $('#text').value = '';
    if (lastTopic) { workMode = true; mode3 = 'work'; paintMode(); runWork(lastTopic); }
    return;
  }
  if (verbHit) {
    // 先在「做过的所有东西」里按关键词找，找不到再退回最近那件
    const list = await getJSON('/artifacts');
    const items = (list && list.items) || [];
    const words = (t || '').replace(/[打开|放桌面|放到桌面|念一遍|读一遍|再来一版|的|那个|之前|上次|刚才|这件|那份]/g, ' ')
      .split(/\s+/).filter((w) => w.length >= 2);
    let hit = null;
    if (words.length) {
      let best = 0;
      items.forEach((it) => {
        const hay = (it.task || '') + ' ' + (it.files || []).join(' ');
        let sc = 0;
        words.forEach((w) => { if (hay.includes(w)) sc++; });
        if (sc > best) { best = sc; hit = it; }
      });
    }
    if (!hit && /之前|上次|前面|那个|上一次/.test(t || '')) {
      const cand = await getJSON('/artifacts');
      const its = (cand && cand.items) || [];
      if (its.length) {
        addSys(lang() === 'zh' ? '我这边做过这些，点一个：' : 'pick one:');
        its.slice(0, 5).forEach((it) => addSys(it.task + ' · ' + (it.files || []).join('、')));
        return;
      }
    }
    if (hit) {
      addMsg(t, 'me'); $('#text').value = '';
      if (/念一遍|读一遍/.test(t)) {
        const base2 = await findAPI();
        const r2 = await fetch(base2 + '/file/text?dir=' + encodeURIComponent(hit.workdir) +
          '&name=' + encodeURIComponent((hit.files || [])[0] || '')).then((x) => x.json()).catch(() => null);
        addMsg((r2 && r2.text) ? r2.text.slice(0, 1200) : (lang() === 'zh' ? '读不出来' : 'cannot read'), 'her');
        return;
      }
      const r3 = await postJSON('/open/last', {id: hit.id});
      addMsg(r3 && r3.ok
        ? (lang() === 'zh' ? '给你放桌面并打开了：' : 'opened: ') + (r3.path || '')
        : (lang() === 'zh' ? '没能打开：' : 'failed: ') + ((r3 && r3.error) || ''), 'her');
      return;
    }
  }
  if (verbHit && lastArtifact && (pointsToIt || short)) {
    addMsg(t, 'me'); $('#text').value = '';
    if (/念一遍|读一遍/.test(t)) {
      const base = await findAPI();
      const r = await fetch(base + '/file/text?dir=' + encodeURIComponent(lastArtifact.dir) +
        '&name=' + encodeURIComponent(lastArtifact.files[0])).then((x) => x.json()).catch(() => null);
      addMsg((r && r.text) ? r.text.slice(0, 1200) : (lang() === 'zh' ? '读不出来' : 'cannot read'), 'her');
      return;
    }
    if (/再来一版|重做一版/.test(t)) {
      addSys(lang() === 'zh' ? '换一版重做' : 'redo'); runWork('再来一版：' + (lastArtifact.files[0] || '')); return;
    }
    if (/继续跑/.test(t)) { postJSON('/code/resume', {id: lastJobId}); return; }
    const r = await postJSON('/open/last', {});
    addMsg(r && r.ok
      ? (lang() === 'zh' ? '已经放到桌面并打开了：' : 'copied and opened: ') + r.path
      : (lang() === 'zh' ? '没找到可打开的成果' : 'nothing to open'), 'her');
    return;
  }
  if (t && /(打开|放桌面|放到桌面|发到桌面)/.test(t)) {
    addMsg(t, 'me');
    $('#text').value = '';
    const r = await postJSON('/open/last', {});
    if (r && r.ok) {
      addMsg((lang() === 'zh' ? '已经放到桌面并打开了：' : 'copied to Desktop and opened: ') + r.path, 'her');
      addSys((lang() === 'zh' ? '共 ' : '') + (r.count || 1) + (lang() === 'zh' ? ' 个文件' : ' files'));
    } else {
      addMsg((lang() === 'zh' ? '没找到可打开的成果：' : 'nothing to open: ') + ((r && r.error) || ''), 'her');
    }
    return;
  }
  // 说“加进行程”就直接记一条，不再绕任务队列
  // 日常里顺口说的习惯，记下来，之后做决定时带进提示
  const habits = [
    [/(?:我家|家里|家)?\s*(?:到|去)\s*(?:机场|车站|高铁站)[^0-9]{0,6}(\d{1,3})\s*分钟/, '到机场'],
    [/(\d{1,2})\s*点(?:左右)?\s*(?:起床|起)/, '起床'],
    [/(\d{1,2})\s*点(?:左右)?\s*(?:睡|睡觉|上床)/, '睡觉'],
    [/(?:不吃|忌口|不能吃)\s*([\u4e00-\u9fa5]{1,8})/, '忌口'],
    [/(?:我|本人)\s*(?:喜欢|爱|偏好)\s*([\u4e00-\u9fa5]{1,10})/, '偏好'],
    [/(?:我|本人)\s*(?:对|吃)\s*([\u4e00-\u9fa5]{1,6})\s*过敏/, '过敏']
  ];
  let _hit = null;
  for (const [re, key] of habits) {
    const m = t.match(re);
    if (m) { _hit = [key, m[1]]; break; }
  }
  if (_hit) {
    (async () => {
      const base = await findAPI();
      await fetch(base + '/habit/set', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ key: _hit[0], value: _hit[1] }) }).catch(() => null);
    })();
  }
  // 他说的“从家到机场多少分钟”要记住，之后行程提醒按这个提前
  const tm0 = t.match(/(?:我家|家里|家)?\s*(?:到|去)\s*(?:机场|车站|高铁站)[^0-9]{0,6}(\d{1,3})\s*分钟/);
  if (tm0) {
    (async () => {
      const base = await findAPI();
      await fetch(base + '/trip/travel', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ minutes: parseInt(tm0[1], 10) }) }).catch(() => null);
      addMsg((lang() === 'zh' ? '记住了，' + tm0[1] + ' 分钟。以后有去机场的行程，我按这个提前叫你。'
                              : 'noted: ' + tm0[1] + ' minutes.'), 'her');
    })();
    return;
  }
  if (/(加入|记进|记到|记一下|放到).{0,3}行程|行程.{0,4}(加入|记下|记一下)/.test(t)) {
    const tm = (t.match(/(今天|明天|后天)?\s*\d{1,2}[:：]\d{2}/) || [''])[0].trim();
    const what = t.replace(/(帮我|给我)?(把|将)?[^，。]*?(加入|记进|记到|记一下|放到)[^，。]*行程[，。]?/, '')
                  .replace(/^(帮我|给我)/, '').trim() || t;
    (async () => {
      const base = await findAPI();
      const r = await fetch(base + '/trip/add', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ what, when: tm }) }).then((x) => x.json()).catch(() => null);
      addMsg((r && r.ok)
        ? (lang() === 'zh' ? '记好了，到点我叫你。' : 'saved.')
        : (lang() === 'zh' ? '没记上，去「行程」页手动填一下。' : 'could not save'), 'her');
    })();
    return;
  }
  if (workMode && t) {
    // 只有真的在吩咐活儿才继续走工作流；不然就退回聊天，别把闲聊当任务
    if (looksLikeWork(t)) { runWork(t); return; }
    workMode = false; mode3 = 'chat'; localStorage.setItem('aster.mode', 'chat'); paintMode();
  }
  if (mode3 === 'debate' && t) { runDebate(t); return; }
  const t0 = performance.now();
  let d = null;
  for (let _try = 0; _try < 2; _try++) {
    d = await postJSON('/chat', {text: ask, channel: 'desktop'}).catch(() => null);
    if (d && d.ok) break;
    await new Promise((r) => setTimeout(r, 900));
  }
  const ms = Math.round(performance.now() - t0);
  latencies.push(ms);
  if (d && d.ok && d.reply) {
    // 想起：只报这一轮真塞进提示的那一条，不再单独请求 /recall
    if (d.recalled && d.recalled.length) {
      addSys((lang() === 'zh' ? '想起：' : 'recalled: ') + String(d.recalled[0]).slice(0, 40));
    }
    addMsg(d.reply, 'her');
    // 2026-11-22：这里原来写的是 t('local')，可 send() 里 const t = 输入框内容，
    // 把 i18n 的 t 遮住了，每回完一句就抛 "t is not a function"，下面的 loadRail() 也跟着不跑。
    $('#modeLine').textContent = (d.need === 'local_ok' ? window.t('local') : '') + ' · ' + (ms / 1000).toFixed(1) + 's';
    $('#statLatency').textContent = (latencies.reduce((a, b) => a + b, 0) / latencies.length / 1000).toFixed(1) + 's';
    if (d.memory_hits) addSys((lang() === 'zh' ? '命中记忆 ' : 'memory hits ') + d.memory_hits);
  } else {
    addMsg(lang() === 'zh' ? '引擎没应声，稍后再试' : 'No answer from the engine', 'her');
  }
  loadRail();
}
$('#sendBtn').onclick = send;
$('#text').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
});
$('#text').addEventListener('input', (e) => {
  e.target.style.height = '34px';
  e.target.style.height = Math.min(120, e.target.scrollHeight) + 'px';
});

/* ---- 新会话：清空当前视图，并在引擎里开一条新线 ---- */
$('#newBtn').onclick = async () => {
  await postJSON('/session/new', {});
  const log = $('#log');
  log.innerHTML = '<p class="day" id="dayLine"></p>';
  paintDate();
  $('#emptyBlock').style.display = 'block';
  addSys(lang() === 'zh' ? '已开新会话' : 'new session');
};

/* ---- 记录有误：就地写一句更正，交给引擎 ---- */
$('#wrongBtn').onclick = () => {
  const box = document.createElement('div');
  box.style.cssText = 'margin:0 14px 8px;padding:10px 12px;background:#fff;border:1px solid var(--line2);' +
    'border-radius:12px;display:flex;gap:8px';
  box.innerHTML = '<input id="fixInput" style="flex:1;border:0;outline:none;font-size:13px" ' +
    'placeholder="' + (lang() === 'zh' ? '哪里记错了？写一句，我来改' : 'What was wrong? One line, I will fix it') + '">' +
    '<button class="send" id="fixGo" style="height:28px">' + (lang() === 'zh' ? '提交' : 'Send') + '</button>';
  $('.compose').appendChild(box);
  box.querySelector('#fixInput').focus();
  box.querySelector('#fixGo').onclick = async () => {
    const v = box.querySelector('#fixInput').value.trim();
    if (!v) return box.remove();
    await postJSON('/correction', {text: v});
    box.remove();
    addSys(lang() === 'zh' ? '记下了这条更正' : 'correction noted');
  };
};

/* ---- 附件 ---- */
function renderFile() {
  const row = $('#fileRow');
  if (!pending) { row.style.display = 'none'; return; }
  row.style.display = 'flex';
  $('#fileName').textContent = pending.name;
  $('#fileMeta').textContent = pending.chars ? '· ' + pending.chars + (lang() === 'zh' ? ' 字' : ' chars') : '';
}
$('#fileX').onclick = () => { pending = null; renderFile(); };
async function upload(file) {
  const b64 = await new Promise((res, rej) => {
    const fr = new FileReader(); fr.onload = () => res(String(fr.result)); fr.onerror = rej;
    fr.readAsDataURL(file);
  });
  const up = await postJSON('/upload', {name: file.name, data: b64});
  if (!up || !up.ok) { addSys((lang() === 'zh' ? '文件没读成：' : 'Upload failed: ') + (up && up.error || '')); return; }
  pending = {name: up.name, text: up.text || '', chars: up.chars || 0};
  renderFile();
  $('#text').focus();
}
['dragover', 'drop'].forEach((ev) => $('.chat').addEventListener(ev, (e) => {
  e.preventDefault();
  if (ev === 'drop' && e.dataTransfer.files[0]) upload(e.dataTransfer.files[0]);
}));
$('#fileBtn').onclick = () => {
  const i = document.createElement('input'); i.type = 'file';
  i.onchange = () => i.files[0] && upload(i.files[0]); i.click();
};
$('#picBtn').onclick = () => {
  const i = document.createElement('input'); i.type = 'file'; i.accept = 'image/*';
  i.onchange = () => i.files[0] && upload(i.files[0]); i.click();
};
/* 按住说话：录一段 → 引擎本地转写 → 填进输入框 */
let rec = null, chunks = [], recording = false;
async function startRec() {
  if (recording) return;
  try {
    const stream = await navigator.mediaDevices.getUserMedia({audio: true});
    rec = new MediaRecorder(stream); chunks = []; recording = true;
    rec.ondataavailable = (e) => e.data.size && chunks.push(e.data);
    rec.onstop = async () => {
      recording = false;
      stream.getTracks().forEach((t) => t.stop());
      const blob = new Blob(chunks, {type: 'audio/webm'});
      $('#micBtn').style.opacity = .5;
      const base = await findAPI();
      try {
        const r = await fetch(base + '/asr', {method: 'POST', headers: {'Content-Type': 'audio/webm'}, body: blob});
        const d = await r.json();
        if (d && d.ok && d.text) {
          const ta = $('#text');
          ta.value = (ta.value ? ta.value + ' ' : '') + d.text;
          ta.focus();
        } else {
          addSys((lang() === 'zh' ? '没听清：' : 'did not catch: ') + ((d && d.error) || ''));
        }
      } catch (e) {
        addSys(lang() === 'zh' ? '转写失败' : 'transcription failed');
      }
      $('#micBtn').style.opacity = 1;
      $('#micBtn').style.background = '';
    };
    rec.start();
    $('#micBtn').style.color = 'var(--pink)';
    $('#micBtn').style.background = '#fdeef0';
    addSys(lang() === 'zh' ? '在听… 再点一下结束' : 'listening… tap again to stop');
  } catch (e) {
    addSys(lang() === 'zh' ? '没有麦克风权限' : 'microphone not allowed');
  }
}
function stopRec() {
  if (rec && recording) { rec.stop(); $('#micBtn').style.color = ''; }
}
$('#micBtn').onclick = () => {
  if (recording) { stopRec(); return; }
  startRec();
};
/* ---- 右栏 ---- */
const GAUGES = [
  ['energy', '活力', 'Energy'], ['attachment', '接近', 'Closeness'], ['softness', '柔软', 'Softness'],
  ['desire', '想念', 'Longing'], ['tension', '张力', 'Tension'], ['loneliness', '孤独', 'Lonely'],
  ['curiosity', '好奇', 'Curious'], ['grievance', '委屈', 'Hurt']
];
function paintGauges(st) {
  const list = expanded ? GAUGES : GAUGES.slice(0, 4);
  let max = 0, maxKey = '';
  list.forEach(([k]) => { const v = Number(st[k] || 0); if (v > max) { max = v; maxKey = k; } });
  $('#gauges').innerHTML = list.map(([k, zh, en]) => {
    const v = Number(st[k] || 0);
    return '<div class="g' + (k === maxKey ? ' hot' : '') + '"><span>' + (lang() === 'zh' ? zh : en) +
      '</span><i><b style="width:' + Math.round(v * 100) + '%"></b></i><em>' + v.toFixed(2) + '</em></div>';
  }).join('');
  $('#moreBtn').textContent = expanded ? t('less') : t('all8');
}
$('#moreBtn').onclick = () => { expanded = !expanded; loadRail(); };

async function loadRail() {
  const st = await getJSON('/state');
  if (st && st.states) {
    const s = st.states;
    paintGauges({energy: s['能量'], attachment: s['黏'], softness: s['软'], desire: s['欲'],
      tension: s['醋'], loneliness: s['孤独'], curiosity: s['好奇'], grievance: 0});
  }
  const rel = await getJSON('/relation');
  if (rel && rel.ok) {
    const v = Math.max(0, Math.min(100, Number(rel.intimacy) || 0));
    const bar = $('#relBar'), num = $('#relNum'), row = $('#relRow');
    if (bar) bar.style.width = v + '%';
    if (num) num.textContent = rel.tier_name + ' ' + Math.round(v);
    if (row) row.title = (lang() === 'zh' ? '亲近度 ' : 'Bond ') + v + '/100';
  }
  const mood = await getJSON('/mood');
  if (mood && mood.mood) $('#nowLine').textContent = mood.mood;
  const tasks = await getJSON('/task/list');
  if (tasks && tasks.items) {
    const recent = tasks.items.slice(-2).reverse();
    $('#tasks').innerHTML = recent.length ? recent.map((x, i) =>
      '<div class="w' + (x.status === 'running' ? ' run' : '') + '"><span class="d"></span><div><b>' +
      (x.goal || '').slice(0, 18) + '</b>' + (x.status === 'running' ? (lang() === 'zh' ? '正在做' : 'running')
      : (lang() === 'zh' ? '排队中' : 'queued')) + '</div></div>').join('')
      : '<div class="w"><span class="d"></span><div>' + (lang() === 'zh' ? '暂无任务' : 'nothing queued') + '</div></div>';
    const done = tasks.items.filter(x => x.status === 'done').length;
    const blocked = tasks.items.filter(x => x.status === 'blocked').length;
    $('#statCompleted').textContent = done + (lang() === 'zh' ? ' 件' : '');
    $('#statBlocked').textContent = blocked + (lang() === 'zh' ? ' 件' : '');
  }
  const mem = await getJSON('/memory');
  if (mem && mem.items) {
    const recent = mem.items.slice(-2).reverse();
    $('#memos').innerHTML = recent.map((m) => '<div class="memo"><span>' +
      new Date((m.ts || 0) * 1000).toLocaleTimeString('zh-CN', {hour: '2-digit', minute: '2-digit'}) +
      '</span>' + String(m.text || '').slice(0, 40) + '</div>').join('')
      || '<div class="memo">' + (lang() === 'zh' ? '还没记录什么' : 'nothing yet') + '</div>';
    $('#statRecorded').textContent = mem.items.length + (lang() === 'zh' ? ' 条' : '');
  }
  const fc = await getJSON('/face');
  if (fc && fc.ok && fc.path) {
    const base2 = await findAPI();
    const img = document.getElementById('faceImg');
    if (img) img.src = base2 + '/img?f=' + encodeURIComponent(fc.path) + '&t=' + Date.now();
    const cap2 = document.getElementById('faceCap');
    if (cap2 && fc.mood) cap2.textContent = (lang() === 'zh' ? '表情 · ' : 'Expression · ') + String(fc.mood).slice(0, 10);
  }
  const me = await getJSON('/selfcheck');
  if (me && me.model) $('#modelLine').textContent = (lang() === 'zh' ? '本地 · ' : 'local · ') + 'qwen2.5:7b';
}

/* ---- 空态建议 ---- */
const IDEAS = {
  zh: ['将桌面文件按日期整理', '阅读该 PDF 的第三节', '于七时提醒我'],
  en: ['Sort the Desktop files by date', 'Read section 3 of that PDF', 'Wake me at seven']
};
function paintIdeas() {
  $('#emptyChips').innerHTML = IDEAS[lang()].map((x) => '<span class="chip">' + x + '</span>').join('');
  $('#emptyChips').querySelectorAll('.chip').forEach((el) => {
    el.onclick = () => { $('#text').value = el.textContent; $('#text').focus(); };
  });
}

/* ---- 启动 ---- */
(async function boot() {
  
  applyNames(); wireFacePick();
  setInterval(pollProactive, 120000); setTimeout(pollProactive, 20000);
  paintLang(); paintMusic(); paintIdeas(); paintDate(); paintPerm(); paintMode(); loadRail();
  // 2026-11-22：右栏以前只在开机和发完消息时刷一次，激素自己在变也看不见。
  // 现在每 25 秒拉一次 /state，窗口重新拿到焦点也立刻刷。
  setInterval(loadRail, 25000);
  window.addEventListener('focus', () => loadRail());
  const lastDebate = localStorage.getItem('aster.debateJob');
  if (lastDebate) {
    debateJob = lastDebate; debateSeen = 0;
    const sd = await getJSON('/debate/status?id=' + lastDebate);
    if (sd && sd.ok) {
      (sd.log || []).forEach((x) => {
        debateSeen++;
        if (x.kind === 'speak') addSys('【第' + x.round + '轮 · ' + plabel(x.who) + '】' + (x.text || '').slice(0, 200));
      });
      if (sd.state === 'running') { if (!debateTimer) debateTimer = setInterval(pollDebate, 2500); }
      else { debateJob = null; localStorage.removeItem('aster.debateJob'); }
    }
  }
  const lastJob = localStorage.getItem('aster.workJob');
  if (lastJob) {
    workJob = lastJob; pollWork._seen = 0;
    const st0 = await getJSON('/code/status?id=' + lastJob);
    if (st0 && st0.ok) {
      const line = document.createElement('p');
      line.className = 'sys';
      line.textContent = (lang() === 'zh' ? '接着上次那件：' : 'resuming: ') + (st0.task || '');
      $('#log').appendChild(line);
      if (st0.state === 'running') {
        if (!workTimer) workTimer = setInterval(pollWork, 2000);
        pollWork();
      } else {
        (st0.steps || []).forEach((x) => addSys('#' + x.n + ' ' + (x.role || '') + ' · ' + x.who +
          (x.digest ? ' · ' + x.digest : '')));
        addSys((lang() === 'zh' ? '上次已结束 · ' : 'done · ') + (st0.cost || 0) + ' 元');
      }
    }
  }
  $('#modeLine').textContent = t('local') + ' · —';
  const hist = await getJSON('/history');
  if (hist && hist.items && hist.items.length) {
    hist.items.slice(-40).forEach((m) => {
      const who = (m.who || (m.role === 'user' ? 'me' : 'her')) === 'me' ? 'me' : 'her';
      const text = m.text || m.content || '';
      if (!text) return;
      const el = document.createElement('div');
      el.className = 't ' + who;
      el.dataset.quiet = '1';
      el.innerHTML = '<p></p><span class="at"></span>';
      el.querySelector('p').textContent = text;
      el.querySelector('.at').textContent = m.ts
        ? new Date(m.ts * 1000).toTimeString().slice(0, 5) : '';
      $('#log').appendChild(el);
    });
    $('#log').scrollTop = $('#log').scrollHeight;
    $('#emptyBlock').style.display = 'none';
  }
  const face = await getJSON('/face');
  if (face && face.file) $('#faceImg').src = (await findAPI()) + '/img?f=' + encodeURIComponent(face.file);
})();
