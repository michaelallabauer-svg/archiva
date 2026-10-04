"use strict";
(() => {
  const menu = document.createElement('div');
  menu.className = 'overview-action-menu';
  menu.id = 'overview-action-menu';
  menu.hidden = true;
  menu.setAttribute('role', 'menu');
  menu.setAttribute('aria-label', 'Zeilenaktionen');
  document.body.append(menu);
  let trigger = null;
  const close = (restoreFocus = false) => {
    menu.hidden = true;
    if (trigger) {
      trigger.setAttribute('aria-expanded', 'false');
      if (restoreFocus) trigger.focus({preventScroll:true});
    }
    trigger = null;
  };
  const open = (row, x, y) => {
    const source = row.querySelector('template.row-action-template');
    if (!source) return;
    close();
    trigger = row.querySelector('.row-action-trigger');
    trigger.setAttribute('aria-expanded', 'true');
    trigger.setAttribute('aria-controls', menu.id);
    menu.replaceChildren(source.content.cloneNode(true));
    menu.querySelectorAll('a,button').forEach(el => el.setAttribute('role', 'menuitem'));
    menu.hidden = false;
    menu.style.left = `${Math.max(8, Math.min(x, innerWidth - menu.offsetWidth - 8))}px`;
    menu.style.top = `${Math.max(8, Math.min(y, innerHeight - menu.offsetHeight - 8))}px`;
    menu.querySelector('a,button')?.focus({preventScroll:true});
  };
  document.addEventListener('click', event => {
    const button = event.target.closest('.row-action-trigger');
    if (button) {
      if (trigger === button && !menu.hidden) return close();
      const rect = button.getBoundingClientRect();
      open(button.closest('tr'), rect.left, rect.bottom + 4);
    } else if (!menu.contains(event.target)) close();
  });
  document.addEventListener('contextmenu', event => {
    const row = event.target.closest('tr[data-overview-row]');
    if (!row || !row.querySelector('.row-action-template')) return;
    event.preventDefault();
    open(row, event.clientX, event.clientY);
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && !menu.hidden) {
      event.preventDefault(); close(true); return;
    }
    if (event.key === 'ContextMenu' || (event.shiftKey && event.key === 'F10')) {
      const row = event.target.closest('tr[data-overview-row]');
      if (row?.querySelector('.row-action-trigger')) {
        event.preventDefault();
        const rect = row.querySelector('.row-action-trigger').getBoundingClientRect();
        open(row, rect.left, rect.bottom);
      }
    }
    if (!menu.hidden && menu.contains(event.target) && ['ArrowDown','ArrowUp','Home','End'].includes(event.key)) {
      event.preventDefault();
      const items = [...menu.querySelectorAll('a,button')];
      const index = items.indexOf(document.activeElement);
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? items.length - 1 : (index + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length;
      items[next]?.focus();
    }
  });
  document.addEventListener('focusin', event => {
    if (!menu.hidden && !menu.contains(event.target) && event.target !== trigger) close();
  });
  document.addEventListener('scroll', event => {
    if (!menu.contains(event.target)) close();
  }, true);
  window.addEventListener('resize', () => close());
  menu.addEventListener('submit', event => {
    const form = event.target;
    if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) event.preventDefault();
  });
})();
    document.querySelectorAll('.search-results-container').forEach((container) => {
      const viewport = container.querySelector('.search-results-scroll');
      const control = container.querySelector('.search-scroll-control');
      const slider = control.querySelector('input');
      const sync = () => {
        const max = Math.max(0, viewport.scrollWidth - viewport.clientWidth);
        control.hidden = max <= 1;
        slider.max = String(max);
        slider.value = String(viewport.scrollLeft);
      };
      slider.addEventListener('input', () => { viewport.scrollLeft = Number(slider.value); });
      viewport.addEventListener('scroll', sync, { passive:true });
      new ResizeObserver(sync).observe(viewport);
      sync();
    });
