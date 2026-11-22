/* 左栏导航：所有页面共用一份，当前页高亮 */
(function () {
  const PAGES = [
    ['chat', 'index.html', '对话', 'Chat'],
    ['work', 'work.html', '工作台', 'Work'],
    ['mem', 'memory.html', '记忆', 'Memory'],
    ['role', 'role.html', '角色', 'Character'],
    ['trip', 'trips.html', '日程', 'Schedule'],
    ['code', 'code.html', '编码', 'Coding'],
    ['debate', 'debate.html', '议事', 'Debate'],
    ['skill', 'skills.html', '技能', 'Skills'],
    ['set', 'settings.html', '设置', 'Settings']
  ];
  const here = (location.pathname.split('/').pop() || 'index.html');
  const nav = document.createElement('nav');
  nav.className = 'nav fixedNav';
  nav.innerHTML =
    '<div class="mark"><svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="#e8173d" ' +
    'stroke-width="1.5" stroke-linejoin="round"><path d="M12 2.6 13.8 10 21 12l-7.2 2L12 21.4 10.2 14 3 12l7.2-2Z"/></svg>' +
    '<b>ASTER</b></div>' +
    PAGES.map(([k, href, zh, en]) =>
      '<a data-nav="' + k + '" href="' + href + '" data-zh="' + zh + '" data-en="' + en + '"' +
      (here === href ? ' class="on"' : '') + '>' + zh + '</a>').join('') +
    '<div class="foot" data-zh="记忆与情绪存于本机<br>不上传 · 可导出迁移" ' +
    'data-en="Memory and mood are stored on this device<br>never uploaded · exportable">' +
    '记忆与情绪存于本机<br>不上传 · 可导出迁移</div>';
  document.body.appendChild(nav);
  document.body.classList.add('hasNav');   // 让正文给左栏让位
  nav.querySelectorAll('a').forEach((a) => {
    a.addEventListener('click', (e) => {
      const href = a.getAttribute('href') || '';
      // 「浏览器」改成单独开窗，不挤走当前页
      if (a.dataset.nav === 'browser' && window.xingzi && window.xingzi.openPreview) {
        e.preventDefault(); window.xingzi.openPreview(); return;
      }
      e.preventDefault(); location.href = href;
    });
  });
})();
