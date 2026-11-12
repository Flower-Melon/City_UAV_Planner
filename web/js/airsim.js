'use strict';

(() => {
  const colors = ['#e6553f', '#237fc1', '#1b9c78', '#925bcc', '#d89120'];
  const container = document.getElementById('drone-cameras');
  const selector = document.getElementById('camera-drone');
  const status = document.getElementById('airsim-status');
  const panel = document.getElementById('uav-camera-panel');
  const expand = document.getElementById('camera-expand');
  const dialog = document.getElementById('camera-dialog');
  const panelParent = panel.parentNode, panelNext = panel.nextSibling;
  expand.onclick = () => {
    if (dialog.open) { dialog.close(); return; }
    // 移动原面板，保留实时画布、视频流和无人机选择状态。
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
  let selected = selector.value;
  let stateRequest = null, timer, generation = 0;
  let stream = null;
  const params = new URLSearchParams(location.search);
  // 画面由 CityNPC 在 UE 内直接输出；地址可加 fps=（默认 30，0 为每帧）、stats=1（显示帧率与延迟）。
  const video = {mode: params.get('mode') || 'capture', fps: params.get('fps')};
  const stats = window.__videoStats = {frames: [], received: 0, events: []};
  const note = (event, detail = '') => { stats.events.push([Date.now(), event, String(detail)]); if (stats.events.length > 2000) stats.events.shift(); };
  const HEADER_END = new Uint8Array([13, 10, 13, 10]);

  const card = document.createElement('article'); card.className = 'drone-card';
  const heading = document.createElement('div'); heading.className = 'drone-heading';
  const title = document.createElement('strong'); title.textContent = selected;
  const locate = document.createElement('button'); locate.type = 'button'; locate.textContent = '定位'; locate.disabled = true;
  locate.onclick = () => window.liveMap?.centerDrone(selected);
  heading.append(title, locate);
  const view = document.createElement('div'); view.className = 'camera-view';
  const canvas = document.createElement('canvas'); canvas.hidden = true; canvas.setAttribute('role', 'img');
  const context = canvas.getContext('2d');
  const placeholder = document.createElement('div'); placeholder.className = 'camera-placeholder';
  const overlay = document.createElement('div'); overlay.className = 'camera-stats'; overlay.hidden = params.get('stats') !== '1';
  const position = document.createElement('p'); position.className = 'drone-position';
  view.append(canvas, placeholder, overlay); card.append(heading, view, position); container.append(card);

  function clearImage(message) {
    stream?.controller.abort(); stream = null;
    canvas.hidden = true; placeholder.hidden = false; placeholder.textContent = message;
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

  function record(captured) {
    stats.frames.push([Date.now(), captured]);
    if (stats.frames.length > 200000) stats.frames.splice(0, 100000);
  }

  // 只画最新到达的一帧：解码跟不上时丢弃中间帧，不积压延迟。
  async function readStream(url, controller) {
    let pending = null, drawing = false;
    const draw = async () => {
      if (drawing || !pending) return;
      drawing = true;
      const {bytes, captured} = pending; pending = null;
      try {
        const bitmap = await createImageBitmap(new Blob([bytes], {type: 'image/jpeg'}));
        if (controller.signal.aborted) { bitmap.close(); return; }
        if (canvas.width !== bitmap.width || canvas.height !== bitmap.height) {
          canvas.width = bitmap.width; canvas.height = bitmap.height;
        }
        context.drawImage(bitmap, 0, 0); bitmap.close();
        canvas.hidden = false; placeholder.hidden = true;
        record(captured);
      } catch {} finally {
        drawing = false;
        if (pending && !controller.signal.aborted) void draw();
      }
    };
    try {
      const response = await fetch(url, {cache: 'no-store', signal: controller.signal});
      if (!response.ok || !response.body) throw new Error('视频流不可用');
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
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
          stats.received++;
          buffer = buffer.subarray(start + length);
          void draw();
        }
      }
      note('stream-end', 'done');
    } catch (error) { note('stream-end', controller.signal.aborted ? 'aborted' : error.message); }
    // 连接断开后由下一次状态刷新重新建立。
    if (stream?.controller === controller) stream = null;
  }

  function ensureStream(name) {
    if (stream?.camera === name) return;
    stream?.controller.abort();
    const controller = new AbortController();
    stream = {camera: name, controller};
    const query = new URLSearchParams({camera: name, mode: video.mode});
    if (video.fps !== null) query.set('fps', video.fps);
    note('stream-start', name);
    void readStream(`/api/airsim/stream?${query}`, controller);
  }

  setInterval(() => {
    if (overlay.hidden) return;
    const now = Date.now(), recent = stats.frames.filter(frame => now - frame[0] <= 1000);
    const delays = recent.filter(frame => frame[1]).map(frame => frame[0] - frame[1]).sort((a, b) => a - b);
    overlay.textContent = `${recent.length} fps` + (delays.length ? ` · 延迟 ${delays[delays.length >> 1]} ms` : '');
  }, 500);

  function resetSelection() {
    title.textContent = selected; canvas.setAttribute('aria-label', `${selected} 第三人称实时画面`);
    card.style.setProperty('--drone-color', colors[Math.max(0, selector.selectedIndex) % colors.length]);
    position.textContent = '等待实时位置'; locate.disabled = true;
    clearImage('等待摄像头画面');
  }

  function disconnected() {
    status.dataset.online = 'false'; status.textContent = '未连接 AirSim，请确认仿真处于播放模式';
    window.liveMap?.setDrones([]);
    locate.disabled = true; position.textContent = '等待实时位置'; clearImage('等待 AirSim 连接');
  }

  function setOptions(drones) {
    const names = drones.map(drone => drone.name);
    if (!names.length) return;
    if (JSON.stringify([...selector.options].map(option => option.value)) !== JSON.stringify(names)) {
      selector.replaceChildren(...names.map(name => new Option(name, name)));
    }
    if (!names.includes(selected)) { selected = names[0]; resetSelection(); }
    selector.value = selected;
  }

  async function update() {
    clearTimeout(timer); stateRequest?.abort();
    const current = ++generation;
    if (location.protocol === 'file:') { status.textContent = '请通过本地 HTTP 服务打开'; return; }
    if (!document.hidden) {
      const controller = new AbortController(); stateRequest = controller;
      const timeout = setTimeout(() => controller.abort(), 4000);
      try {
        const capture = !document.getElementById('uav-camera-panel').hidden;
        // 画面来自 UE 推送，Python 端只读状态不取图。
        const response = await fetch(`/api/airsim/state?camera=${encodeURIComponent(selected)}&capture=false`, {cache: 'no-store', signal: controller.signal});
        if (!response.ok) throw new Error('读取实时状态失败');
        const data = await response.json();
        if (current !== generation) return;
        setOptions(data.drones);
        const records = data.drones.map((drone, index) => ({...drone, color: colors[index % colors.length]}));
        const drone = records.find(record => record.name === selected);
        card.style.setProperty('--drone-color', drone?.color || colors[0]);
        if (Array.isArray(drone?.position_m) && drone.position_m.every(Number.isFinite)) {
          const [x, y, z] = drone.position_m;
          position.textContent = `UE X ${x.toFixed(1)} · Y ${y.toFixed(1)} · Z ${z.toFixed(1)} m`;
          locate.disabled = !window.liveMap?.isReady();
        } else {
          position.textContent = data.connected ? '正在校准世界坐标…' : '等待实时位置'; locate.disabled = true;
        }
        if (data.connected) {
          const located = records.filter(record => record.position_m).length;
          status.dataset.online = 'true';
          status.textContent = located ? `已连接 · ${located} 架无人机实时位置` : data.coordinate_error ? '已连接 · 世界坐标校准失败' : '已连接 · 正在校准世界坐标';
          status.title = data.coordinate_error || '';
          window.liveMap?.setDrones(records);
          if (capture) ensureStream(selected);
          else clearImage('正在等待摄像头画面…');
        } else { note('state-offline'); disconnected(); }
      } catch (error) { if (current === generation) { note('state-error', error.message); disconnected(); } }
      finally {
        clearTimeout(timeout);
        if (stateRequest === controller) stateRequest = null;
      }
    }
    if (current === generation) timer = setTimeout(update, 100);
  }

  window.addEventListener('mission:reset', () => {
    window.liveMap?.clearTrails();
    // update 会中止旧请求并递增 generation，防止复位前的响应重新画出旧轨迹。
    void update();
  });
  selector.onchange = () => { selected = selector.value; resetSelection(); void update(); };
  window.addEventListener('sidebar:change', () => { clearImage('等待摄像头画面'); void update(); });
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) clearImage('等待摄像头画面');
    else void update();
  });
  window.addEventListener('pagehide', () => { clearTimeout(timer); stateRequest?.abort(); clearImage(''); });
  resetSelection(); void update();
})();
