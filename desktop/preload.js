const { contextBridge, ipcRenderer, webUtils } = require('electron');
contextBridge.exposeInMainWorld('xingzi', {
  engineStatus: () => ipcRenderer.invoke('engine:status'),
  engineStart: () => ipcRenderer.invoke('engine:start'),
  engineStop: () => ipcRenderer.invoke('engine:stop'),
  isDesktop: true,
  diagnose: () => ipcRenderer.invoke('app:diagnose'),
  openUrl: (u) => ipcRenderer.invoke('app:openUrl', u),
  openPreview: (u) => ipcRenderer.invoke('preview:open', u || ''),
  previewNav: (u) => ipcRenderer.invoke('preview:nav', u),
  previewNewTab: (u) => ipcRenderer.invoke('preview:newtab', u || ''),
  previewSwitch: (id) => ipcRenderer.invoke('preview:switch', id),
  previewClose: (id) => ipcRenderer.invoke('preview:close', id),
  previewAct: (a) => ipcRenderer.invoke('preview:act', a),
  previewRead: () => ipcRenderer.invoke('preview:read'),
  previewLinks: () => ipcRenderer.invoke('preview:links'),
  onPreviewTabs: (cb) => ipcRenderer.on('preview:tabs', (_e, tabs) => cb(tabs)),
  // 2026-11-12 夜：把拖进来的文件换成真实路径，交给引擎读
  pathForFile: (f) => { try { return webUtils.getPathForFile(f) || (f && f.path) || ''; } catch (e) { return (f && f.path) || ''; } },
});
