/* 中英对照：t('键') 或在 HTML 里用 data-zh / data-en */
window.I18N = {
  zh: {
    nav_chat: '对话', nav_work: '工作台', nav_mem: '记忆', nav_skill: '技能', nav_set: '设置',
    foot: '记忆与情绪存于本机<br>不上传 · 可导出迁移',
    perm_r: '只读', perm_w: '工作区内可修改', perm_a: '完全权限',
    search: '搜索', newtopic: '新会话',
    today: '今日', recorded: '记录', finished: '完成', blocked: '受阻', reminders: '提醒', cost: '费用', latency: '平均响应',
    doing: '进行中的事项', workbench: '前往工作台 →',
    emotions: '情绪', all8: '展开全部八项 ▾', less: '收起 ▴',
    recent: '最近记录',
    ph: '请输入内容', send: '发送',
    empty_title: '今日尚无进行中的事项',
    hint_enter: '按回车键发送', hint_shift: 'Shift 加回车换行',
    local: '本地处理', mistaken: '记录有误',
    attach_pending: '待发送', attach_remove: '撤下',
    start: '进入'
  },
  en: {
    nav_chat: 'Chat', nav_work: 'Work', nav_mem: 'Memory', nav_skill: 'Skills', nav_set: 'Settings',
    foot: 'Memory and mood are stored on this device<br>never uploaded · exportable',
    perm_r: 'Read only', perm_w: 'Edit within workspace', perm_a: 'Full access',
    search: 'Search', newtopic: 'New session',
    today: 'Today', recorded: 'Recorded', finished: 'Completed', blocked: 'Blocked', reminders: 'Reminders', cost: 'Cost', latency: 'Avg',
    doing: 'In progress', workbench: 'Open workbench →',
    emotions: 'Mood', all8: 'Show all eight ▾', less: 'Collapse ▴',
    recent: 'Recently recorded',
    ph: 'Type a message…', send: 'Send',
    empty_title: 'No tasks in progress today',
    hint_enter: 'Press Enter to send', hint_shift: 'Shift+Enter to insert a line break',
    local: 'local', mistaken: "Report an error",
    attach_pending: 'Pending', attach_remove: 'Remove',
    start: 'Enter'
  }
};
// 每次启动默认中文；用户在本次会话里切过才记住
(function () {
  if (sessionStorage.getItem('aster.lang.user') !== '1') {
    localStorage.setItem('aster.lang', 'zh');
  }
})();
window.lang = () => (localStorage.getItem('aster.lang') || 'zh');
window.setLang = (v) => {
  localStorage.setItem('aster.lang', v);
  sessionStorage.setItem('aster.lang.user', '1');
};
window.t = (k) => (I18N[lang()][k] || I18N.zh[k] || k);
window.applyI18n = () => {
  document.querySelectorAll('[data-zh]').forEach(el => {
    const v = el.getAttribute('data-' + lang());
    if (v !== null) el.innerHTML = v;
  });
  // 输入框占位、下拉里的选项这类，也得跟着语言走
  document.querySelectorAll('[data-zh-ph]').forEach(el => {
    const v = el.getAttribute('data-' + lang() + '-ph');
    if (v !== null) el.setAttribute('placeholder', v);
  });
};

/* 2026-11-22：不默认女性，也不替他决定。
   文案仍按「她」写，显示时按 profile 换：
     male  → 他
     female→ 她
     没设置→ TA（等用户自己选，不先入为主） */
window.__gender = '';
function _genderSwap() {
  const g = window.__gender;
  if (g === 'female') return;
  const to = g === 'male' ? '他' : 'TA';
  try {
    const skip = { SCRIPT: 1, STYLE: 1, TEXTAREA: 1, INPUT: 1 };
    const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null, false);
    const nodes = [];
    let n;
    while ((n = w.nextNode())) nodes.push(n);
    nodes.forEach((t) => {
      const pe = t.parentElement;
      if (!pe || skip[pe.tagName]) return;
      if (t.nodeValue && t.nodeValue.indexOf('她') >= 0) t.nodeValue = t.nodeValue.replace(/她/g, to);
    });
  } catch (e) {}
}
window.setGender = function (g) {
  window.__gender = g || '';
  if (window.applyI18n) window.applyI18n();
};
(function () {
  const orig = window.applyI18n;
  window.applyI18n = function () {
    if (orig) orig.apply(null, arguments);
    _genderSwap();
  };
  // 页面内容常常是脚本后填进来的，等它能挂观察器就盯一眼，换完就不再动
  let _pending = false;
  const _soon = () => {
    if (_pending) return;
    _pending = true;
    requestAnimationFrame(() => { _pending = false; _genderSwap(); });
  };
  const _boot = () => {
    if (window.MutationObserver) {
      new MutationObserver(_soon).observe(document.documentElement,
        { childList: true, subtree: true, characterData: true });
    }
    _soon();
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', _boot);
  else _boot();
})();
