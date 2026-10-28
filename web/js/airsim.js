'use strict';

(() => {
  const colors = ['#e6553f', '#237fc1', '#1b9c78', '#925bcc', '#d89120'];
  const container = document.getElementById('drone-cameras');
  const selector = document.getElementById('camera-drone');
  const status = document.getElementById('airsim-status');
  const clearTrails = document.getElementById('clear-trails');
  const trailStatus = document.getElementById('trail-status');
  let clearingTrails = false;
  let selected = selector.value;
  let frameRequest = null, stateRequest = null, timer, generation = 0;
  let stamp = 0, imageUrl = null;

  const card = document.createElement('article'); card.className = 'drone-card';
  const heading = document.createElement('div'); heading.className = 'drone-heading';
  const title = document.createElement('strong'); title.textContent = selected;
  const locate = document.createElement('button'); locate.type = 'button'; locate.textContent = '定位'; locate.disabled = true;
  locate.onclick = () => window.liveMap?.centerDrone(selected);
  heading.append(title, locate);
  const view = document.createElement('div'); view.className = 'camera-view';
  const image = document.createElement('img'); image.hidden = true;
  const placeholder = document.createElement('div'); placeholder.className = 'camera-placeholder';
  const position = document.createElement('p'); position.className = 'drone-position';
  view.append(image, placeholder); card.append(heading, view, position); container.append(card);

  function clearImage(message) {
    frameRequest?.abort(); frameRequest = null; stamp = 0;
    image.hidden = true; image.removeAttribute('src');
    if (imageUrl) URL.revokeObjectURL(imageUrl);
    imageUrl = null; placeholder.hidden = false; placeholder.textContent = message;
  }

  function resetSelection() {
    title.textContent = selected; image.alt = `${selected} 第三人称实时画面`;
    card.style.setProperty('--drone-color', colors[Math.max(0, selector.selectedIndex) % colors.length]);
    position.textContent = '等待实时位置'; locate.disabled = true;
    clearImage('等待摄像头画面');
  }

  async function frameFor(name, updatedAt) {
    if (frameRequest || updatedAt === stamp) return;
    const controller = new AbortController(); frameRequest = controller;
    const timeout = setTimeout(() => controller.abort(), 4000);
    try {
      const response = await fetch(`/api/airsim/camera/${encodeURIComponent(name)}?t=${updatedAt}`, {cache: 'no-store', signal: controller.signal});
      if (!response.ok) throw new Error('画面暂不可用');
      const blob = await response.blob();
      if (controller.signal.aborted || name !== selected || document.hidden || document.getElementById('uav-camera-panel').hidden) return;
      const previous = imageUrl; imageUrl = URL.createObjectURL(blob); image.src = imageUrl;
      image.hidden = false; placeholder.hidden = true; stamp = updatedAt;
      if (previous) URL.revokeObjectURL(previous);
    } catch {
      if (!controller.signal.aborted && !imageUrl) placeholder.textContent = '正在等待画面…';
    } finally {
      clearTimeout(timeout);
      if (frameRequest === controller) frameRequest = null;
    }
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
    if (clearingTrails) return;
    if (location.protocol === 'file:') { status.textContent = '请通过本地 HTTP 服务打开'; return; }
    if (!document.hidden) {
      const controller = new AbortController(); stateRequest = controller;
      const timeout = setTimeout(() => controller.abort(), 4000);
      try {
        const capture = !document.getElementById('uav-camera-panel').hidden;
        const response = await fetch(`/api/airsim/state?camera=${encodeURIComponent(selected)}&capture=${capture}`, {cache: 'no-store', signal: controller.signal});
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
          if (capture && drone?.camera_updated_at) void frameFor(selected, drone.camera_updated_at);
          else clearImage('正在等待摄像头画面…');
        } else disconnected();
      } catch { if (current === generation) disconnected(); }
      finally {
        clearTimeout(timeout);
        if (stateRequest === controller) stateRequest = null;
      }
    }
    if (current === generation) timer = setTimeout(update, 100);
  }

  clearTrails.disabled = location.protocol === 'file:';
  clearTrails.onclick = async () => {
    if (clearingTrails) return;
    clearingTrails = true; clearTrails.disabled = true; clearTrails.textContent = '清除中…';
    clearTimeout(timer); stateRequest?.abort(); ++generation;
    trailStatus.hidden = false; trailStatus.textContent = '正在清除所有无人机轨迹…';
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 5000);
    try {
      const response = await fetch('/api/airsim/trails/clear', {method: 'POST', signal: controller.signal});
      if (!response.ok) throw new Error('清除失败，请确认本地服务已更新并重试');
      window.liveMap?.clearTrails();
      trailStatus.textContent = '所有无人机轨迹已清除，继续记录新轨迹。';
    } catch (error) {
      trailStatus.textContent = error.name === 'AbortError' ? '清除请求超时，请重试。' : error.message;
    } finally {
      clearTimeout(timeout); clearingTrails = false;
      clearTrails.disabled = false; clearTrails.textContent = '清除轨迹';
      void update();
    }
  };

  selector.onchange = () => { selected = selector.value; resetSelection(); void update(); };
  window.addEventListener('sidebar:change', () => { clearImage('等待摄像头画面'); void update(); });
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) frameRequest?.abort();
    else void update();
  });
  window.addEventListener('pagehide', () => { clearTimeout(timer); stateRequest?.abort(); clearImage(''); });
  resetSelection(); void update();
})();
