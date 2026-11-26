'use strict';

(() => {
  const $ = id => document.getElementById(id);
  const compact = matchMedia('(max-width: 760px)');
  const stage = $('workspace-stage');
  const pursuitPanel = $('fleet-pursuit');
  const input = $('llm-instruction');
  const fullscreen = $('workspace-fullscreen');
  let pursuit = null;

  class WorkspaceDock {
    constructor() {
      this.content = $('workspace-dock-content');
      this.toggle = $('workspace-dock-toggle');
      this.tabs = [...document.querySelectorAll('.workspace-tabs [role="tab"]')];
      this.active = this.tabs.find(tab => tab.getAttribute('aria-selected') === 'true');
      this.toggle.addEventListener('click', () => this.setOpen(this.content.hidden));
      for (const [index, tab] of this.tabs.entries()) {
        tab.addEventListener('click', () => this.show(tab));
        tab.addEventListener('keydown', event => {
          const size = this.tabs.length;
          const next = {ArrowLeft: (index + size - 1) % size, ArrowRight: (index + 1) % size,
            Home: 0, End: size - 1}[event.key];
          if (next === undefined) return;
          event.preventDefault();
          this.show(this.tabs[next]);
          this.tabs[next].focus();
        });
      }
    }

    show(tab) {
      this.setOpen(true, tab);
      if (compact.matches) this.content.scrollIntoView({block: 'nearest'});
    }

    setOpen(open, tab = this.active) {
      const changed = this.content.hidden !== !open || tab !== this.active;
      this.active = tab;
      this.content.hidden = !open;
      this.toggle.setAttribute('aria-expanded', String(open));
      this.toggle.querySelector('span').textContent = open ? '收起工具面板' : '展开工具面板';
      for (const item of this.tabs) {
        const active = item === tab;
        item.setAttribute('aria-selected', String(active));
        item.tabIndex = active ? 0 : -1;
        $(item.getAttribute('aria-controls')).hidden = !open || !active;
      }
      if (changed) window.dispatchEvent(new CustomEvent('sidebar:change', {
        detail: {panel: open ? tab.getAttribute('aria-controls') : null}
      }));
    }
  }

  const dock = new WorkspaceDock();

  function fillTask(instruction, zoneId) {
    if (input.disabled) return;
    if (zoneId) window.liveMap.selectZone(zoneId, true);
    input.value = instruction;
    input.focus();
  }

  document.querySelectorAll('[data-show-task]').forEach(button => {
    button.addEventListener('click', () => {
      $('llm-panel').querySelector('.mission-body').scrollTop = 0;
      $('llm-panel').scrollIntoView({block: 'start'});
      const target = button.hasAttribute('data-focus-instruction') && !input.disabled ? input : $('mission-title');
      target.focus({preventScroll: true});
    });
  });

  document.querySelectorAll('[data-workspace-tab]').forEach(button => {
    button.addEventListener('click', () => {
      const tab = $(button.dataset.workspaceTab);
      dock.show(tab);
      tab.focus();
    });
  });
  $('task-presets').querySelectorAll('[data-instruction]').forEach(button => {
    button.addEventListener('click', () => fillTask(button.dataset.instruction, button.dataset.zone));
  });
  $('task-use-selection').addEventListener('click', () => {
    const zone = window.liveMap.selectedZone();
    if (zone) fillTask(`巡查${zone.properties.name}所在区域。`);
  });

  window.addEventListener('map:selection', ({detail: {feature}}) => {
    const selection = $('stage-selection');
    selection.hidden = !feature;
    selection.textContent = feature?.properties.name ?? '';
    const zone = window.liveMap.selectedZone();
    $('task-use-selection').hidden = !zone;
    $('task-selection-name').textContent = zone?.properties.name ?? '';
  });
  $('stage-follow').addEventListener('click', () => window.liveMap.followPursuit());
  window.addEventListener('workspace:show-map', () => {
    if (compact.matches) stage.scrollIntoView({block: 'center'});
  });
  document.addEventListener('click', event => {
    if (!event.target.closest('[data-open-pursuit]') || !pursuit) return;
    $('fleet-tab').click();
    pursuitPanel.focus({preventScroll: true});
    pursuitPanel.scrollIntoView({block: compact.matches ? 'start' : 'nearest'});
  });
  window.addEventListener('workspace:mission', ({detail}) => {
    window.liveMap.setMissionZones(detail?.tasks.map(task => task.target.zone_id) ?? []);
  });
  window.addEventListener('workspace:pursuit', ({detail}) => {
    pursuit = detail?.pursuit ?? null;
    $('stage-pursuit').hidden = !pursuit;
    $('stage-follow').hidden = !pursuit || ['complete', 'failed', 'stopped'].includes(pursuit.status);
  });

  function updateFullscreen() {
    const native = !!document.fullscreenElement;
    const immersive = document.body.classList.contains('workspace-immersive');
    fullscreen.setAttribute('aria-pressed', String(native || immersive));
    const label = native ? '退出全屏' : immersive ? '退出沉浸'
      : document.fullscreenEnabled ? '全屏工作台' : '沉浸模式';
    fullscreen.setAttribute('aria-label', label);
    fullscreen.title = label;
  }

  fullscreen.addEventListener('click', async () => {
    if (document.body.classList.contains('workspace-immersive')) {
      document.body.classList.remove('workspace-immersive');
      updateFullscreen();
      return;
    }
    try {
      if (document.fullscreenElement) await document.exitFullscreen();
      else if (document.fullscreenEnabled) await document.documentElement.requestFullscreen();
      else document.body.classList.add('workspace-immersive');
    } catch {
      document.body.classList.add('workspace-immersive');
    }
    updateFullscreen();
  });
  document.addEventListener('fullscreenchange', updateFullscreen);
  updateFullscreen();
})();
