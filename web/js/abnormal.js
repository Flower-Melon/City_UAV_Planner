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
  const scenes = {
    patient: {title: '医疗求助', icon: '+', description: '随机布置在街区人行道，也可手动选择位置。'},
    armed_person: {title: '持刀人员', icon: '!', description: '随机布置巡游人员，与医疗救援现场保持距离。'},
    vehicle: {title: '异常车辆', icon: '↗', description: '沿所选街区外围道路行驶，可调整驾驶行为。'}
  };
  const sceneCards = new Map();
  const modes = {mixed: '混合', speeding: '超速行驶', wrong_way: '逆向行驶（超速）'};
  const actions = ['原地停留', '步行', '奔跑', '持刀威胁', '痛苦', '倒地中', '躺卧'];
  const storageKey = 'city-abnormal-pending';
  let targets = [], selected = null, currentZone = null, picking = false, busy = false, online = false;
  let timer, frameTimer, imageUrl, frameController, frameGeneration = 0, stateGeneration = 0, session = null, listKey = '';
  let pending;
  try { pending = JSON.parse(sessionStorage.getItem(storageKey)); } catch { /* 禁止存储时仍可使用。 */ }
  const visible = () => !$('abnormal-panel').hidden && !document.hidden;

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
    $('abnormal-zone-pick').disabled = busy || !window.liveMap?.isReady();
    $('abnormal-zone-pick').textContent = picking === 'zone' ? '请点击地图…' : currentZone ? '更换街区' : '地图选择';
    $('abnormal-zone-name').textContent = currentZone?.properties.name || '尚未选择';
    $('abnormal-zone-hint').textContent = currentZone ? '可自动布置，也可在地图上指定位置' : '在地图上点击目标街区';
    $('abnormal-cancel').hidden = !picking;
    $('abnormal-apply').disabled = busy || !online;
    for (const button of document.querySelectorAll('.abnormal-remove')) button.disabled = busy || !online;
    for (const [kind, card] of sceneCards) {
      const target = targets.find(target => target.zone_id === currentZone?.id && target.kind === kind);
      const working = pending?.data.kind === kind && pending.data.zone_id === currentZone?.id
        || pending?.route === 'remove' && pending.data.target_id === target?.id;
      card.root.dataset.active = String(!!target);
      card.state.textContent = working ? pending.route === 'remove' ? '正在移除…' : '正在添加…'
        : target ? target.error ? '需要关注' : '现场运行中' : !currentZone ? '待选街区' : !online ? '等待连接' : '待布置';
      card.description.textContent = target ? target.error ? '目标运行异常，可查看详情或重新布置。'
        : target.kind === 'vehicle' ? `${modes[target.active_behavior] || '行驶中'} · ${(target.speed_m_s * 3.6).toFixed(1)} km/h`
        : `当前状态：${actions[target.action] || '等待更新'}` : scenes[kind].description;
      card.primary.textContent = target ? '定位' : working ? '正在添加…' : '添加';
      card.primary.setAttribute('aria-label', `${scenes[kind].title}：${target ? '定位' : '添加'}`);
      card.primary.classList.toggle('abnormal-add', !target);
      card.primary.disabled = busy || !currentZone || !window.liveMap?.isReady() || !target && !online;
      card.secondary.textContent = target ? '移除' : '指定位置';
      card.secondary.hidden = kind === 'vehicle' && !target;
      card.secondary.setAttribute('aria-label', `${scenes[kind].title}：${target ? '移除' : '指定位置'}`);
      card.secondary.disabled = busy || !currentZone || !online || !window.liveMap?.isReady();
      card.root.setAttribute('aria-busy', String(!!working));
    }
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
    window.liveMap?.setAbnormal(targets, selected);
    const localTargets = targets.filter(target => target.zone_id === currentZone?.id);
    $('abnormal-count').textContent = localTargets.length;
    $('abnormal-empty').hidden = localTargets.length > 0;
    $('abnormal-empty').textContent = currentZone ? '尚未布置场景，可从上方添加。' : '选择街区后查看现场目标。';
    const key = JSON.stringify([localTargets.map(t => [t.id, t.kind, t.zone_id]), selected]);
    if (key !== listKey) {
      listKey = key;
      $('abnormal-list').replaceChildren(...localTargets.map(target => {
        const row = document.createElement('div'); row.className = 'abnormal-target-row';
        const button = document.createElement('button'); button.type = 'button';
        button.className = 'abnormal-target'; button.dataset.kind = target.kind;
        button.setAttribute('aria-pressed', String(target.id === selected));
        const label = document.createElement('strong'), id = document.createElement('small');
        label.textContent = names[target.kind]; id.textContent = `${target.zone_name} · ${target.id}`;
        button.append(label, id); button.onclick = () => select(target.id);
        const remove = document.createElement('button'); remove.type = 'button';
        remove.className = 'abnormal-remove'; remove.textContent = '移除';
        remove.setAttribute('aria-label', `移除 ${names[target.kind]} ${target.id}`);
        remove.onclick = () => void runJob('remove', {target_id: target.id});
        row.append(button, remove);
        return row;
      }));
    }
    const target = targets.find(t => t.id === selected);
    $('abnormal-detail').hidden = !target;
    if (target) {
      $('abnormal-title').textContent = `${target.zone_name} · ${names[target.kind]} · ${target.id.split('_').pop()}`;
      $('abnormal-position').textContent = `UE X ${target.position_m[0].toFixed(1)} · Y ${target.position_m[1].toFixed(1)} · Z ${target.position_m[2].toFixed(1)} m`;
      $('abnormal-action').textContent = target.error ? `运行异常：${target.error}` : target.kind === 'vehicle'
        ? `${modes[target.behavior] || target.behavior} · 当前 ${modes[target.active_behavior] || target.active_behavior} · ${(target.speed_m_s * 3.6).toFixed(1)} km/h`
        : `当前状态：${actions[target.action] || '等待更新'}`;
      $('abnormal-action').dataset.error = String(!!target.error);
      $('abnormal-vehicle-controls').hidden = target.kind !== 'vehicle';
      if ($('abnormal-behavior').dataset.target !== selected) {
        $('abnormal-behavior').value = target.behavior || 'speeding';
        $('abnormal-behavior').dataset.target = selected;
      }
    }
    controls();
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
    if (busy || !online || !currentZone) return;
    const existing = targets.find(target => target.zone_id === currentZone.id && target.kind === kind);
    if (existing) { locate(existing); return; }
    void runJob('spawn', {kind, zone_id: currentZone.id, behavior: 'speeding'});
  }
  function pickPosition(kind) {
    if (busy || !online || !currentZone) return;
    const zone = currentZone;
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
  function savePending(value) {
    pending = value;
    try { if (value) sessionStorage.setItem(storageKey, JSON.stringify(value)); else sessionStorage.removeItem(storageKey); } catch { /* 存储可选。 */ }
  }
  async function runJob(route, data) {
    if (busy) return;
    cancelPick(); busy = true;
    const operation = {route, data: {request_id: crypto.randomUUID(), ...data}};
    savePending(operation);
    controls();
    message(route === 'spawn' ? `正在生成${names[data.kind]}，请等待仿真返回…`
      : route === 'remove' ? `正在移除 ${data.target_id}…` : '正在设置车辆行为…');
    try {
      await json(`/api/abnormal/${route}`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(operation.data)});
    } catch (error) {
      if (error.status >= 400 && error.status < 500) {
        savePending(null); busy = false; window.liveMap?.setPendingPoint(null); controls();
        message(error.message, true); return;
      }
      message(`提交结果尚未确认：${error.message}；正在查询操作记录。`, true);
    }
    void pollJob();
  }
  async function pollJob() {
    if (!pending) return;
    busy = true; controls();
    try {
      const job = await json(`/api/abnormal/jobs/${encodeURIComponent(pending.data.request_id)}`);
      if (job.status === 'running') { setTimeout(pollJob, 800); return; }
      if (job.status === 'completed') {
        ++stateGeneration;
        if (pending.route === 'remove') {
          targets = targets.filter(target => target.id !== job.result.target_id);
          if (selected === job.result.target_id) {
            selected = targets.find(target => target.zone_id === currentZone?.id)?.id || null;
            clearFrame(selected ? '正在等待实时画面…' : '选择一个已生成目标以查看画面');
          }
          render();
          message(`已移除：${job.result.target_id}`);
        } else {
          selected = job.result.target_id; clearFrame();
          message(job.result.warning || (pending.route === 'spawn'
            ? `已在${job.result.zone_name}添加${names[pending.data.kind]}。` : '车辆行为已更新。'), !!job.result.warning);
        }
      } else message(`操作失败：${job.error}`, true);
      await poll();
      savePending(null); busy = false; window.liveMap?.setPendingPoint(null); controls();
    } catch (error) {
      if (error.status === 404) {
        savePending(null); busy = false; window.liveMap?.setPendingPoint(null); controls();
        message('未找到操作记录（服务可能已重启）。请先查看已有目标，确认上次结果后再生成。', true);
        return;
      }
      // 保留待确认请求，防止网络失败后重复生成。
      message(`操作状态待确认：${error.message}。正在重试查询，请勿重复生成。`, true);
      setTimeout(pollJob, 2000);
    }
  }
  async function poll() {
    clearTimeout(timer);
    const generation = stateGeneration;
    if (!document.hidden) {
      try {
        const data = await json(`/api/abnormal/state?target=${encodeURIComponent(selected || '')}&watch=${visible() && !!selected ? 1 : 0}`);
        if (generation !== stateGeneration) { timer = setTimeout(poll, 0); return; }
        if (session !== data.session) {
          session = data.session;
          clearFrame(selected ? '正在等待实时画面…' : '选择一个已生成目标以查看画面');
        }
        online = data.connected; targets = data.targets;
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
    clearTimeout(timer); timer = setTimeout(poll, 700);
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
    picking = 'zone'; controls(); message('请在地图上点击要布置场景的街区，按 Esc 取消。');
    window.liveMap.setPickHandler(position => {
      const zone = window.liveMap.spawnZoneAt(position);
      if (!zone) { message('此处没有可用街区，请重新选点。', true); return; }
      cancelPick(); setZone(zone); window.liveMap.selectZone(zone.id);
      message(`已选择${zone.properties.name}，可添加场景或指定位置。`);
    });
  };
  $('abnormal-cancel').onclick = () => { cancelPick(); message('已取消地图选点。'); };
  $('abnormal-locate').onclick = () => window.liveMap?.centerTarget(selected);
  $('abnormal-apply').onclick = () => void runJob('behavior', {target_id: selected, behavior: $('abnormal-behavior').value});
  window.addEventListener('keydown', event => { if (event.key === 'Escape' && picking) $('abnormal-cancel').click(); });
  window.addEventListener('abnormal:select', event => { $('abnormal-tab').click(); select(event.detail.id); });
  window.addEventListener('map:selection', event => {
    if (visible() && !busy && event.detail.feature?.geometry.type !== 'Point'
      && event.detail.feature?.properties.category !== 'transport') setZone(event.detail.feature);
  });
  window.addEventListener('sidebar:change', () => {
    cancelPick();
    if (visible() && !busy) setZone(window.liveMap?.selectedZone());
    clearFrame(selected ? '正在等待实时画面…' : '选择一个已生成目标以查看画面');
  });
  document.addEventListener('visibilitychange', () => { if (document.hidden) { cancelPick(); clearFrame(); } });
  window.addEventListener('pagehide', () => { clearTimeout(timer); clearFrame(''); window.liveMap?.setPickHandler(null); });
  for (const [kind, scene] of Object.entries(scenes)) {
    const root = document.createElement('article'); root.className = 'abnormal-scene'; root.dataset.kind = kind;
    const heading = document.createElement('div'); heading.className = 'abnormal-scene-heading';
    const icon = document.createElement('span'); icon.className = 'abnormal-scene-icon'; icon.textContent = scene.icon;
    icon.setAttribute('aria-hidden', 'true');
    const title = document.createElement('strong'); title.textContent = scene.title;
    const state = document.createElement('span'); state.className = 'abnormal-scene-state';
    heading.append(icon, title, state);
    const description = document.createElement('p'), actions = document.createElement('div');
    actions.className = 'abnormal-scene-actions';
    const primary = document.createElement('button'), secondary = document.createElement('button');
    primary.type = secondary.type = 'button';
    primary.onclick = () => {
      const target = targets.find(target => target.zone_id === currentZone?.id && target.kind === kind);
      if (target) locate(target); else addScene(kind);
    };
    secondary.onclick = () => {
      const target = targets.find(target => target.zone_id === currentZone?.id && target.kind === kind);
      if (target) void runJob('remove', {target_id: target.id}); else pickPosition(kind);
    };
    actions.append(primary, secondary); root.append(heading, description, actions);
    sceneCards.set(kind, {root, state, description, primary, secondary}); $('abnormal-scenes').append(root);
  }
  controls();
  if (pending) { message('正在恢复上一次操作状态…'); void pollJob(); }
  void poll();
})();
