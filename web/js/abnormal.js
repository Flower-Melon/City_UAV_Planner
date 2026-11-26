'use strict';

(() => {
  const $ = id => document.getElementById(id);
  const panel = $('abnormal-camera-panel');
  const expand = $('abnormal-camera-expand');
  const dialog = $('abnormal-camera-dialog');
  const panelParent = panel.parentNode, panelNext = panel.nextSibling;
  expand.onclick = () => {
    if (dialog.open) { dialog.close(); return; }
    // 移动原面板，保持跟随镜头持续刷新。
    dialog.append(panel);
    expand.textContent = '还原'; expand.title = '还原实时画面（Esc）';
    expand.setAttribute('aria-expanded', 'true');
    dialog.showModal(); expand.focus();
  };
  dialog.addEventListener('close', () => {
    panelParent.insertBefore(panel, panelNext);
    expand.textContent = '放大'; expand.title = '放大实时画面';
    expand.setAttribute('aria-expanded', 'false'); expand.focus();
  });
  const names = {vehicle: '异常车辆', armed_person: '持刀人员', patient: '病人'};
  const personActions = {patient: {route: 'deliver', label: '派机送药'}, armed_person: {route: 'subdue', label: '派机处置'}};
  const personPhases = {queued: '等待调度', selecting: '正在选机', planning: '正在规划航线',
    loading: '正在起飞挂载药箱', en_route: '正在飞往现场', delivering: '正在投递药品',
    treating: '正在现场处置', returning: '正在返航'};
  const sceneKind = $('scene-kind');
  const sceneAdd = $('scene-add');
  const scenePlace = $('scene-place');
  const storageKey = 'city-abnormal-pending';
  const personJobs = new Map();
  let activePersonActions = new Set();
  let targets = [], selected = null, currentZone = null, picking = false, busy = false, online = false;
  let timer, frameTimer, imageUrl, frameController, frameGeneration = 0, stateGeneration = 0, session = null, listKey = '';
  let lastWatch;
  let pending;
  let pursuit = null;
  const pursuitActive = () => pursuit && !['complete', 'failed', 'stopped'].includes(pursuit.status);
  const isPersonAction = route => route === 'deliver' || route === 'subdue';
  const personBusy = id => personJobs.has(id) || activePersonActions.has(id);
  try {
    const saved = JSON.parse(sessionStorage.getItem(storageKey));
    for (const operation of Array.isArray(saved) ? saved : saved ? [saved] : []) {
      if (isPersonAction(operation.route)) personJobs.set(operation.data.target_id, operation);
      else pending = operation;
    }
    busy = !!pending;
  } catch { /* 禁止存储时仍可使用。 */ }
  const visible = () => !$('abnormal-panel').hidden && !document.hidden;
  const list = $('abnormal-list');

  function positionTargetActions(menu, trigger) {
    const {right, top, bottom} = trigger.getBoundingClientRect();
    const width = document.documentElement.clientWidth;
    const height = document.documentElement.clientHeight;
    const spaceAbove = top - 14, spaceBelow = height - bottom - 14;
    const above = spaceBelow < 128 && spaceAbove > spaceBelow;
    menu.style.right = `${Math.max(8, width - right)}px`;
    menu.style.top = above ? 'auto' : `${bottom + 6}px`;
    menu.style.bottom = above ? `${height - top + 6}px` : 'auto';
    menu.style.maxHeight = `${Math.max(0, above ? spaceAbove : spaceBelow)}px`;
  }

  function closeTargetActions(event) {
    const menu = list.querySelector('.scene-target-actions:popover-open');
    if (menu && !menu.contains(event.target instanceof Node ? event.target : null)) menu.hidePopover();
  }

  document.addEventListener('scroll', closeTargetActions, true);
  window.addEventListener('resize', closeTargetActions);

  async function json(url, options = {}) {
    const controller = new AbortController(), timeout = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch(url, {...options, cache: 'no-store', signal: controller.signal});
      const result = await response.json();
      if (!response.ok) throw Object.assign(Error(result.error || `请求失败（${response.status}）`), {status: response.status});
      return result;
    } finally { clearTimeout(timeout); }
  }
  function message(text, error = false) {
    $('abnormal-operation').textContent = text;
    $('abnormal-operation').dataset.error = String(error);
  }
  function controls() {
    $('abnormal-camera-panel').hidden = !dialog.open && (!visible() || !selected);
    $('scene-preview-empty').hidden = !!selected;
    $('abnormal-zone-pick').disabled = busy || !window.liveMap?.isReady();
    $('abnormal-zone-pick').textContent = picking === 'zone' ? '请点击地图…' : currentZone ? '更换街区' : '地图选择';
    $('abnormal-zone-name').textContent = currentZone?.properties.name || '尚未选择';
    $('abnormal-zone-hint').textContent = currentZone ? '可自动布置，也可在地图上指定位置' : '在地图上点击目标街区';
    $('abnormal-cancel').hidden = !picking;
    for (const button of document.querySelectorAll('[data-escape-target]')) button.disabled = busy || !online;
    for (const button of document.querySelectorAll('[data-person-action-target]')) {
      const id = button.dataset.personActionTarget;
      const target = targets.find(item => item.id === id);
      const acting = personBusy(id);
      const phase = personPhases[personJobs.get(id)?.phase];
      button.disabled = busy || !online || acting || !!target?.error;
      if (target) button.textContent = acting ? `${phase || '执行中'}…` : personActions[target.kind].label;
    }
    for (const button of document.querySelectorAll('[data-pursuit-target]'))
      button.disabled = busy || !online || !!pursuitActive();
    for (const button of document.querySelectorAll('.abnormal-remove'))
      button.disabled = busy || !online || personBusy(button.dataset.target)
        || pursuitActive() && pursuit.vehicle_id === button.dataset.target;
    const kind = sceneKind.value;
    const existing = targets.some(target => target.zone_id === currentZone?.id && target.kind === kind);
    const unavailable = busy || !currentZone || !window.liveMap?.isReady();
    sceneKind.disabled = busy || !!picking;
    sceneAdd.textContent = existing ? '定位' : '添加';
    sceneAdd.disabled = unavailable || !!picking || (!existing && !online);
    scenePlace.hidden = kind === 'vehicle' || existing;
    scenePlace.disabled = unavailable || !!picking || !online;
  }
  function cancelPick() {
    picking = false; window.liveMap?.setPickHandler(null); controls();
  }
  function clearFrame(text = '正在等待实时画面…') {
    ++frameGeneration; frameController?.abort(); clearTimeout(frameTimer); frameTimer = null;
    $('abnormal-image').hidden = true; $('abnormal-image').removeAttribute('src');
    if (imageUrl) URL.revokeObjectURL(imageUrl);
    imageUrl = null; $('abnormal-camera-message').hidden = false;
    $('abnormal-camera-message').textContent = text;
  }
  function select(id) {
    const target = targets.find(target => target.id === id);
    if (target) window.liveMap?.selectZone(target.zone_id);
    if (id !== selected) { selected = id; clearFrame(); }
    render();
    if (visible() && selected) void frame();
  }
  function render() {
    window.liveMap?.setAbnormal(targets, selected, session);
    const localTargets = targets.filter(target => target.zone_id === currentZone?.id);
    $('abnormal-count').textContent = localTargets.length;
    $('abnormal-empty').hidden = localTargets.length > 0;
    $('abnormal-empty').textContent = currentZone ? '尚未布置目标。' : '请先选择街区。';
    const key = JSON.stringify(localTargets.map(t => [t.id, t.kind, t.zone_id]));
    if (key !== listKey) {
      listKey = key;
      list.replaceChildren(...localTargets.map(target => {
        const row = document.createElement('div'); row.className = 'abnormal-target-row';
        const button = document.createElement('button'); button.type = 'button';
        button.className = 'abnormal-target'; button.dataset.targetId = target.id;
        button.title = target.id;
        const label = document.createElement('strong'), id = document.createElement('small');
        const number = target.id.match(/_(\d+)$/)?.[1];
        label.textContent = names[target.kind]; id.textContent = number ? `编号 ${number}` : target.id;
        button.append(label, id); button.onclick = () => select(target.id);
        const remove = document.createElement('button'); remove.type = 'button';
        remove.className = 'abnormal-remove'; remove.textContent = '移除';
        remove.setAttribute('aria-label', `移除 ${names[target.kind]} ${target.id}`);
        remove.onclick = () => void runJob('remove', {target_id: target.id});
        remove.dataset.target = target.id;
        const menu = document.createElement('div'); menu.className = 'scene-target-actions';
        menu.setAttribute('popover', 'auto');
        const more = document.createElement('button'); more.type = 'button'; more.className = 'scene-target-more';
        more.textContent = '⋯'; more.title = '更多操作';
        more.setAttribute('aria-label', `${names[target.kind]} ${number ?? target.id} 的操作`);
        more.popoverTargetElement = menu;
        menu.addEventListener('beforetoggle', event => {
          if (event.newState === 'open') positionTargetActions(menu, more);
        });
        menu.addEventListener('click', event => {
          if (!event.target.closest('button')) return;
          menu.hidePopover();
          more.focus({preventScroll: true});
        });
        row.append(button, more, menu);
        if (target.kind === 'vehicle') {
          const escape = document.createElement('button'); escape.type = 'button';
          escape.dataset.escapeTarget = target.id;
          escape.textContent = '启动逃逸';
          escape.onclick = () => void runJob('escape', {target_id: target.id});
          const follow = document.createElement('button'); follow.type = 'button';
          follow.dataset.pursuitTarget = target.id;
          follow.textContent = '开始追踪';
          follow.onclick = () => void runJob('pursue', {target_id: target.id});
          menu.append(escape, follow);
        } else if (personActions[target.kind]) {
          const {route, label} = personActions[target.kind];
          const action = document.createElement('button'); action.type = 'button';
          action.textContent = label;
          action.dataset.personActionTarget = target.id;
          action.setAttribute('aria-label', `${label} ${names[target.kind]} ${target.id}`);
          action.onclick = () => {
            select(target.id);
            void runJob(route, {target_id: target.id});
          };
          menu.append(action);
        }
        menu.append(remove);
        return row;
      }));
    }
    for (const button of list.querySelectorAll('.abnormal-target')) {
      button.setAttribute('aria-pressed', String(button.dataset.targetId === selected));
    }
    renderPursuit(); controls();
  }
  function renderPursuit() {
    if (pursuit) window.pursuitView?.update(pursuit);
  }
  function setZone(zone) {
    if (!zone || zone.id === currentZone?.id) return;
    cancelPick(); currentZone = zone;
    selected = targets.find(target => target.zone_id === zone.id)?.id || null;
    clearFrame(selected ? '正在等待实时画面…' : '添加场景后可查看现场画面');
    message(`已选择${zone.properties.name}，可添加场景或指定位置。`);
    render();
  }
  function locate(target) {
    select(target.id); window.liveMap?.centerTarget(target.id);
    message(`已定位到${target.zone_name}的${names[target.kind]}。`);
  }
  function addScene(kind) {
    if (busy || !currentZone) return;
    const existing = targets.find(target => target.zone_id === currentZone.id && target.kind === kind);
    if (existing) { locate(existing); return; }
    if (!online) return;
    void runJob('spawn', {kind, zone_id: currentZone.id, behavior: 'speeding'});
  }
  function pickPosition(kind) {
    if (busy || !online || !currentZone) return;
    const zone = currentZone;
    window.dispatchEvent(new Event('workspace:show-map'));
    picking = kind; controls();
    message(`请在${zone.properties.name}附近点击${names[kind]}出现的位置，按 Esc 取消。`);
    window.liveMap.setPickHandler(position => {
      if (window.liveMap.spawnZoneAt(position)?.id !== zone.id) {
        message(`请在${zone.properties.name}所属范围内选点，或取消后更换街区。`, true); return;
      }
      const existing = targets.find(target => target.zone_id === zone.id && target.kind === kind);
      if (existing) { cancelPick(); locate(existing); return; }
      window.liveMap.setPendingPoint(position);
      void runJob('spawn', {kind, zone_id: zone.id, position, behavior: 'speeding'});
    });
  }
  function savePending(operation, running) {
    if (isPersonAction(operation.route)) {
      if (running) personJobs.set(operation.data.target_id, operation);
      else personJobs.delete(operation.data.target_id);
    } else {
      pending = running ? operation : null;
      busy = running;
    }
    const saved = pending ? [pending, ...personJobs.values()] : [...personJobs.values()];
    try {
      if (saved.length) sessionStorage.setItem(storageKey, JSON.stringify(saved));
      else sessionStorage.removeItem(storageKey);
    } catch { /* 存储可选。 */ }
  }
  function finishJob(operation) {
    savePending(operation, false);
    if (!isPersonAction(operation.route)) window.liveMap?.setPendingPoint(null);
    controls();
  }
  async function runJob(route, data) {
    if (busy || personBusy(data.target_id)) return;
    cancelPick();
    if (route === 'pursue' || isPersonAction(route)) data = {...data, flight: window.getMissionFlightSettings()};
    const operation = {route, data: {request_id: crypto.randomUUID(), ...data}};
    savePending(operation, true);
    controls();
    message(route === 'spawn' ? `正在生成${names[data.kind]}，请等待仿真返回…`
      : route === 'escape' ? '正在切换车辆路线…'
      : route === 'pursue' ? '正在检查可用于追踪的无人机…'
      : route === 'deliver' ? '正在提交送药请求…'
      : route === 'subdue' ? '正在提交处置请求…'
      : route === 'remove' ? `正在移除 ${data.target_id}…` : '正在设置车辆行为…');
    try {
      await json(`/api/abnormal/${route}`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(operation.data)});
    } catch (error) {
      if (error.status >= 400 && error.status < 500) {
        finishJob(operation);
        message(error.message, true); return;
      }
      message(`提交结果尚未确认：${error.message}；正在查询操作记录。`, true);
    }
    void pollJob(operation);
  }
  async function pollJob(operation) {
    const {route, data} = operation;
    const personAction = isPersonAction(route);
    try {
      const job = await json(`/api/abnormal/jobs/${encodeURIComponent(data.request_id)}`);
      if (job.status === 'running') {
        if (personAction && personPhases[job.phase] &&
            (job.phase !== operation.phase || job.drone_id !== operation.drone_id)) {
          operation.phase = job.phase;
          operation.drone_id = job.drone_id;
          controls();
          const drone = job.drone_id ? `${job.drone_id} ` : '';
          const phase = job.phase === 'returning'
            ? `${route === 'deliver' ? '药品已投递' : '处置已完成'}，${drone}正在返航`
            : drone + personPhases[job.phase];
          message(`${data.target_id} · ${phase}。`);
        }
        setTimeout(() => pollJob(operation), personAction && !['en_route', 'delivering', 'treating', 'returning'].includes(job.phase) ? 300 : 800);
        return;
      }
      if (personAction) {
        const result = job.status === 'completed' ? `${route === 'deliver' ? '药品已投递' : '持刀人员已制服'}，无人机已返航降落。`
          : `操作失败：${job.error}`;
        message(`${data.target_id} · ${result}`, job.status !== 'completed');
      } else if (job.status === 'completed') {
        ++stateGeneration;
        if (route === 'remove') {
          targets = targets.filter(target => target.id !== job.result.target_id);
          if (selected === job.result.target_id) {
            selected = targets.find(target => target.zone_id === currentZone?.id)?.id || null;
            clearFrame(selected ? '正在等待实时画面…' : '选择一个已生成目标以查看画面');
          }
          render();
          message(`已移除：${job.result.target_id}`);
        } else {
          selected = job.result.target_id; clearFrame();
          message(job.result.warning || (route === 'escape' ? '车辆已启动逃逸。'
            : route === 'pursue' ? '正在调度追踪，可在「机队」查看追踪镜头。'
            : route === 'spawn'
            ? `已在${job.result.zone_name}添加${names[data.kind]}。` : '车辆行为已更新。'), !!job.result.warning);
        }
      } else message(`操作失败：${job.error}`, true);
      if (!personAction) await poll();
      finishJob(operation);
    } catch (error) {
      if (error.status === 404) {
        finishJob(operation);
        message(personAction ? `${data.target_id} · 未找到处置记录，请确认结果后再派机。`
          : '未找到操作记录（服务可能已重启）。请先查看已有目标，确认上次结果后再生成。', true);
        return;
      }
      // 保留待确认请求，防止网络失败后重复生成。
      message(personAction ? `${data.target_id} · 处置状态待确认，正在重试查询。`
        : `操作状态待确认：${error.message}。正在重试查询，请勿重复生成。`, true);
      setTimeout(() => pollJob(operation), 2000);
    }
  }
  async function poll() {
    clearTimeout(timer);
    const started = performance.now();
    const generation = stateGeneration;
    if (!document.hidden) {
      try {
        const target = selected;
        const watch = visible() && !!target && (lastWatch?.target !== target || started - lastWatch.at >= 700);
        const data = await json(`/api/abnormal/state?target=${encodeURIComponent(target || '')}&watch=${watch ? 1 : 0}`);
        if (generation !== stateGeneration) { timer = setTimeout(poll, 0); return; }
        if (session !== data.session) {
          session = data.session;
          lastWatch = null;
          clearFrame(selected ? '正在等待实时画面…' : '选择一个已生成目标以查看画面');
        }
        if (watch && data.connected) lastWatch = {target, at: started};
        online = data.connected; targets = data.targets;
        activePersonActions = new Set(data.active_person_actions || []);
        const previousStatus = pursuit?.status;
        pursuit = data.pursuit;
        if (previousStatus !== pursuit?.status) window.dispatchEvent(new Event('vehicle-pursuit:change'));
        if (!currentZone && pending?.data.zone_id) setZone(window.liveMap?.getZone(pending.data.zone_id));
        if (!targets.some(t => t.id === selected && t.zone_id === currentZone?.id)) {
          const changed = selected !== null;
          selected = targets.find(target => target.zone_id === currentZone?.id)?.id || null;
          if (changed || selected) clearFrame(selected ? '正在等待实时画面…' : '选择一个已生成目标以查看画面');
        }
        $('abnormal-connection').textContent = online ? '已连接' : '请启动 Play';
        $('abnormal-connection').title = data.camera_error || '';
        if (!online) clearFrame('等待 AirSim Play 仿真');
        render();
        if (visible() && selected && !frameController && !frameTimer) void frame();
      } catch (error) {
        online = false; targets = []; clearFrame('监控连接失败，正在重试'); render();
        $('abnormal-connection').textContent = '连接失败'; $('abnormal-connection').title = error.message;
      }
    }
    const interval = online && targets.length && !document.hidden ? 200 : 700;
    clearTimeout(timer); timer = setTimeout(poll, Math.max(0, interval - (performance.now() - started)));
  }
  async function frame() {
    clearTimeout(frameTimer); frameTimer = null;
    if (!visible() || !selected || !online || frameController) return;
    const id = selected, current = frameGeneration, controller = new AbortController();
    frameController = controller;
    const timeout = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch(`/api/abnormal/camera?target=${encodeURIComponent(id)}&t=${Date.now()}`, {cache: 'no-store', signal: controller.signal});
      if (!response.ok) { const result = await response.json(); throw Error(result.error); }
      const blob = await response.blob();
      if (current !== frameGeneration || id !== selected || !visible() || controller.signal.aborted) return;
      if (session !== response.headers.get('X-Simulation-Session')) return;
      const old = imageUrl; imageUrl = URL.createObjectURL(blob); $('abnormal-image').src = imageUrl;
      $('abnormal-image').hidden = false; $('abnormal-camera-message').hidden = true;
      if (old) URL.revokeObjectURL(old);
    } catch (error) {
      if (!controller.signal.aborted && current === frameGeneration) {
        if (imageUrl) URL.revokeObjectURL(imageUrl);
        imageUrl = null; $('abnormal-image').hidden = true;
        $('abnormal-camera-message').hidden = false; $('abnormal-camera-message').textContent = `画面暂不可用：${error.message}`;
      }
    } finally {
      clearTimeout(timeout); if (frameController === controller) frameController = null;
      if (visible() && selected && online) frameTimer = setTimeout(frame, 500);
    }
  }
  $('abnormal-zone-pick').onclick = () => {
    if (busy || !window.liveMap?.isReady()) return;
    window.dispatchEvent(new Event('workspace:show-map'));
    picking = 'zone'; controls(); message('请在地图上点击要布置场景的街区，按 Esc 取消。');
    window.liveMap.setPickHandler(position => {
      const zone = window.liveMap.spawnZoneAt(position);
      if (!zone) { message('此处没有可用街区，请重新选点。', true); return; }
      cancelPick(); setZone(zone); window.liveMap.selectZone(zone.id);
      message(`已选择${zone.properties.name}，可添加场景或指定位置。`);
    });
  };
  $('abnormal-cancel').onclick = () => { cancelPick(); message('已取消地图选点。'); };
  sceneKind.onchange = controls;
  sceneAdd.onclick = () => addScene(sceneKind.value);
  scenePlace.onclick = () => pickPosition(sceneKind.value);
  window.vehiclePursuit = {escape: id => runJob('escape', {target_id: id}),
    start: id => runJob('pursue', {target_id: id}),
    get active() { return !!pursuitActive(); }, get busy() { return busy; }, get online() { return online; }};
  window.addEventListener('keydown', event => { if (event.key === 'Escape' && picking) $('abnormal-cancel').click(); });
  window.addEventListener('abnormal:select', event => { $('abnormal-tab').click(); select(event.detail.id); });
  window.addEventListener('map:selection', event => {
    if (visible() && !busy && event.detail.feature?.geometry.type !== 'Point'
      && event.detail.feature?.properties.category !== 'transport') setZone(event.detail.feature);
  });
  window.addEventListener('sidebar:change', event => {
    closeTargetActions(event);
    cancelPick();
    if (visible() && !busy) setZone(window.liveMap?.selectedZone());
    clearFrame(selected ? '正在等待实时画面…' : '选择一个已生成目标以查看画面');
  });
  document.addEventListener('visibilitychange', () => { if (document.hidden) { cancelPick(); clearFrame(); } });
  window.addEventListener('pagehide', () => { clearTimeout(timer); clearFrame(''); window.liveMap?.setPickHandler(null); });
  controls();
  if (pending || personJobs.size) message('正在恢复操作状态…');
  if (pending) void pollJob(pending);
  for (const operation of personJobs.values()) void pollJob(operation);
  void poll();
})();
