'use strict';

(() => {
  const $ = id => document.getElementById(id);
  const colors = ['#e6553f', '#237fc1', '#1b9c78', '#925bcc', '#d89120'];
  const container = $('drone-cameras'), panel = $('uav-camera-panel'), status = $('airsim-status');
  const filter = $('camera-filter'), dialog = $('camera-dialog'), expand = $('camera-expand');
  const panelParent = panel.parentNode, panelNext = panel.nextSibling;
  const cards = new Map();
  let records = [], connected = false, focused = null;
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
  function openMonitor(name = null) {
    focused = name;
    if (!dialog.open) {
      dialog.append(panel); dialog.showModal();
      expand.textContent = '收起'; expand.setAttribute('aria-expanded', 'true');
    }
    render();
  }
  expand.onclick = () => { if (dialog.open) dialog.close(); else openMonitor(); };
  $('camera-back').onclick = () => { focused = null; render(); };
  dialog.addEventListener('close', () => {
    panelParent.insertBefore(panel, panelNext); focused = null;
    expand.textContent = '展开监控墙'; expand.setAttribute('aria-expanded', 'false');
    render();
  });

  function createCard(name, index) {
    const root = document.createElement('article'); root.className = 'drone-card'; root.dataset.drone = name;
    root.style.setProperty('--drone-color', colors[index % colors.length]);
    const heading = document.createElement('div'); heading.className = 'drone-heading';
    const title = document.createElement('strong'); title.textContent = name;
    const badge = document.createElement('span'); badge.className = 'drone-state';
    const locate = document.createElement('button'); locate.type = 'button'; locate.textContent = '定位';
    locate.setAttribute('aria-label', `定位 ${name}`); locate.onclick = () => window.liveMap?.centerDrone(name);
    heading.append(title, badge, locate);
    const view = document.createElement('div'); view.className = 'camera-view';
    const canvas = document.createElement('canvas'); canvas.hidden = true;
    canvas.setAttribute('role', 'img'); canvas.setAttribute('aria-label', `${name} 第三人称实时画面`);
    const placeholder = document.createElement('div'); placeholder.className = 'camera-placeholder';
    placeholder.textContent = '正在连接画面…';
    const overlay = document.createElement('div'); overlay.className = 'camera-stats'; overlay.hidden = params.get('stats') !== '1';
    const zoom = document.createElement('button'); zoom.type = 'button'; zoom.className = 'drone-zoom';
    zoom.textContent = '放大'; zoom.setAttribute('aria-label', `放大 ${name} 画面`); zoom.onclick = () => openMonitor(name);
    const position = document.createElement('p'); position.className = 'drone-position';
    view.append(canvas, placeholder, overlay, zoom); root.append(heading, view, position); container.append(root);
    const card = {root, badge, locate, canvas, context: canvas.getContext('2d'), placeholder, overlay, position,
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
    const active = records.filter(drone => drone.airborne === true);
    $('fleet-total').textContent = connected ? records.length : '—';
    $('fleet-active').textContent = connected ? active.length : '—';
    $('fleet-idle').textContent = connected ? records.filter(drone => drone.available === true).length : '—';
    $('fleet-tab-count').textContent = active.length; $('fleet-tab-count').hidden = !connected || !active.length;
    const shown = connected ? records.filter(drone => filter.value === 'all' || drone.airborne === true) : [];
    if (focused && !shown.some(drone => drone.name === focused)) focused = null;
    $('camera-back').hidden = !focused;
    $('fleet-visible-count').textContent = focused ? 1 : shown.length;
    container.dataset.count = String(focused ? 1 : shown.length);
    $('fleet-empty').hidden = shown.length > 0;
    $('fleet-empty-title').textContent = connected ? '当前没有飞行中的无人机' : '等待仿真连接';
    $('fleet-empty-message').textContent = connected ? '任务起飞后，实时画面会自动出现在这里。' : '请启动仿真，连接后即可查看机队与现场画面。';
    $('fleet-show-all').hidden = !connected || filter.value === 'all';
    for (const [name, card] of cards) {
      if (!records.some(drone => drone.name === name)) {
        stopStream(card, ''); card.root.remove(); cards.delete(name);
      }
    }
    for (const [index, drone] of records.entries()) {
      const included = shown.some(item => item.name === drone.name) && (!focused || focused === drone.name);
      let card = cards.get(drone.name);
      if (!card && !included) continue;
      card ||= createCard(drone.name, index); card.root.hidden = !included;
      card.badge.textContent = drone.airborne ? '飞行中' : drone.available ? '待命' : '准备中';
      card.badge.dataset.active = String(!!drone.airborne);
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
  function disconnected() {
    connected = false; status.dataset.online = 'false'; status.textContent = '仿真未连接';
    window.liveMap?.setDrones([]); stopAll('等待仿真连接'); render();
  }
  async function update() {
    clearTimeout(timer); stateRequest?.abort();
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
        connected = data.connected;
        if (connected) {
          status.dataset.online = 'true'; status.textContent = '实时连接'; status.title = data.coordinate_error || '';
          window.liveMap?.setDrones(records); render();
        } else disconnected();
      } catch (error) { if (current === generation) { note('state-error', error.message); disconnected(); } }
      finally { clearTimeout(timeout); if (stateRequest === controller) stateRequest = null; }
    }
    if (current === generation) timer = setTimeout(update, 350);
  }
  filter.onchange = () => { focused = null; render(); };
  $('fleet-show-all').onclick = () => { filter.value = 'all'; render(); };
  window.addEventListener('mission:reset', () => { window.liveMap?.clearTrails(); stopAll('正在复位…'); void update(); });
  window.addEventListener('sidebar:change', () => { if (!visible()) stopAll('等待实时画面'); void update(); });
  document.addEventListener('visibilitychange', () => { if (document.hidden) stopAll('等待实时画面'); else void update(); });
  window.addEventListener('pagehide', () => { clearTimeout(timer); stateRequest?.abort(); stopAll(''); });
  void update();
})();
