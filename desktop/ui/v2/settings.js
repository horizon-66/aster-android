const $ = (s) => document.querySelector(s);
const seg = (id) => document.querySelectorAll('#' + id + ' button');

function paint() {
  document.documentElement.lang = lang() === 'zh' ? 'zh-CN' : 'en';
  applyI18n();
  seg('langSeg').forEach((b) => b.classList.toggle('on', b.dataset.l === lang()));
  seg('musicSeg').forEach((b) => b.classList.toggle('on', b.dataset.m === (localStorage.getItem('aster.music') === '1' ? '1' : '0')));
}
seg('langSeg').forEach((b) => b.onclick = () => {
  setLang(b.dataset.l); paint(); location.reload();
});
seg('musicSeg').forEach((b) => b.onclick = () => {
  localStorage.setItem('aster.music', b.dataset.m); paint();
});

async function load() {
  const cl = await getJSON('/cloud/status');
  $('#cloudMsg').textContent = cl && cl.has_key
    ? (lang() === 'zh' ? '已配置密钥' : 'key set') : (lang() === 'zh' ? '尚未配置密钥' : 'no key yet');
  const rm = await getJSON('/router/model');
  if (rm && rm.current) $('#routerModel').value = rm.current;
  const pm = await getJSON('/permissions');
  const lv = (pm && (pm.level || pm.value)) || 2;
  seg('permSeg').forEach((b) => b.classList.toggle('on', Number(b.dataset.p) === Number(lv)));
  const health = await getJSON('/health');
  const up = !!(health && health.ok);
  $('#dEngine').textContent = up ? (lang() === 'zh' ? '在跑' : 'up') : (lang() === 'zh' ? '未起' : 'down');
  $('#dModel').textContent = ((rm && rm.current) || 'qwen2.5:7b').replace('qwen2.5:', '');
  $('#dPerm').textContent = ({1: (lang() === 'zh' ? '只读' : 'read'), 2: (lang() === 'zh' ? '工作区' : 'work'),
    3: (lang() === 'zh' ? '完全' : 'full')})[lv] || '—';
  $('#aboutLine').textContent = 'Aster · ' + (up ? (lang() === 'zh' ? '引擎进行中' : 'engine up') : (lang() === 'zh' ? '引擎没起' : 'engine down'));
}

$('#saveKey').onclick = async () => {
  const k = $('#cloudKey').value.trim();
  if (!k) return;
  const r = await postJSON('/cloud/key', {key: k});
  $('#cloudMsg').textContent = r && r.ok ? (lang() === 'zh' ? '已保存' : 'saved') : (lang() === 'zh' ? '保存失败' : 'failed');
  $('#cloudKey').value = '';
};
$('#routerModel').onchange = async (e) => {
  const r = await postJSON('/router/model', {model: e.target.value});
  $('#cloudMsg').textContent = r && r.ok ? (lang() === 'zh' ? '已切换为 ' + r.model : 'now ' + r.model) : '';
};
seg('permSeg').forEach((b) => b.onclick = async () => {
  const r = await postJSON('/permissions', {level: Number(b.dataset.p)});
  seg('permSeg').forEach((x) => x.classList.toggle('on', x === b));
  $('#permMsg').textContent = r && r.ok ? (lang() === 'zh' ? '已更新' : 'updated') : '';
});
$('#checkBtn').onclick = async () => {
  const r = await getJSON('/memory/check');
  $('#memMsg').textContent = r ? (r.summary || r.error) : '';
};
$('#fixBtn').onclick = async () => {
  if (!confirm(lang() === 'zh' ? '只去重、补时间、隔离坏条目，内容不改。继续？' : 'Dedupe, fix timestamps, quarantine bad rows only. Continue?')) return;
  const r = await postJSON('/memory/fix', {});
  $('#memMsg').textContent = r && r.ok ? (lang() === 'zh' ? '已修复' : 'repaired') : ((r && r.error) || '');
};
$('#exportBtn').onclick = async () => { location.href = (await findAPI()) + '/memory/export_all'; };
$('#importBtn').onclick = () => {
  const i = document.createElement('input'); i.type = 'file'; i.accept = '.zip';
  i.onchange = () => alert(lang() === 'zh' ? '请使用上方的「导入」按钮选择记忆包' : 'use the Import button above');
  i.click();
};
$('#backBtn').onclick = () => location.href = 'index.html';

mountTools({ lang: paint });
loadProviders();
paint(); load();

/* ---- 模型供应商 ---- */
let provs = [];
async function loadProviders() {
  const d = await getJSON('/providers');
  provs = (d && d.items) || [];
  renderProviders();
}
function renderProviders() {
  const zh = lang() === 'zh';
  let lastGroup = '';
  $('#provList').innerHTML = provs.map((p) => {
    const gHead = (p.group && p.group !== lastGroup)
      ? '<div style="margin:14px 0 4px;font-family:var(--mono);font-size:10px;letter-spacing:.14em;color:var(--dim)">' +
        (lastGroup = p.group) + '</div>' : '';
    const on = p.has_key;
    return gHead + '<div class="prov" data-name="' + p.name + '" style="display:flex;gap:8px;align-items:center;' +
      'padding:8px 0;border-top:1px solid #f4f1ec">' +
      '<span style="width:74px;font-size:12.5px;color:var(--ink)">' + (p.label || p.name) + '</span>' +
      '<input class="pkey" type="password" placeholder="' + (on ? (zh ? '已填，可重填' : 'set — enter to replace')
        : (zh ? '填 key' : 'paste key')) + '" style="flex:1;min-width:120px;background:#fff;' +
        'border:1px solid var(--line2);border-radius:9px;padding:7px 10px;font-size:12px">' +
      '<button class="psave" style="height:30px;padding:0 12px;border-radius:9px;border:0;background:var(--ink);' +
        'color:var(--paper);font-size:12px;cursor:pointer">' + (zh ? '保存' : 'Save') + '</button>' +
      '<input class="pmodel" placeholder="' + (p.model || (p.name === 'doubao' ? (zh ? '必填：ep-…' : 'required: ep-…')
          : (zh ? '必填：模型名' : 'required: model'))) + '" value="' +
        (p.model || '') + '" style="width:132px;background:#fff;border:1px solid ' +
        (p.model ? 'var(--line2)' : (p.name === 'local' ? 'var(--line2)' : 'rgba(232,23,61,.45)')) + ';' +
        'border-radius:9px;padding:7px 10px;font-size:12px">' +
      '<button class="ptest" style="height:30px;padding:0 12px;border-radius:9px;border:1px solid var(--line2);' +
        'background:#fff;color:var(--ink2);font-size:12px;cursor:pointer">' + (zh ? '试一下' : 'Test') + '</button>' +
      '<span class="pstate" style="width:64px;font-size:11px;color:' + (on ? '#5aa87a' : 'var(--dim)') + '">' +
        (on ? (zh ? '可用' : 'ready') : (zh ? '未填' : 'no key')) + '</span>' +
      '</div>';
  }).join('') || '<div class="msg">—</div>';

  document.querySelectorAll('.prov').forEach((row) => {
    const name = row.dataset.name;
    row.querySelector('.psave').onclick = async () => {
      const k = row.querySelector('.pkey').value.trim();
      if (!k) return;
      const mdl = (row.querySelector('.pmodel').value || '').trim();
      const r = await postJSON('/providers/key', {name, key: k, model: mdl});
      row.querySelector('.pkey').value = '';
      row.querySelector('.pstate').textContent = r && r.ok ? (lang() === 'zh' ? '已保存' : 'saved')
        : (lang() === 'zh' ? '保存失败' : 'failed');
      row.querySelector('.pstate').style.color = r && r.ok ? '#5aa87a' : 'var(--pink)';
      loadProviders();
    };
    row.querySelector('.ptest').onclick = async () => {
      // 先把手里的 key/模型存下来，省得用户忘了点保存
      const kIn = row.querySelector('.pkey');
      const mIn = row.querySelector('.pmodel');
      if (kIn.value.trim()) {
        await postJSON('/providers/key', {name, key: kIn.value.trim(), model: (mIn.value || '').trim()});
        kIn.value = '';
      } else if ((mIn.value || '').trim()) {
        const saved = provs.find((x) => x.name === name) || {};
        if (saved.has_key) await postJSON('/providers/key', {name, key: '__keep__', model: (mIn.value || '').trim()});
      }
      row.querySelector('.pstate').textContent = lang() === 'zh' ? '测试中…' : 'testing…';
      const r = await postJSON('/providers/test', {name});
      row.querySelector('.pstate').textContent = r && r.ok
        ? (lang() === 'zh' ? '连接正常' : 'ok') : (lang() === 'zh' ? '连接失败' : 'fail');
      row.querySelector('.pstate').style.color = r && r.ok ? '#5aa87a' : 'var(--pink)';
      row.querySelector('.pstate').title = (r && (r.error || r.text)) || '';
    };
  });
}

/* ---- 2026-11-22：把原来没接线的几块补上 ---- */
function fmt(n) { return String(n || 0).replace(/\B(?=(\d{3})+(?!\d))/g, ','); }

async function loadUsage() {
  try {
    const u = await getJSON('/usage');
    if (!u || !u.ok) { $('#usageBox').textContent = '—'; return; }
    const c1 = '¥' + Number(u.cost || 0).toFixed(2);
    const c7 = '¥' + Number(u.cost_week || 0).toFixed(2);
    $('#usageBox').innerHTML =
      (lang() === 'zh'
        ? '今日调用 <b>' + fmt(u.calls) + '</b> 次 · 输入 <b>' + fmt(u.prompt_tok) + '</b> tok · 输出 <b>' +
          fmt(u.out_tok) + '</b> tok · 平均 <b>' + (u.avg_ms || 0) + '</b> ms' +
          '<br>今日花费 <b>' + c1 + '</b> · 近七日 <b>' + c7 + '</b>' +
          '<br><span style="color:var(--dim);font-size:11.5px">按云端现价估算，实际以账单为准</span>'
        : 'Today: <b>' + fmt(u.calls) + '</b> calls · prompt <b>' + fmt(u.prompt_tok) + '</b> tok · out <b>' +
          fmt(u.out_tok) + '</b> tok · avg <b>' + (u.avg_ms || 0) + '</b> ms' +
          '<br>Today <b>' + c1 + '</b> · last 7 days <b>' + c7 + '</b>' +
          '<br><span style="color:var(--dim);font-size:11.5px">Estimated at current cloud pricing</span>');
  } catch (e) {}
}

async function loadBriefs() {
  try {
    const d = await getJSON('/briefs');
    const items = (d && d.briefs) || [];
    $('#briefBox').innerHTML = items.length
      ? items.slice(-6).reverse().map((x) => '<div style="padding:4px 0;border-top:1px solid #f4f1ec">' + x.text + '</div>').join('')
      : (lang() === 'zh' ? '暂无摘要。对话累积到一定长度并完成压缩后，将在此显示。' : 'No summaries yet.');
  } catch (e) {}
}

async function loadProactive() {
  try {
    const d = await getJSON('/proactive/state');
    const on = !!(d && d.enabled);
    $('#proToggle').textContent = on ? (lang() === 'zh' ? '关闭' : 'Off') : (lang() === 'zh' ? '开启' : 'On');
    $('#proMsg').textContent = on ? (lang() === 'zh' ? '将在合适时机主动发言' : 'may speak up')
      : (lang() === 'zh' ? '不会主动发言' : 'quiet');
  } catch (e) {}
}

async function loadPair() {
  try {
    const d = await getJSON('/pair/new');
    if (d && d.ok && d.qr) {
      $('#qrBox').innerHTML = '<img src="' + d.qr + '" style="width:160px;height:160px">';
      $('#pairState').textContent = lang() === 'zh' ? '等待手机扫描' : 'waiting for scan';
    }
    pollPair();
  } catch (e) {}
}

async function pollPair() {
  try {
    const st = await getJSON('/pair/status');
    if (st && st.paired) {
      $('#pairState').textContent = lang() === 'zh' ? '已连接：' + (st.url || '') : 'paired';
    }
  } catch (e) {}
  setTimeout(pollPair, 4000);
}

async function loadModels() {
  try {
    const h = await getJSON('/health');
    const list = (h && (h.available || h.models)) || [];
    const cur = (h && h.model) || '';
    $('#modelPick').innerHTML = list.map((m) =>
      '<option value="' + m + '"' + (m === cur ? ' selected' : '') + '>' + m + '</option>').join('')
      || '<option>（没发现本地模型）</option>';
  } catch (e) {}
}

function wireExtras() {
  $('#proToggle').onclick = async () => {
    const on = ($('#proToggle').textContent || '').indexOf(lang() === 'zh' ? '关闭' : 'Off') >= 0;
    const r = await fetch((window.API || '') + '/proactive/enable?on=' + (on ? '0' : '1')).then((x) => x.json()).catch(() => null);
    await loadProactive();
    $('#proMsg').textContent = (r && r.ok) ? (lang() === 'zh' ? '已更新' : 'updated') : '';
  };
  $('#pairRefresh').onclick = async () => { await fetch((window.API || '') + '/pair/new?refresh=1').catch(() => null); loadPair(); };
  const priv = $('#privBtn');
  async function paintPriv() {
    const box = $('#privBox');
    if (!box) return;
    const d = await getJSON('/private-net').catch(() => null);
    if (!d || !d.ok) { box.textContent = ''; return; }
    if (!d.installed) {
      box.innerHTML = (lang() === 'zh'
        ? '未检测到私有网络客户端。安装并在两台设备登录同一账号后，手机可在任何网络下连回这台电脑，地址固定、不经公网。'
        : 'No private-network client found.') +
        ' <a href="#" id="privGet" style="color:var(--pink)">' + (lang() === 'zh' ? '前往下载' : 'Download') + '</a>';
      const g = $('#privGet');
      if (g) g.onclick = (e) => { e.preventDefault(); window.open('https://tailscale.com/download/mac', '_blank'); };
      return;
    }
    if (!d.logged_in) {
      box.textContent = lang() === 'zh' ? '已安装，尚未登录或尚未连接。' : 'Installed, not logged in.';
      return;
    }
    box.innerHTML = (lang() === 'zh' ? '私有网络地址（固定）：' : 'Private address: ') +
      '<div style="word-break:break-all">' + d.url + '</div>' +
      (d.qr ? '<img src="' + d.qr + '" style="width:140px;height:140px;margin-top:6px">' : '');
  }
  if (priv) { priv.onclick = paintPriv; paintPriv(); }
  const wan = $('#wanBtn');
  if (wan) wan.onclick = async () => {
    $('#wanBox').style.display = 'block';
    $('#wanBox').textContent = lang() === 'zh' ? '正在建立外网通道，约需十秒……' : 'opening a tunnel…';
    const r = await postJSON('/tunnel/start', {}).catch(() => null);
    if (r && r.ok) {
      $('#wanBox').innerHTML =
        (r.qr ? '<img src="' + r.qr + '" style="width:150px;height:150px;margin-bottom:8px">' : '') +
        '<div>' + (lang() === 'zh' ? '手机扫码，或用这个地址：' : 'Scan or open:') + '</div>' +
        '<div style="word-break:break-all">' + r.url + '</div>' +
        '<div style="color:var(--dim)">' + (lang() === 'zh'
          ? '此通道为临时地址，关闭后失效；期间所有外部访问均需该令牌。' : 'Temporary address; closes when stopped.') + '</div>' +
        '<button class="btn ghost" id="wanStop" style="margin-top:8px">' + (lang() === 'zh' ? '关闭通道' : 'Close') + '</button>';
      const st = $('#wanStop');
      if (st) st.onclick = async () => {
        await postJSON('/tunnel/stop', {}).catch(() => null);
        $('#wanBox').style.display = 'none';
        $('#wanBox').innerHTML = '';
      };
    } else {
      $('#wanBox').textContent = (lang() === 'zh' ? '未能建立：' : 'failed: ') + ((r && r.error) || '');
    }
  };
  $('#pairReset').onclick = async () => {
    await postJSON('/pair/reset', {}).catch(() => null);
    $('#qrBox').innerHTML = '';
    $('#pairState').textContent = lang() === 'zh' ? '已断开。需要连接时请重新生成二维码。' : 'unpaired';
  };
  $('#briefNow').onclick = async () => {
    $('#briefNow').disabled = true;
    const r = await postJSON('/brief/now', {}).catch(() => null);
    $('#briefNow').disabled = false;
    if (r && r.ok) { loadBriefs(); }
    else { $('#briefBox').textContent = (lang() === 'zh' ? '生成失败：' : 'failed: ') + ((r && r.error) || ''); }
  };

  $('#packExport').onclick = async () => { location.href = (await findAPI()) + '/memory/pack.zip'; };
  $('#packImport').onclick = () => $('#packFile').click();
  $('#packFile').onchange = async () => {
    const f = $('#packFile').files && $('#packFile').files[0];
    if (!f) return;
    $('#packMsg').textContent = lang() === 'zh' ? '导入中…' : 'importing…';
    const rd = new FileReader();
    rd.onload = async () => {
      const r = await postJSON('/memory/import', { data: String(rd.result) });
      $('#packMsg').textContent = (r && r.ok) ? (lang() === 'zh' ? '导进来了' : 'imported')
        : (lang() === 'zh' ? '没导上：' + ((r && r.error) || '') : 'failed');
    };
    rd.readAsDataURL(f);
  };
  $('#packCheck').onclick = async () => {
    const r = await getJSON('/memory/check');
    $('#packMsg').textContent = r ? (r.summary || r.error || '') : '';
  };
  $('#packFix').onclick = async () => {
    const r = await postJSON('/memory/fix', {});
    $('#packMsg').textContent = (r && r.ok) ? (lang() === 'zh' ? '已修复' : 'repaired') : ((r && r.error) || '');
  };

  $('#modelApply').onclick = async () => {
    const m = $('#modelPick').value;
    if (!m || m.indexOf('（') === 0) return;
    const r = await postJSON('/models/use', { model: m });
    $('#modelMsg').textContent = (r && r.ok) ? (lang() === 'zh' ? '已保存，重启后生效' : 'saved — restart to apply')
      : (lang() === 'zh' ? '保存失败' : 'failed');
  };
}

wireExtras();

$('#diagBtn').onclick = async () => {
  const box = $('#diagBox');
  box.textContent = lang() === 'zh' ? '检查中…' : 'checking…';
  const d = await getJSON('/selfcheck').catch(() => null);
  if (!d || !d.checks) { box.textContent = lang() === 'zh' ? '连不上引擎' : 'engine unreachable'; return; }
  box.innerHTML = d.checks.map((c) =>
    '<div>' + (c.ok ? '✓' : '✗') + ' ' + c.name + ' · ' + (c.detail || '') + '</div>').join('') +
    '<div style="margin-top:6px;color:' + (d.ok ? '#5aa87a' : 'var(--pink)') + '">' +
    (d.ok ? (lang() === 'zh' ? '全部正常' : 'all good') : (lang() === 'zh' ? '有问题，见上面' : 'issues above')) + '</div>';
};
loadUsage(); loadBriefs(); loadProactive(); loadPair(); loadModels();
setInterval(loadUsage, 30000);
