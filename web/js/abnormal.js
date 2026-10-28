'use strict';

(() => {
  const $ = id => document.getElementById(id);
  const names = {vehicle: '异常车辆', armed_person: '持刀人员', patient: '病人'};
  const modes = {mixed: '混合', cruise: '正常巡航', surge: '突然加速', weave: '蛇形行驶', countersteer: '反打方向', brake: '急刹减速', wrong_way: '逆向行驶', stop: '停止'};
  const actions = ['原地停留', '步行', '奔跑', '持刀威胁', '痛苦', '倒地中', '躺卧'];
  const storageKey = 'city-abnormal-pending';
  let targets = [], selected = null, picking = false, busy = false, online = false;
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
    $('abnormal-pick').disabled = busy || !online || !window.liveMap?.isReady();
    $('abnormal-kind').disabled = busy || picking;
    $('abnormal-pick').textContent = picking ? '请点击地图…' : busy ? '操作进行中…' : '地图选点生成';
    $('abnormal-cancel').hidden = !picking;
    $('abnormal-apply').disabled = busy || !online;
    for (const button of document.querySelectorAll('.abnormal-remove')) button.disabled = busy || !online;
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
    if (id !== selected) { selected = id; clearFrame(); }
    render();
    if (visible() && selected) void frame();
  }
  function render() {
    window.liveMap?.setAbnormal(targets, selected);
    $('abnormal-count').textContent = targets.length;
    $('abnormal-empty').hidden = targets.length > 0;
    const key = JSON.stringify([targets.map(t => [t.id, t.kind]), selected]);
    if (key !== listKey) {
      listKey = key;
      $('abnormal-list').replaceChildren(...targets.map(target => {
        const row = document.createElement('div'); row.className = 'abnormal-target-row';
        const button = document.createElement('button'); button.type = 'button';
        button.className = 'abnormal-target'; button.dataset.kind = target.kind;
        button.setAttribute('aria-pressed', String(target.id === selected));
        const label = document.createElement('strong'), id = document.createElement('small');
        label.textContent = names[target.kind]; id.textContent = target.id;
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
      $('abnormal-title').textContent = `${names[target.kind]} · ${target.id.split('_').pop()}`;
      $('abnormal-position').textContent = `UE X ${target.position_m[0].toFixed(1)} · Y ${target.position_m[1].toFixed(1)} · Z ${target.position_m[2].toFixed(1)} m`;
      $('abnormal-action').textContent = target.error ? `运行异常：${target.error}` : target.kind === 'vehicle'
        ? `${modes[target.behavior] || target.behavior} · 当前 ${modes[target.active_behavior] || target.active_behavior} · ${(target.speed_m_s * 3.6).toFixed(1)} km/h`
        : `当前状态：${actions[target.action] || '等待更新'}`;
      $('abnormal-action').dataset.error = String(!!target.error);
      $('abnormal-vehicle-controls').hidden = target.kind !== 'vehicle';
      if ($('abnormal-behavior').dataset.target !== selected) {
        $('abnormal-behavior').value = target.behavior || 'mixed';
        $('abnormal-behavior').dataset.target = selected;
      }
    }
    controls();
  }
  function savePending(value) {
    pending = value;
    try { if (value) sessionStorage.setItem(storageKey, JSON.stringify(value)); else sessionStorage.removeItem(storageKey); } catch { /* 存储可选。 */ }
  }
  async function runJob(route, data) {
    if (busy) return;
    cancelPick(); busy = true; controls();
    const operation = {route, data: {request_id: crypto.randomUUID(), ...data}};
    savePending(operation);
    message(route === 'spawn' ? `正在生成${names[data.kind]}，请等待仿真返回…`
      : route === 'remove' ? `正在移除 ${data.target_id}…` : '正在设置车辆行为…');
    try {
      await json(`/api/abnormal/${route}`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(operation.data)});
    } catch (error) { message(`提交结果尚未确认：${error.message}；正在查询操作记录。`, true); }
    void pollJob();
  }
  async function pollJob() {
    if (!pending) return;
    busy = true; controls();
    try {
      const job = await json(`/api/abnormal/jobs/${encodeURIComponent(pending.data.request_id)}`);
      if (job.status === 'running') { setTimeout(pollJob, 800); return; }
      if (job.status === 'completed') {
        if (pending.route === 'remove') {
          ++stateGeneration;
          targets = targets.filter(target => target.id !== job.result.target_id);
          if (selected === job.result.target_id) {
            selected = targets[0]?.id || null;
            clearFrame(selected ? '正在等待实时画面…' : '选择一个已生成目标以查看画面');
          }
          render();
          message(`已移除：${job.result.target_id}`);
        } else {
          selected = job.result.target_id; clearFrame();
          message(job.result.warning || `操作完成：${selected}`, !!job.result.warning);
        }
      } else message(`操作失败：${job.error}`, true);
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
        if (!targets.some(t => t.id === selected)) {
          const changed = selected !== null;
          selected = targets[0]?.id || null;
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
    timer = setTimeout(poll, 700);
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
  $('abnormal-pick').onclick = () => {
    if (busy || !online || !window.liveMap?.isReady()) return;
    picking = true; controls(); message(`请点击地图指定${names[$('abnormal-kind').value]}出现的位置；按 Esc 取消。`);
    window.liveMap.setPickHandler(position => {
      window.liveMap.setPendingPoint(position);
      void runJob('spawn', {kind: $('abnormal-kind').value, position, behavior: 'mixed'});
    });
  };
  $('abnormal-cancel').onclick = () => { cancelPick(); message('已取消地图选点。'); };
  $('abnormal-locate').onclick = () => window.liveMap?.centerTarget(selected);
  $('abnormal-apply').onclick = () => void runJob('behavior', {target_id: selected, behavior: $('abnormal-behavior').value});
  window.addEventListener('keydown', event => { if (event.key === 'Escape' && picking) $('abnormal-cancel').click(); });
  window.addEventListener('abnormal:select', event => { $('abnormal-tab').click(); select(event.detail.id); });
  window.addEventListener('sidebar:change', () => { cancelPick(); clearFrame(selected ? '正在等待实时画面…' : '选择一个已生成目标以查看画面'); });
  document.addEventListener('visibilitychange', () => { if (document.hidden) { cancelPick(); clearFrame(); } });
  window.addEventListener('pagehide', () => { clearTimeout(timer); clearFrame(''); window.liveMap?.setPickHandler(null); });
  if (pending) { message('正在恢复上一次操作状态…'); void pollJob(); }
  void poll();
})();
