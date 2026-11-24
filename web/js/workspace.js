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
    const vehicles = $('llm-traffic-targets').children.length;
    const people = $('llm-anomalies').children.length;
    const directPursuit = pursuit && !window.trafficView.has(pursuit.vehicle_id);
    const counts = {vehicles: vehicles + Number(!!directPursuit), people};
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
    $('llm-traffic').hidden = !showVehicles || !(vehicles || !$('llm-traffic-error').hidden);
    $('llm-vision').hidden = !showPeople || !(people || !$('llm-vision-error').hidden);
    $('events-pursuit').hidden = !showVehicles || !pursuit;
    if (pursuit) {
      const vehicle = mission?.traffic?.targets.find(target => target.target_id === pursuit.vehicle_id);
      const identity = vehicle?.status === 'confirmed' && vehicle.plate || pursuit.vehicle_id;
      const title = `车辆追踪 · ${identity}`;
      if ($('events-pursuit-target').textContent !== title) $('events-pursuit-target').textContent = title;
    }
    $('events-empty').hidden = !$('llm-traffic').hidden || !$('llm-vision').hidden || !$('events-pursuit').hidden;
    const hasMission = !!mission || !!pursuit;
    if (!$('events-empty').hidden) {
      const subject = {all: '巡查', vehicles: '车辆', people: '人员'}[eventFilter];
      const title = `暂无${subject}事件`;
      const receiving = ['events-traffic-state', 'events-vision-state'].some((id, index) =>
        (index === 0 ? showVehicles : showPeople) && ['active', 'waiting'].includes($(id).dataset.state));
      const message = !hasMission ? '开始巡查后，异常记录会显示在这里。'
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

  document.querySelectorAll('[data-workspace-tab]').forEach(button => {
    button.addEventListener('click', () => {
      const tab = $(button.dataset.workspaceTab);
      tab.click();
      if (!compact.matches) tab.focus();
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
  window.addEventListener('sidebar:change', ({detail: {panel}}) => {
    if (compact.matches && !$('map').classList.contains('picking')) $(panel).scrollIntoView({block: 'start'});
  });
  function updateNavOrientation() {
    document.querySelector('.workspace-tabs').setAttribute('aria-orientation', compact.matches ? 'horizontal' : 'vertical');
  }
  compact.addEventListener('change', updateNavOrientation);
  updateNavOrientation();

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
    if (!window.trafficView.focus(id) && !$('events-pursuit').hidden) $('events-pursuit').querySelector('button').focus();
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
    updateEvents();
  });

  function updateFullscreen() {
    const native = !!document.fullscreenElement;
    const immersive = document.body.classList.contains('workspace-immersive');
    fullscreen.setAttribute('aria-pressed', String(native || immersive));
    fullscreen.querySelector('span').textContent = native ? '退出全屏' : immersive ? '退出沉浸'
      : document.fullscreenEnabled ? '全屏工作台' : '沉浸模式';
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
