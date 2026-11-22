/* 引擎接口：端口从 8777 起找一个活的 */
window.API = null;
window.findAPI = async function () {
  if (window.API) return window.API;
  const saved = localStorage.getItem('aster.api');
  const list = saved ? [saved] : [];
  for (let p = 8777; p <= 8800; p++) list.push('http://127.0.0.1:' + p);
  for (const base of list) {
    try {
      const ctl = new AbortController();
      const to = setTimeout(() => ctl.abort(), 900);
      const r = await fetch(base + '/health', {signal: ctl.signal});
      clearTimeout(to);
      if (r.ok) { window.API = base; localStorage.setItem('aster.api', base); return base; }
    } catch (e) {}
  }
  return null;
};
window.getJSON = async (path) => {
  const base = await findAPI();
  if (!base) return null;
  try { const r = await fetch(base + path); return await r.json(); } catch (e) { return null; }
};
window.postJSON = async (path, body) => {
  const base = await findAPI();
  if (!base) return null;
  try {
    const r = await fetch(base + path, {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body || {})});
    return await r.json();
  } catch (e) { return null; }
};

/* 2026-11-22：进页面先问一句性别，男生就把文案里的「她」换掉 */
(async function () {
  try {
    const d = await getJSON('/identity');
    if (window.setGender) window.setGender((d && d.gender) || '');
  } catch (e) {}
})();
