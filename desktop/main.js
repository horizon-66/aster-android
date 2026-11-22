function bootlog(m){try{require('fs').appendFileSync('/tmp/boot.log',new Date().toISOString()+' '+m+'\n')}catch(e){}}
bootlog('module loaded');
// 星子 · 桌面壳（Electron）
// 职责：开一个窗口、把本地大脑当子进程拉起、没有本地模型时切云端 API。
const { app, BrowserWindow, ipcMain, shell } = require('electron');
function pvlog(...a) { try { require('fs').appendFileSync('/tmp/pv.log', new Date().toISOString() + ' ' + a.join(' ') + '\n'); } catch (e) {} }
const path = require('path');
const http = require('http');
const { spawn } = require('child_process');
const fs = require('fs');
const LOGF = '/tmp/aster-main.log';
function log() {
  try { fs.appendFileSync(LOGF, '[' + new Date().toISOString().slice(11,19) + '] ' +
    Array.from(arguments).join(' ') + '\n'); } catch (e) {}
}


// 用户数据目录：记忆、配置都放这儿
function userDataDir() {
  const dir = path.join(app.getPath('userData'), 'memory');
  if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
  // 首次启动：把包里的空白模板拷过去
  const tpl = path.join(__dirname, 'engine', 'memory_seed');
  try {
    for (const f of fs.readdirSync(tpl)) {
      const dst = path.join(dir, f);
      if (!fs.existsSync(dst)) fs.copyFileSync(path.join(tpl, f), dst);
    }
  } catch (e) { /* 模板缺了也能跑 */ }
  return dir;
}

let ENGINE_PORT = Number(process.env.ASTER_PORT) || 0;
let engine = null;
let win = null;
let lastEngineResult = null;
let engineStarting = null;

// 固定端口（手机要记住它）；被占再往后找
const PREFERRED_PORT = Number(process.env.ASTER_PORT) || 8790;

function pickPort(startAt) {
  const PREFERRED = startAt || PREFERRED_PORT;
  const net = require('net');
  return new Promise((resolve) => {
    let p = PREFERRED;
    const limit = PREFERRED + 20;
    const tryOne = () => {
      const srv = net.createServer();
      srv.once('error', () => { p += 1; if (p > limit) resolve(PREFERRED); else tryOne(); });
      srv.once('listening', () => srv.close(() => resolve(p)));
      srv.listen(p, '0.0.0.0');
    };
    tryOne();
  });
}

function engineHealthy() {
  return new Promise((resolve) => {
    const req = http.get({ host: '127.0.0.1', port: ENGINE_PORT, path: '/health', timeout: 800 }, (res) => {
      res.resume(); resolve(res.statusCode === 200);
    });
    req.on('error', () => resolve(false));
    req.on('timeout', () => { req.destroy(); resolve(false); });
  });
}

async function startEngine() {
  // 并发调用只允许一份，否则第二个会抢端口然后崩
  if (engineStarting) return engineStarting;
  engineStarting = (async () => {
    // 端口在 app ready 时已经定过一次，这里不再重挑（否则界面拿到的是旧值）
    if (!ENGINE_PORT) ENGINE_PORT = await pickPort();
    log('using port', ENGINE_PORT);
    const engineDir = path.join(__dirname, 'engine');
    // 先用包里带的 Python，没有再退到系统的
    // 跨平台：mac 是 py/bin/python3，Windows 是 py/python.exe
    const bundledUnix = path.join(engineDir, 'py', 'bin', 'python3');
    const bundledWin = path.join(engineDir, 'py', 'python.exe');
    const bundled = fs.existsSync(bundledUnix) ? bundledUnix : bundledWin;
    const py = process.env.XINGZI_PYTHON || (fs.existsSync(bundled) ? bundled : (process.platform === 'win32' ? 'python' : 'python3'));
    try {
      engine = spawn(py, [path.join(engineDir, 'core.py')], {
      cwd: engineDir,
      env: {
        ...process.env,
        EMBER_PORT: String(ENGINE_PORT),
        ASTER_HOME: userDataDir(),          // 打包后包内只读，记忆必须放用户目录
        PATH: path.join(engineDir, 'py', 'bin') + ':' + (process.env.PATH || ''),
        PYTHONPATH: engineDir,
      },
      stdio: ['ignore', fs.openSync('/tmp/aster-engine.log', 'a'), fs.openSync('/tmp/aster-engine.log', 'a')],
      detached: false,
    });
    // 2026-11-13：引擎挂了要自己拉起来，不然界面一直显示离线
    engine.on('exit', (code, sig) => {
      log('engine exited', code, sig);
      engine = null;
      if (app.isQuitting) return;
      setTimeout(() => { startEngine().then(r => log('auto restart', JSON.stringify(r))).catch(() => {}); }, 1200);
    });
    } catch (e) {
      return { started: false, reason: String(e) };
    }
    for (let i = 0; i < 40; i++) {
      if (await engineHealthy()) return { started: true };
      await new Promise((r) => setTimeout(r, 250));
    }
    return { started: false, reason: '引擎起了但没响应' };
  })().finally(() => { engineStarting = null; });
  return engineStarting;
}

function createWindow() {
  win = new BrowserWindow({
    width: 1180, height: 820, minWidth: 1240, minHeight: 800,
    titleBarStyle: 'hiddenInset',
    // 2026-11-12 夜：原来 transparent + vibrancy 让桌面内容直接透上来，他说太透
    // 改成不透桌面：实底 + 卡片刻度靠 CSS 做磨砂感
    transparent: false,
    vibrancy: undefined,
    backgroundColor: '#141417',
    opacity: 0.95,
    webPreferences: { preload: path.join(__dirname, 'preload.js'), contextIsolation: true,
                      webviewTag: true, sandbox: false },
  });
  // 2026-11-20：新界面（v2）成为默认；旧界面保留在 ui/index.html，可从新界面的「旧界面」入口进入
  // 开场信只在「还没走完引导」时出现；走过一次就直接进主界面，不再每次闪一下。
  // 想重看：设置里点「重新阅读开场信」，或者命令行加 ASTER_INTRO=1。
  const _v2dir = path.join(__dirname, 'ui', 'v2');
  const _intro = path.join(_v2dir, 'intro.html');
  const _home = path.join(_v2dir, 'index.html');
  let _entry = _home;
  try {
    const _ob = path.join(userDataDir(), 'onboarding.json');
    const _done = fs.existsSync(_ob) && (JSON.parse(fs.readFileSync(_ob, 'utf8')).finished === true);
    if (!_done || process.env.ASTER_INTRO === '1') _entry = _intro;
  } catch (e) {
    _entry = fs.existsSync(_intro) ? _intro : _home;
  }
  if (!fs.existsSync(_entry)) _entry = fs.existsSync(_intro) ? _intro : path.join(__dirname, 'ui', 'index.html');
  log('loadFile', _entry);
  // 只在「打开软件」这一次给对话页带上 boot=1，开场遮罩据此只播一遍；
  // 从别的页面导航回来（没带这个参数）就不再播。
  try { win.loadFile(_entry, { query: _entry.endsWith('index.html') ? { boot: '1' } : {} }); }
  catch (e) { win.loadFile(_entry); }
  win.webContents.on('did-fail-load', (e, c, d) => log('did-fail-load', c, d));
  win.webContents.on('console-message', (e, lvl, msg) => log('console', lvl, msg.slice(0, 120)));
  // 2026-11-20：语音要用麦克风，Electron 默认会拒，这里明确放行 media
  try {
    const ses = win.webContents.session;
    ses.setPermissionRequestHandler((_wc, permission, cb) => {
      cb(['media', 'audioCapture', 'mediaKeySystem', 'clipboard-read'].includes(permission));
    });
    if (ses.setPermissionCheckHandler) {
      ses.setPermissionCheckHandler((_wc, permission) => permission === 'media' || permission === 'audioCapture');
    }
  } catch (e) { log('perm handler failed', String(e)); }
  win.webContents.setWindowOpenHandler(({ url }) => {
    const u = String(url);
    if (u.startsWith('file:') && u.indexOf(path.join(__dirname, 'ui')) === 7) {
      return { action: 'allow', overrideBrowserWindowOptions: {
        width: 1180, height: 820, minWidth: 520, minHeight: 380, resizable: true, title: 'Aster 预览器',
        webPreferences: { webviewTag: true, contextIsolation: true, preload: path.join(__dirname, 'preload.js') }
      } };
    }
    shell.openExternal(u);
    return { action: 'deny' };
  });
  // 2026-11-13：把文件拖进窗口时，Chromium 会试图导航去 file://，结果用系统程序打开了它。
  // 拦掉导航，交给页面的 drop 处理器读。
  // 2026-11-20：允许在 ui 目录内部跳转（对话/工作台/记忆/技能/设置），其余 file: 导航继续拦
  const _uiDir = path.join(__dirname, 'ui') + path.sep;
  win.webContents.on('will-navigate', (e, url) => {
    const u = String(url);
    if (!u.startsWith('file:')) return;
    let p2 = '';
    try { p2 = require('url').fileURLToPath(u); } catch (_) { p2 = ''; }
    if (p2 && p2.startsWith(_uiDir)) return;   // 界面内的页面互相跳，放行
    e.preventDefault();
  });
}

app.commandLine.appendSwitch('autoplay-policy', 'no-user-gesture-required');

app.whenReady().then(async () => {
  log('app ready');
  ENGINE_PORT = await pickPort(8777);      // 固定从 8777 开始，界面默认也连它
  log('picked port', ENGINE_PORT);
  bootlog('whenReady');
  createWindow();
  bootlog('createWindow done');
  global.__mainWin = BrowserWindow.getAllWindows()[0];
  // 不依赖渲染进程：主进程自己把引擎拉起来
  startEngine().then(r => log('auto start', JSON.stringify(r))).catch(e => log('auto start failed', String(e)));
  // 2026-11-22：进程没死但引擎不应答时，靠退出事件是救不回来的，这里每 30 秒探一次
  setInterval(async () => {
    try {
      if (!ENGINE_PORT) return;
      if (await engineHealthy()) return;
      log('health check failed, restarting engine');
      engineStarting = null;
      await startEngine();
      log('health restart result', JSON.stringify(engineStarting));
    } catch (e) {}
  }, 30000);
  // ———— 预览器：一个窗口里用 WebContentsView 放多个页面（替代 webview） ————
  const PREVIEW = new Map();   // win.id -> {tabs:[{id,view,url}], active}
  const CHROME_H = 118;        // 上方页签条 + 地址栏占位
  const BOTTOM_PAD = 64;

  const layouts = (win, st) => {
    const [w, h] = win.getContentSize();
    pvlog('[pv] layout', w, h);
    const box = { x: 14, y: CHROME_H, width: Math.max(200, w - 28), height: Math.max(120, h - CHROME_H - BOTTOM_PAD) };
    st.tabs.forEach((t) => { try { t.view.setBounds(box); pvlog('[pv] bounds', JSON.stringify(box)); } catch (e) { pvlog('[pv] bounds FAIL', String(e)); } });
  };
  const attach = (win, st, t) => {
    if (!t || !t.view) return;
    try { win.contentView.addChildView(t.view); } catch (e) { pvlog('[pv] attach FAIL ' + e); }
    layouts(win, st);
  };
  const showTab = (win, st, id) => {
    st.tabs.forEach((t) => { if (t.view) { try { win.contentView.removeChildView(t.view); } catch (e) {} } });
    const t = st.tabs.find((x) => x.id === id) || st.tabs[0];
    if (!t) return;
    st.active = t.id;
    if (t.view) attach(win, st, t);          // 只有真的导航过才有视图
    win.webContents.send('preview:tabs', st.tabs.map((x) => ({ id: x.id, url: x.url, active: x.id === st.active })));
  };
  const makeView = (win, st, t) => {
    if (t.view) return t.view;
    const { WebContentsView } = require('electron');
    t.view = new WebContentsView({ webPreferences: { contextIsolation: true, sandbox: true } });
    t.view.webContents.setWindowOpenHandler(({ url: u2 }) => {
      t.url = u2; t.view.webContents.loadURL(u2).catch(() => {}); return { action: 'deny' };
    });
    t.view.webContents.on('will-navigate', (_e, u3) => {
      t.url = u3;
      win.webContents.send('preview:tabs', st.tabs.map((x) => ({ id: x.id, url: x.url, active: x.id === st.active })));
    });
    return t.view;
  };
  const newTab = (win, st, url) => {
    const id = 't' + Date.now() + Math.floor(Math.random() * 100);
    const u0 = (url && url !== 'about:blank') ? url : '';
    const tab = { id, view: null, url: u0 };
    st.tabs.push(tab);
    if (u0) { makeView(win, st, tab); tab.view.webContents.loadURL(u0).catch((e) => pvlog('[pv] load FAIL ' + e)); }
    showTab(win, st, id);
    return id;
  };

  ipcMain.handle('preview:open', async (_e, url) => {
    pvlog('[pv] open asked url=' + String(url || ''));
    const main = BrowserWindow.getAllWindows().find((x) => x && !x.isDestroyed());
    let bw = 860, bh = 860, bx, by;
    if (main) {
      const b = main.getBounds();
      bw = Math.max(560, Math.round(b.width * 0.64));
      bh = b.height;
      bx = b.x + b.width + 8;
      by = b.y;
      try {
        const area = require('electron').screen.getDisplayMatching(b).workArea;
        if (bx + bw > area.x + area.width) bx = Math.max(area.x, b.x + b.width - bw);
      } catch (e) {}
    }
    const win = new BrowserWindow({
      width: bw, height: bh, x: bx, y: by, minWidth: 560, minHeight: 420, resizable: true,
      title: 'Aster 预览器', titleBarStyle: 'hiddenInset', backgroundColor: '#f1efeb',
      webPreferences: { contextIsolation: true, preload: path.join(__dirname, 'preload.js') }
    });
    const st = { tabs: [], active: null };
    PREVIEW.set(win.id, st);
    await win.loadFile(path.join(__dirname, 'ui', 'v2', 'browser.html'));
    newTab(win, st, url || '');
    win.show(); win.focus();
    win.on('resize', () => layouts(win, PREVIEW.get(win.id)));
    win.on('closed', () => PREVIEW.delete(win.id));
    pvlog('[pv] window ready tabs=' + st.tabs.length);
    return true;
  });

  ipcMain.handle('preview:nav', (e, url) => {
    const win = BrowserWindow.fromWebContents(e.sender);
    const st = win && PREVIEW.get(win.id);
    if (!st) return false;
    const t = st.tabs.find((x) => x.id === st.active);
    if (!t) { newTab(win, st, url); return true; }
    t.url = url;
    makeView(win, st, t);
    t.view.webContents.loadURL(url).catch((e) => pvlog('[pv] nav FAIL ' + e));
    try { win.contentView.removeChildView(t.view); } catch (e) {}
    attach(win, st, t);
    win.webContents.send('preview:tabs', st.tabs.map((x) => ({ id: x.id, url: x.url, title: x.title, active: x.id === st.active })));
    return true;
  });
  ipcMain.handle('preview:newtab', (e, url) => {
    const win = BrowserWindow.fromWebContents(e.sender);
    const st = win && PREVIEW.get(win.id);
    if (!st) return false;
    newTab(win, st, url || 'about:blank');
    return true;
  });
  ipcMain.handle('preview:switch', (e, id) => {
    const win = BrowserWindow.fromWebContents(e.sender);
    const st = win && PREVIEW.get(win.id);
    if (st) showTab(win, st, id);
    return true;
  });
  ipcMain.handle('preview:close', (e, id) => {
    const win = BrowserWindow.fromWebContents(e.sender);
    const st = win && PREVIEW.get(win.id);
    if (!st) return false;
    const i = st.tabs.findIndex((x) => x.id === id);
    if (i >= 0) { try { win.contentView.removeChildView(st.tabs[i].view); } catch (err) {} st.tabs.splice(i, 1); }
    showTab(win, st, (st.tabs[0] || {}).id);
    if (!st.tabs.length) win.close();
    return true;
  });
  ipcMain.handle('preview:links', async (e) => {
    let win = BrowserWindow.fromWebContents(e.sender);
    if (!win || !PREVIEW.has(win.id)) {
      const ids = [...PREVIEW.keys()].reverse();
      win = ids.map((id) => BrowserWindow.fromId(id)).find((x) => x && !x.isDestroyed());
    }
    const st = win && PREVIEW.get(win.id);
    const t = st && st.tabs.find((x) => x.id === st.active);
    if (!t || !t.view) return { ok: false };
    try {
      const links = await t.view.webContents.executeJavaScript(
        "Array.from(document.querySelectorAll('a')).slice(0, 80).map(a => ({t: (a.innerText||'').trim().slice(0,60), h: a.href})).filter(x => x.t && /^https?:/.test(x.h))");
      return { ok: true, links };
    } catch (err) { return { ok: false, error: String(err).slice(0, 60) }; }
  });
  ipcMain.handle('preview:read', async (e) => {
    // 调用方是聊天窗口；要读的是预览窗口，取最近打开的那个
    let win = BrowserWindow.fromWebContents(e.sender);
    if (!win || !PREVIEW.has(win.id)) {
      const ids = [...PREVIEW.keys()].reverse();
      win = ids.map((id) => BrowserWindow.fromId(id)).find((x) => x && !x.isDestroyed());
    }
    pvlog('[pv] read asked, win=' + (win && win.id));
    const st = win && PREVIEW.get(win.id);
    if (!st) return { ok: false, error: 'no window' };
    const t = st.tabs.find((x) => x.id === st.active);
    if (!t || !t.view) return { ok: false, error: 'no tab' };
    try {
      const txt = await t.view.webContents.executeJavaScript(
        "(() => { const el = document.body; return (el ? el.innerText : '').slice(0, 6000); })()");
      const url = t.url || '';
      pvlog('[pv] read ok len=' + String(txt || '').length + ' url=' + url);
      return { ok: true, url, text: String(txt || '') };
    } catch (err) {
      return { ok: false, error: String(err).slice(0, 80) };
    }
  });
  ipcMain.handle('preview:act', (e, act) => {
    const win = BrowserWindow.fromWebContents(e.sender);
    const st = win && PREVIEW.get(win.id);
    if (!st) return false;
    const t = st.tabs.find((x) => x.id === st.active);
    if (!t) return false;
    const wc = t.view.webContents;
    try {
      if (act === 'back') wc.navigationHistory ? wc.navigationHistory.goBack() : wc.goBack();
      else if (act === 'fwd') wc.navigationHistory ? wc.navigationHistory.goForward() : wc.goForward();
      else if (act === 'reload') wc.reload();
    } catch (err) {}
    return true;
  });

  ipcMain.handle('app:openUrl', async (_e, url) => {
    try {
      if (typeof url === 'string' && /^https:\/\//.test(url)) { await shell.openExternal(url); return true; }
    } catch (e) {}
    return false;
  });
  ipcMain.handle('engine:status', async () => { const h = await engineHealthy(); log('ipc engine:status', h, 'port', ENGINE_PORT); return { healthy: h, port: ENGINE_PORT }; });
  ipcMain.handle('engine:start', async () => { log('ipc engine:start'); const r = await startEngine(); lastEngineResult = r; log('spawn result', JSON.stringify(r)); return r; });
  ipcMain.handle('app:diagnose', async () => {
    const engineDir = path.join(__dirname, 'engine');
    const py = path.join(engineDir, 'py', process.platform === 'win32' ? 'python.exe' : path.join('bin', 'python3'));
    let portBusy = false;
    try { portBusy = await engineHealthy(); } catch (e) {}
    return {
      version: require('./package.json').version,
      platform: process.platform,
      arch: process.arch,
      healthy: portBusy,
      port: ENGINE_PORT,
      pythonPath: py,
      pythonExists: fs.existsSync(py),
      engineDir,
      engineScriptExists: fs.existsSync(path.join(engineDir, 'core.py')),
      userData: app.getPath('userData'),
      memoryDir: userDataDir(),
      memoryFiles: (() => { try { return fs.readdirSync(userDataDir()).length; } catch (e) { return -1; } })(),
      lastEngineResult,
      engineLog: (() => { try { return fs.readFileSync('/tmp/aster-engine.log', 'utf8').split('\n').slice(-12).join('\n'); } catch (e) { return '(没有日志)'; } })(),
      logFile: '/tmp/aster-main.log',
    };
  });
  ipcMain.handle('engine:stop', async () => { if (engine) { engine.kill(); engine = null; } return { ok: true }; });
});

app.on('window-all-closed', () => { if (engine) engine.kill(); if (process.platform !== 'darwin') app.quit(); });
app.on('before-quit', () => { if (engine) engine.kill(); });
