/* 小工具：页面右上角的语言与音乐按钮，所有 v2 页面共用 */
window.mountTools = function (handlers) {
  const box = document.createElement('div');
  box.style.cssText = 'position:fixed;top:16px;right:18px;display:flex;gap:8px;z-index:60';
  box.innerHTML = '<button class="tbtn" id="__lang">中 / EN</button>' +
    '<button class="tbtn" id="__music" title="music">♪</button>';
  document.body.appendChild(box);
  const langBtn = box.querySelector('#__lang'), musicBtn = box.querySelector('#__music');
  const paint = () => {
    langBtn.textContent = window.lang() === 'zh' ? 'EN' : '中';
    const on = localStorage.getItem('aster.music') === '1';
    musicBtn.style.color = on ? 'var(--ink)' : 'var(--dim)';
  };
  langBtn.onclick = () => {
    window.setLang(window.lang() === 'zh' ? 'en' : 'zh');
    paint(); window.applyI18n(); handlers && handlers.lang && handlers.lang();
  };
  musicBtn.onclick = () => {
    localStorage.setItem('aster.music', localStorage.getItem('aster.music') === '1' ? '0' : '1');
    paint(); handlers && handlers.music && handlers.music();
  };
  paint();
};
