'use strict';

(() => {
  const $ = id => document.getElementById(id);
  const compact = matchMedia('(max-width: 760px)');
  const stage = $('workspace-stage');
  const pursuitPanel = $('fleet-pursuit');
  const input = $('llm-instruction');
  const fullscreen = $('workspace-fullscreen');
  const eventFilters = document.querySelectorAll('[data-event-filter]');
  let mission = null;
  let pursuit = null;
  let eventFilter = 'all';

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

  class EventReview {
    constructor(queue) {
      this.queue = queue;
      this.rows = new Map();
      this.active = null;
      this.selected = null;
    }

    render(cards, groups) {
      this.groups = groups;
      this.queue.hidden = cards.length === 0;
      const keys = new Set(cards.map(card => card.dataset.reviewId));
      for (const [key, row] of this.rows) {
        if (keys.has(key)) continue;
        row.remove();
        this.rows.delete(key);
      }
      for (const [index, card] of cards.entries()) {
        const key = card.dataset.reviewId;
        let row = this.rows.get(key);
        if (!row) {
          row = document.createElement('button');
          row.type = 'button';
          row.className = 'dock-list-item';
          row.append(document.createElement('strong'), document.createElement('small'));
          row.addEventListener('click', () => this.select(key));
          this.rows.set(key, row);
        }
        row.card = card;
        const title = card.querySelector('.traffic-card-plate, .event-person-heading h4').textContent;
        const context = [...card.querySelectorAll('.traffic-card-zone, .traffic-card-state, .event-person-heading p')]
          .map(field => field.textContent).join(' · ');
        if (row.firstElementChild.textContent !== title) row.firstElementChild.textContent = title;
        if (row.lastElementChild.textContent !== context) row.lastElementChild.textContent = context;
        if (this.queue.children[index] !== row) this.queue.insertBefore(row, this.queue.children[index] || null);
      }
      this.show(keys.has(this.selected) ? this.selected : keys.values().next().value);
    }

    select(key, focus = false) {
      if (!this.rows.has(key)) return false;
      this.selected = key;
      this.show(key);
      if (focus) this.rows.get(key).focus();
      return true;
    }

    show(key) {
      const changed = this.rows.get(key)?.card !== this.active;
      this.active = this.rows.get(key)?.card;
      for (const [id, row] of this.rows) {
        const selected = id === key;
        row.setAttribute('aria-pressed', String(selected));
        row.card.hidden = !selected;
      }
      const evidence = changed && this.active?.querySelector('.traffic-evidence');
      if (evidence) evidence.open = true;
      for (const group of [$('llm-traffic'), $('llm-vision')]) {
        group.hidden = !this.groups.includes(group.id) ||
          !(this.active && group.contains(this.active) || group.querySelector('.llm-vision-error:not([hidden])'));
      }
    }
  }

  const eventReview = new EventReview($('events-queue'));

  function updateEventStatus() {
    const traffic = mission?.traffic;
    const vision = mission?.vision;
    const monitors = mission?.tasks.map(task => task.monitor).filter(Boolean) ?? [];
    const trafficError = !$('llm-traffic-error').hidden;
    const visionError = !$('llm-vision-error').hidden || vision?.status === 'completed_with_errors';
    const trafficState = traffic?.suspended ? ['已暂停', 'idle']
      : trafficError ? ['部分不可用', 'issue']
      : traffic?.active ? ['监测中', 'active']
      : monitors.some(monitor => monitor.traffic?.status === 'starting') ? ['正在启动', 'waiting']
      : monitors.some(monitor => ['queued', 'flying'].includes(monitor.status)) ? ['等待无人机就位', 'waiting']
      : traffic ? ['已结束', 'idle'] : ['待启动', 'idle'];
    const visionState = visionError ? ['部分未完成', 'issue']
      : vision?.status === 'running' ? (vision.total ? ['识别中', 'active'] : ['等待巡查画面', 'waiting'])
      : vision?.status === 'completed' ? ['已完成', 'idle'] : ['待启动', 'idle'];
    for (const [id, [label, state]] of [['events-traffic-state', trafficState], ['events-vision-state', visionState]]) {
      const field = $(id);
      if (field.textContent !== label) field.textContent = label;
      field.dataset.state = state;
    }
  }

  function updateEvents() {
    updateEventStatus();
    const vehicles = [...$('llm-traffic-targets').children];
    const people = [...$('llm-anomalies').children];
    const directPursuit = pursuit && !window.trafficView.has(pursuit.vehicle_id);
    const counts = {vehicles: vehicles.length + Number(!!directPursuit), people: people.length};
    counts.all = counts.vehicles + counts.people;
    for (const button of eventFilters) {
      const kind = button.dataset.eventFilter;
      const selected = String(kind === eventFilter);
      if (button.getAttribute('aria-pressed') !== selected) button.setAttribute('aria-pressed', selected);
      const count = button.querySelector('[data-event-count]');
      if (count.textContent !== String(counts[kind])) count.textContent = counts[kind];
    }
    const badge = $('events-tab-count');
    if (badge.textContent !== String(counts.all)) badge.textContent = counts.all;
    badge.hidden = !counts.all;

    const showVehicles = eventFilter !== 'people';
    const showPeople = eventFilter !== 'vehicles';
    const available = [...(showVehicles ? vehicles : []), ...(showPeople ? people : [])];
    eventReview.render(available, [showVehicles && 'llm-traffic', showPeople && 'llm-vision'].filter(Boolean));
    $('events-pursuit').hidden = !showVehicles || !pursuit;
    if (pursuit) {
      const vehicle = mission?.traffic?.targets.find(target => target.target_id === pursuit.vehicle_id);
      const identity = vehicle?.status === 'confirmed' && vehicle.plate || pursuit.vehicle_id;
      const title = `车辆追踪 · ${identity}`;
      if ($('events-pursuit-target').textContent !== title) $('events-pursuit-target').textContent = title;
    }
    const empty = $('events-empty');
    empty.hidden = !$('llm-traffic').hidden || !$('llm-vision').hidden || !$('events-pursuit').hidden;
    const hasMission = !!mission || !!pursuit;
    empty.dataset.phase = hasMission ? 'mission' : 'idle';
    const preview = $('events-preview');
    preview.hidden = hasMission;
    for (const item of preview.children) {
      item.hidden = eventFilter !== 'all' && eventFilter !== item.dataset.previewKind;
    }
    if (!empty.hidden) {
      const subject = {all: '巡查', vehicles: '车辆', people: '人员'}[eventFilter];
      const title = hasMission ? `暂无${subject}事件` : '等待巡查任务';
      const receiving = ['events-traffic-state', 'events-vision-state'].some((id, index) =>
        (index === 0 ? showVehicles : showPeople) && ['active', 'waiting'].includes($(id).dataset.state));
      const message = !hasMission ? '在巡查助手中指定区域。发现线索后，可在这里打开记录并复核证据。'
        : receiving ? '发现异常后，证据和识别结果会显示在这里。' : '本次任务暂无该类异常记录。';
      if ($('events-empty-title').textContent !== title) $('events-empty-title').textContent = title;
      if ($('events-empty-message').textContent !== message) $('events-empty-message').textContent = message;
    }
    $('events-create-task').hidden = hasMission;
    $('task-open-events').hidden = !hasMission;
  }

  for (const button of eventFilters) button.addEventListener('click', () => {
    eventFilter = button.dataset.eventFilter;
    updateEvents();
  });

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
  for (const button of document.querySelectorAll('[data-open-pursuit]')) {
    button.addEventListener('click', () => {
      if (!pursuit) return;
      $('fleet-tab').click();
      pursuitPanel.focus({preventScroll: true});
      pursuitPanel.scrollIntoView({block: compact.matches ? 'start' : 'nearest'});
    });
  }

  window.addEventListener('workspace:mission', ({detail}) => {
    mission = detail;
    window.liveMap.setMissionZones(mission?.tasks.map(task => task.target.zone_id) ?? []);
    updateEvents();
  });
  window.addEventListener('workspace:select-event', ({detail: {id}}) => {
    if (eventFilter === 'people') {
      eventFilter = 'vehicles';
      updateEvents();
    }
    $('events-tab').click();
    if (!eventReview.select(`vehicle:${id}`, true) && !$('events-pursuit').hidden) $('events-pursuit').querySelector('button').focus();
  });
  window.addEventListener('workspace:pursuit', ({detail}) => {
    pursuit = detail?.pursuit ?? null;
    $('stage-pursuit').hidden = !pursuit;
    $('stage-follow').hidden = !pursuit || ['complete', 'failed', 'stopped'].includes(pursuit.status);
    if (pursuit) {
      $('events-pursuit-status').textContent = detail.title;
    }
    updateEvents();
  });
  window.addEventListener('mission:reset', () => {
    eventFilter = 'all';
    eventReview.selected = null;
    updateEvents();
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
