'use strict';

(() => {
  const $ = id => document.getElementById(id);
  const colors = ['#e6553f', '#237fc1', '#1b9c78', '#925bcc', '#d89120'];
  const container = $('drone-cameras'), panel = $('uav-camera-panel'), status = $('airsim-status');
  const filter = $('camera-filter'), dialog = $('camera-dialog'), expand = $('camera-expand');
  const back = $('camera-back'), close = $('camera-close'), wallContext = $('camera-wall-context');
  const fleetBody = panel.querySelector('.fleet-body');
  const panelParent = panel.parentNode, panelNext = panel.nextSibling;
  const cards = new Map();
  const flightDialog = $('fleet-flight-dialog');
  const flightForm = $('fleet-flight-form'), flightDrone = $('fleet-flight-drone');
  const flightFields = $('fleet-flight-fields'), flightInputs = $('fleet-flight-inputs');
  const flightStatus = $('fleet-flight-status'), flightReset = $('fleet-flight-reset'), flightSave = $('fleet-flight-save');
  const flightParameters = Object.entries(window.fleetFlightFields);
  const flightStorageKey = 'city-fleet-unified-flight-profiles';
  const flightDefaults = Object.fromEntries(flightParameters.map(([key, field]) => [key, field.defaultValue]));
  const flightProfiles = new Map(), flightDrafts = new Map();
  let flightNames = [];

  function storedFlightProfile(values) {
    return Object.fromEntries(flightParameters.flatMap(([key, field]) => {
      const value = values?.[key];
      return Number.isFinite(value) && value > 0 && value >= (field.min ?? 0) ? [[key, value]] : [];
    }));
  }

  function mergeTaskFlightProfiles(profiles) {
    // 多个旧任务保存过同一字段时，优先沿用区域巡查的值。
    const priority = ['response', 'delivery', 'pursuit', 'monitor', 'search'];
    return Object.assign({}, ...priority.map(task => storedFlightProfile(profiles?.[task])));
  }

  try {
    const saved = JSON.parse(localStorage.getItem(flightStorageKey));
    let profiles;
    if (saved) {
      Object.assign(flightDefaults, storedFlightProfile(saved.defaults));
      profiles = saved.by_drone || {};
    } else {
      const previousDefaults = JSON.parse(localStorage.getItem('city-fleet-task-flight-defaults'));
      const previousProfiles = JSON.parse(localStorage.getItem('city-fleet-task-flight-profiles'));
      Object.assign(flightDefaults, mergeTaskFlightProfiles(previousDefaults));
      profiles = previousProfiles
        ? Object.fromEntries(Object.entries(previousProfiles).map(([name, profile]) => [name, mergeTaskFlightProfiles(profile)]))
        : JSON.parse(localStorage.getItem('city-fleet-flight-profiles')) || {};
    }
    for (const [name, profile] of Object.entries(profiles)) {
      const values = storedFlightProfile(profile);
      if (Object.keys(values).length) flightProfiles.set(name, values);
    }
  } catch { /* 本地存储不可用时仍可编辑当前页面。 */ }

  window.getMissionFlightSettings = () => ({...flightDefaults, by_drone: Object.fromEntries(flightProfiles)});

  const inputs = flightParameters.map(([key, field]) => {
    const row = document.createElement('div');
    const label = document.createElement('label');
    const title = document.createElement('span');
    title.textContent = field.label;
    const control = document.createElement('span');
    control.className = 'fleet-flight-input';
    const input = document.createElement('input');
    Object.assign(input, {name: key, type: 'number', min: field.min ?? 0, step: 'any', required: true});
    const unit = document.createElement('small');
    unit.textContent = field.unit;
    const hint = document.createElement('small');
    hint.id = 'fleet-flight-hint-' + key;
    hint.className = 'fleet-flight-hint';
    hint.textContent = field.description;
    input.setAttribute('aria-describedby', hint.id);
    control.append(input, unit);
    label.append(title, control);
    row.append(label, hint);
    flightInputs.append(row);
    return input;
  });

  function loadFlightProfile() {
    const name = flightDrone.value;
    const saved = flightProfiles.get(name);
    const draft = flightDrafts.get(name);
    for (const input of inputs) {
      input.value = draft?.[input.name] ?? saved?.[input.name] ?? flightDefaults[input.name];
      input.setCustomValidity('');
    }
    flightFields.disabled = !name;
    flightSave.disabled = !name || !draft;
    flightReset.disabled = !name || !(saved || draft);
    flightStatus.textContent = !name ? '连接仿真后可选择无人机'
      : draft ? '有未保存的修改' : '保存后用于新提交的任务';
  }

  function loadFlightDrones() {
    const previous = flightDrone.value;
    const choices = [...new Set([...flightNames, ...flightProfiles.keys()])];
    const options = choices.map(name => new Option(name, name));
    flightDrone.replaceChildren(...(options.length ? options : [new Option('暂无无人机', '')]));
    if (choices.includes(previous)) flightDrone.value = previous;
    flightDrone.disabled = !choices.length;
    loadFlightProfile();
  }

  function updateFlightDrones(names) {
    if (names.length === flightNames.length && names.every((name, i) => name === flightNames[i])) return;
    flightNames = names;
    loadFlightDrones();
  }

  function storeFlightProfiles(message) {
    loadFlightDrones();
    try {
      localStorage.setItem(flightStorageKey, JSON.stringify({
        defaults: flightDefaults, by_drone: Object.fromEntries(flightProfiles)
      }));
      flightStatus.textContent = message;
    } catch { flightStatus.textContent = '参数已应用到当前页面'; }
  }

  $('fleet-flight-open').onclick = () => flightDialog.showModal();
  $('fleet-flight-close').onclick = () => flightDialog.close();
  flightDrone.onchange = loadFlightProfile;
  flightFields.oninput = event => {
    event.target.setCustomValidity('');
    flightDrafts.set(flightDrone.value, Object.fromEntries(inputs.map(input => [input.name, input.value])));
    flightStatus.textContent = '有未保存的修改';
    flightSave.disabled = flightReset.disabled = false;
  };
  flightForm.onsubmit = event => {
    event.preventDefault();
    const name = flightDrone.value;
    if (!name) return;
    for (const input of inputs) {
      if (!Number.isFinite(input.valueAsNumber) || input.valueAsNumber <= 0) {
        input.setCustomValidity('请输入大于 0 的数值');
        input.reportValidity();
        return;
      }
    }
    flightProfiles.set(name, Object.fromEntries(inputs.map(input => [input.name, input.valueAsNumber])));
    flightDrafts.delete(name);
    storeFlightProfiles(name + ' 参数已保存');
  };
  flightReset.onclick = () => {
    const name = flightDrone.value;
    flightDrafts.delete(name);
    flightProfiles.delete(name);
    storeFlightProfiles(name + ' 已恢复默认');
  };
  loadFlightDrones();
  let records = [], connected = false, focused = null, preview = null, wallOpener = null;
  const roster = $('fleet-roster');
  const rosterRows = new Map();
  let stateRequest = null, timer, generation = 0;
  const params = new URLSearchParams(location.search);
  const video = {mode: params.get('mode') || 'capture', fps: params.get('fps')};
  const stats = window.__videoStats = {frames: [], received: 0, events: []};
  const note = (event, detail = '') => {
    stats.events.push([Date.now(), event, String(detail)]);
    if (stats.events.length > 2000) stats.events.shift();
  };
  const HEADER_END = new Uint8Array([13, 10, 13, 10]);
  const visible = () => !document.hidden && (!panel.hidden || dialog.open);

  function stopStream(card, message) {
    card.stream?.controller.abort(); card.stream = null;
    card.canvas.hidden = true; card.placeholder.hidden = false; card.placeholder.textContent = message;
  }
  function stopAll(message) { for (const card of cards.values()) stopStream(card, message); }
  function openMonitor(name = null, opener = expand) {
    focused = name;
    if (!dialog.open) {
      wallOpener = opener;
      dialog.append(panel);
      dialog.showModal();
      expand.setAttribute('aria-expanded', 'true');
    }
    render();
    fleetBody.scrollTop = 0;
    if (focused) back.focus();
  }
  expand.onclick = () => openMonitor();
  close.onclick = () => dialog.close();
  back.onclick = () => {
    const previous = focused;
    focused = null;
    render();
    (cards.get(previous)?.zoom ?? close).focus();
  };
  dialog.addEventListener('close', () => {
    panelParent.insertBefore(panel, panelNext); focused = null;
    expand.setAttribute('aria-expanded', 'false');
    render();
    wallOpener?.focus({preventScroll: true});
    wallOpener = null;
  });

  function createCard(name, index) {
    const root = document.createElement('article'); root.className = 'drone-card'; root.dataset.drone = name;
    root.style.setProperty('--drone-color', colors[index % colors.length]);
    const heading = document.createElement('div'); heading.className = 'drone-heading';
    const title = document.createElement('strong'); title.textContent = name;
    const badge = document.createElement('span'); badge.className = 'drone-state';
    const locate = document.createElement('button'); locate.type = 'button'; locate.textContent = '定位';
    locate.setAttribute('aria-label', `定位 ${name}`);
    locate.onclick = () => {
      if (dialog.open) dialog.close();
      window.dispatchEvent(new Event('workspace:show-map'));
      window.liveMap?.centerDrone(name);
    };
    const actions = document.createElement('div'); actions.className = 'drone-actions';
    heading.append(title, badge, actions);
    const view = document.createElement('div'); view.className = 'camera-view';
    const canvas = document.createElement('canvas'); canvas.hidden = true;
    canvas.setAttribute('role', 'img'); canvas.setAttribute('aria-label', `${name} 第三人称实时画面`);
    const placeholder = document.createElement('div'); placeholder.className = 'camera-placeholder';
    placeholder.textContent = '正在连接画面…';
    const overlay = document.createElement('div'); overlay.className = 'camera-stats'; overlay.hidden = params.get('stats') !== '1';
    const zoom = document.createElement('button'); zoom.type = 'button'; zoom.className = 'drone-zoom';
    zoom.textContent = '放大'; zoom.setAttribute('aria-label', `放大 ${name} 画面`); zoom.onclick = () => openMonitor(name, zoom);
    const position = document.createElement('p'); position.className = 'drone-position';
    actions.append(locate, zoom);
    view.append(canvas, placeholder, overlay); root.append(heading, view, position); container.append(root);
    const card = {root, badge, locate, zoom, canvas, context: canvas.getContext('2d'), placeholder, overlay, position,
      stream: null, frames: [], lastFrame: 0};
    cards.set(name, card); return card;
  }
  function concat(a, b) {
    const out = new Uint8Array(a.length + b.length); out.set(a); out.set(b, a.length); return out;
  }
  function indexOf(buffer, pattern) {
    outer: for (let i = 0; i + pattern.length <= buffer.length; i++) {
      for (let j = 0; j < pattern.length; j++) if (buffer[i + j] !== pattern[j]) continue outer;
      return i;
    }
    return -1;
  }
  // 每张卡片独立解码，只保留最新帧；多机画面互不覆盖。
  async function readStream(card, name, url, controller) {
    let pending = null, drawing = false;
    const draw = async () => {
      if (drawing || !pending) return;
      drawing = true;
      const {bytes, captured} = pending; pending = null;
      try {
        const bitmap = await createImageBitmap(new Blob([bytes], {type: 'image/jpeg'}));
        if (controller.signal.aborted) { bitmap.close(); return; }
        if (card.canvas.width !== bitmap.width || card.canvas.height !== bitmap.height) {
          card.canvas.width = bitmap.width; card.canvas.height = bitmap.height;
        }
        card.context.drawImage(bitmap, 0, 0); bitmap.close();
        card.canvas.hidden = false; card.placeholder.hidden = true; card.lastFrame = Date.now();
        card.frames.push([card.lastFrame, captured]);
        card.frames = card.frames.filter(frame => card.lastFrame - frame[0] <= 1000);
        stats.frames.push([card.lastFrame, captured, name]);
        if (stats.frames.length > 200000) stats.frames.splice(0, 100000);
        if (!card.overlay.hidden) {
          const delay = captured ? ` · ${Math.max(0, card.lastFrame - captured)} ms` : '';
          card.overlay.textContent = `${card.frames.length} fps${delay}`;
        }
      } catch {} finally {
        drawing = false;
        if (pending && !controller.signal.aborted) void draw();
      }
    };
    try {
      const response = await fetch(url, {cache: 'no-store', signal: controller.signal});
      if (!response.ok || !response.body) throw new Error('视频流不可用');
      const reader = response.body.getReader(), decoder = new TextDecoder();
      let buffer = new Uint8Array(0);
      for (;;) {
        const {value, done} = await reader.read();
        if (done) break;
        buffer = concat(buffer, value);
        for (;;) {
          const end = indexOf(buffer, HEADER_END);
          if (end < 0) break;
          const head = decoder.decode(buffer.subarray(0, end));
          const length = Number(/Content-Length:\s*(\d+)/i.exec(head)?.[1]);
          const start = end + HEADER_END.length;
          if (!Number.isFinite(length)) { buffer = buffer.subarray(start); continue; }
          if (buffer.length < start + length) break;
          pending = {bytes: buffer.slice(start, start + length), captured: Number(/X-Frame-Time:\s*(\d+)/i.exec(head)?.[1]) || 0};
          stats.received++; buffer = buffer.subarray(start + length); void draw();
        }
      }
    } catch (error) { note('stream-end', `${name}: ${controller.signal.aborted ? 'aborted' : error.message}`); }
    if (card.stream?.controller === controller) {
      card.stream = null; card.canvas.hidden = true;
      card.placeholder.hidden = false; card.placeholder.textContent = '画面连接中断，正在重连…';
    }
  }
  function ensureStream(card, name) {
    if (card.stream && Date.now() - card.lastFrame > 6000) stopStream(card, '正在重新连接画面…');
    if (card.stream) return;
    const controller = new AbortController(); card.stream = {controller}; card.lastFrame = Date.now();
    const query = new URLSearchParams({camera: name, mode: video.mode});
    if (video.fps !== null) query.set('fps', video.fps);
    note('stream-start', name); void readStream(card, name, `/api/airsim/stream?${query}`, controller);
  }

  function render() {
    const active = new Set(records.filter(drone => drone.airborne === true).map(drone => drone.name));
    $('fleet-total').textContent = connected ? records.length : '—';
    $('fleet-active').textContent = connected ? active.size : '—';
    $('fleet-idle').textContent = connected ? records.filter(drone => drone.available === true && !active.has(drone.name)).length : '—';
    const connection = $('workspace-connection');
    const state = connected ? 'online' : 'offline';
    if (connection.dataset.state !== state) {
      connection.dataset.state = state;
      connection.textContent = connected ? '仿真已连接' : '仿真未连接';
    }
    for (const [id, value] of [['workspace-airborne', active.size], ['workspace-fleet-total', records.length]]) {
      const output = $(id);
      const text = connected ? String(value) : '—';
      if (output.textContent !== text) output.textContent = text;
    }
    $('fleet-tab-count').textContent = active.size; $('fleet-tab-count').hidden = !connected || !active.size;
    const available = connected ? records : [];
    const shown = available.filter(drone => filter.value === 'all' || active.has(drone.name));
    if (!shown.some(drone => drone.name === preview)) preview = (shown.find(drone => active.has(drone.name)) || shown[0])?.name ?? null;
    if (focused && !available.some(drone => drone.name === focused)) focused = null;
    const views = dialog.open ? available.filter(drone => !focused || drone.name === focused)
      : shown.filter(drone => drone.name === preview);
    updateRoster(shown, active);
    panel.dataset.focused = String(!!focused);
    back.hidden = !focused;
    if (dialog.open) {
      const context = focused || `${views.length} 路画面`;
      if (wallContext.textContent !== context) wallContext.textContent = context;
    }
    container.dataset.count = String(views.length);
    $('fleet-empty').hidden = views.length > 0;
    $('fleet-empty-title').textContent = !connected ? '等待仿真连接' : available.length ? '当前没有飞行中的无人机' : '暂无无人机';
    $('fleet-empty-message').textContent = connected ? '任务起飞后，实时画面会自动出现在这里。' : '请启动仿真，连接后即可查看机队与现场画面。';
    $('fleet-show-all').hidden = !available.length || filter.value === 'all' || dialog.open;
    for (const [name, card] of cards) {
      if (!records.some(drone => drone.name === name)) {
        stopStream(card, ''); card.root.remove(); cards.delete(name);
      }
    }
    for (const [index, drone] of records.entries()) {
      const included = views.some(item => item.name === drone.name);
      let card = cards.get(drone.name);
      if (!card && !included) continue;
      card ||= createCard(drone.name, index); card.root.hidden = !included;
      if (dialog.open && !focused && included && container.children[index] !== card.root) {
        container.insertBefore(card.root, container.children[index] || null);
      }
      const airborne = active.has(drone.name);
      card.badge.textContent = airborne ? '飞行中' : drone.available ? '待命' : '准备中';
      card.badge.dataset.active = String(airborne);
      const located = Array.isArray(drone.position_m) && drone.position_m.every(Number.isFinite);
      card.locate.disabled = !located || !window.liveMap?.isReady();
      if (located) {
        const [x, y, z] = drone.position_m;
        const speed = Number.isFinite(drone.speed_m_s) ? ` · ${drone.speed_m_s.toFixed(1)} m/s` : '';
        card.position.textContent = `X ${x.toFixed(1)} · Y ${y.toFixed(1)} · Z ${z.toFixed(1)} m${speed}`;
      } else card.position.textContent = '正在校准位置…';
      if (included && visible()) ensureStream(card, drone.name);
      else stopStream(card, '等待实时画面');
    }
  }

  function updateRoster(shown, active) {
    const visibleNames = new Set(shown.map(drone => drone.name));
    for (const [name, row] of rosterRows) {
      if (visibleNames.has(name)) continue;
      row.remove();
      rosterRows.delete(name);
    }
    for (const [index, drone] of shown.entries()) {
      let row = rosterRows.get(drone.name);
      if (!row) {
        row = document.createElement('button');
        row.type = 'button';
        row.className = 'dock-list-item';
        const name = document.createElement('strong'); name.textContent = drone.name;
        row.append(name, document.createElement('small'));
        row.addEventListener('click', () => { preview = drone.name; render(); });
        rosterRows.set(drone.name, row);
      }
      const state = active.has(drone.name) ? '飞行中' : drone.available ? '待命' : '准备中';
      if (row.lastElementChild.textContent !== state) row.lastElementChild.textContent = state;
      row.setAttribute('aria-pressed', String(drone.name === preview));
      if (roster.children[index] !== row) roster.insertBefore(row, roster.children[index] || null);
    }
  }
  function disconnected() {
    connected = false; status.dataset.online = 'false'; status.textContent = '仿真未连接';
    window.liveMap?.setDrones([]); stopAll('等待仿真连接'); render();
  }
  async function update() {
    clearTimeout(timer); stateRequest?.abort();
    const started = performance.now();
    const current = ++generation;
    if (location.protocol === 'file:') { status.textContent = '请通过本地 HTTP 服务打开'; return; }
    if (!document.hidden) {
      const controller = new AbortController(); stateRequest = controller;
      const timeout = setTimeout(() => controller.abort(), 4000);
      try {
        const response = await fetch('/api/airsim/state?capture=false', {cache: 'no-store', signal: controller.signal});
        if (!response.ok) throw new Error('读取实时状态失败');
        const data = await response.json();
        if (current !== generation) return;
        records = data.drones.map((drone, index) => ({...drone, color: colors[index % colors.length]}));
        updateFlightDrones(records.map(drone => drone.name));
        connected = data.connected;
        if (connected) {
          status.dataset.online = 'true'; status.textContent = '实时连接'; status.title = data.coordinate_error || '';
          window.liveMap?.setDrones(records, data.session_id); render();
        } else disconnected();
      } catch (error) { if (current === generation) { note('state-error', error.message); disconnected(); } }
      finally { clearTimeout(timeout); if (stateRequest === controller) stateRequest = null; }
    }
    const interval = connected && !document.hidden ? 150 : 700;
    if (current === generation) timer = setTimeout(update, Math.max(0, interval - (performance.now() - started)));
  }
  filter.onchange = () => { focused = null; render(); };
  $('fleet-show-all').onclick = () => { filter.value = 'all'; render(); };
  window.addEventListener('mission:reset', () => { stopAll('正在复位…'); void update(); });
  window.addEventListener('sidebar:change', () => { if (!visible()) stopAll('等待实时画面'); void update(); });
  document.addEventListener('visibilitychange', () => { if (document.hidden) stopAll('等待实时画面'); else void update(); });
  window.addEventListener('pagehide', () => { clearTimeout(timer); stateRequest?.abort(); stopAll(''); });
  void update();
})();
