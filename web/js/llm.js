'use strict';

(() => {
  const $ = id => document.getElementById(id);
  const tabs = [...document.querySelectorAll('.sidebar-tabs [role="tab"]')];
  const form = $('llm-form'), input = $('llm-instruction'), submit = $('llm-submit');
  const followupForm = $('llm-followup-form'), followupInput = $('llm-followup-input');
  const followupSubmit = $('llm-followup-submit'), newTask = $('llm-new-task');
  const status = $('llm-status'), resultPanel = $('llm-result');
  const modelSelect = $('llm-model-select'), modelInfo = $('llm-model-info');
  const modelRetry = $('llm-model-retry');
  let modelBackend = null, modelPending = false;
  const flightButtons = [...document.querySelectorAll('[data-flight-setting]')];
  const flightDialog = $('llm-flight-dialog'), flightValue = $('llm-flight-value');
  const flightFields = {
    max_height: {label: '最大飞行高度', unit: '米', min: 15, valueId: 'llm-max-height-value',
      help: '去程和返航相对沿途地面的最大高度，至少 15 米。'},
    flight_speed: {label: '飞行速度', unit: '米/秒', min: 0, valueId: 'llm-flight-speed-value',
      help: '巡航速度上限，必须大于 0；转弯和短路段会自动降速。'},
    flight_height: {label: '巡查高度', unit: '米', min: 0, valueId: 'llm-flight-height-value',
      help: '相对起飞地面的固定巡查高度（flight_height），必须大于 0。'}
  };
  const flightSettings = {max_height: 60, flight_speed: 30, flight_height: 15};
  let editingFlight = null;
  let pending = false, conversation = [], previousTurn = null;
  let needsClarification = false;
  let checking = true, flying = false, mission = null, pollTimer;
  let resetting = false, resetFailed = false, generation = 0, planningController = null;
  let visionMission = null;
  const shownAnomalies = new Map();
  let trafficMission = null;
  function showFlight(settings) {
    for (const [key, field] of Object.entries(flightFields)) {
      if (typeof settings[key] !== 'number' || !Number.isFinite(settings[key])) continue;
      flightSettings[key] = settings[key];
      $(field.valueId).textContent = String(settings[key]);
    }
  }
  for (const button of flightButtons) button.onclick = () => {
    if (button.disabled) return;
    editingFlight = button.dataset.flightSetting;
    const field = flightFields[editingFlight];
    $('llm-flight-title').textContent = `设置${field.label}`;
    $('llm-flight-label').textContent = `${field.label}（${field.unit}）`;
    $('llm-flight-help').textContent = field.help;
    flightValue.min = String(field.min);
    flightValue.value = String(flightSettings[editingFlight]);
    flightValue.setCustomValidity('');
    flightDialog.showModal();
    flightValue.focus(); flightValue.select();
  };
  flightValue.oninput = () => flightValue.setCustomValidity('');
  $('llm-flight-form').onsubmit = event => {
    event.preventDefault();
    if (!editingFlight || flightButtons.some(button => button.disabled)) return;
    const value = flightValue.valueAsNumber, field = flightFields[editingFlight];
    if (!Number.isFinite(value) || value <= 0 || value < field.min) {
      flightValue.setCustomValidity(field.min ? `${field.label}不得低于 ${field.min} ${field.unit}。` : '请输入大于 0 的有限数值。');
      flightValue.reportValidity(); return;
    }
    showFlight({[editingFlight]: value});
    flightDialog.close();
  };
  $('llm-flight-cancel').onclick = () => flightDialog.close();

  function annotatedImage(record) {
    const view = document.createElement('div'); view.className = 'llm-anomaly-image';
    const image = document.createElement('img');
    image.src = record.image_url; image.alt = record.description;
    view.append(image);
    for (const detection of record.detections) {
      const [x1, y1, x2, y2] = detection.bbox;
      const box = document.createElement('div'); box.className = 'llm-anomaly-box';
      Object.assign(box.style, {left: `${x1 / 10}%`, top: `${y1 / 10}%`,
        width: `${(x2 - x1) / 10}%`, height: `${(y2 - y1) / 10}%`});
      const label = document.createElement('span'); label.textContent = detection.label;
      box.title = detection.label; box.append(label); view.append(box);
    }
    return view;
  }

  function showVision(execution) {
    const vision = execution?.vision;
    $('llm-vision').hidden = !vision;
    if (!vision) return;
    if (visionMission !== execution.mission_id) {
      visionMission = execution.mission_id; shownAnomalies.clear();
      $('llm-anomalies').replaceChildren();
    }
    $('llm-anomaly-count').textContent = vision.unique_targets ?? vision.abnormal;
    const error = vision.errors?.[0]?.error;
    $('llm-vision-error').hidden = !error;
    $('llm-vision-error').textContent = error ? `部分目标识别或定位未完成：${error}` : '';
    const targets = vision.targets ?? vision.anomalies.map(record =>
      ({target_id: `${record.task_id}:${record.image}`, first_observation: record, sightings: 1}));
    for (const target of targets) {
      const record = target.first_observation;
      const key = target.target_id;
      const signature = JSON.stringify([record.task_id, record.image, record.detections,
        target.sightings, target.location_error]);
      let entry = shownAnomalies.get(key);
      if (!entry) {
        const card = document.createElement('article'); card.className = 'llm-anomaly-card';
        entry = {card, signature: null}; shownAnomalies.set(key, entry);
      }
      const card = entry.card;
      // 按任务顺序和照片编号排序；更新已登记目标的卡片。
      $('llm-anomalies').append(card);
      if (entry.signature === signature) continue;
      entry.signature = signature;
      const heading = document.createElement('p'); heading.className = 'llm-anomaly-heading';
      heading.textContent = `${target.person_id || key} · ${record.drone_id} · 首次图像 ${record.image} · ${record.zone_id}`;
      const button = document.createElement('button'); button.type = 'button';
      button.className = 'llm-anomaly-open'; button.setAttribute('aria-label', `放大异常照片 ${record.image}`);
      button.append(annotatedImage(record));
      const categories = [...new Set(record.detections.map(detection =>
        ({patient: '病人', armed_person: '持刀人员'})[detection.status]))].join('、');
      const description = document.createElement('p');
      description.textContent = `${categories}：${record.description}` +
        (target.sightings > 1 ? `（已合并 ${target.sightings} 帧识别）` : '') +
        (target.location_error ? `；${target.location_error}` : '');
      button.onclick = () => {
        $('llm-image-detail').replaceChildren(annotatedImage(record));
        const caption = document.createElement('p'); caption.textContent = description.textContent;
        $('llm-image-detail').append(caption); $('llm-image-dialog').showModal();
      };
      card.replaceChildren(heading, button, description);
    }
  }
  $('llm-image-close').onclick = () => $('llm-image-dialog').close();

  function showTraffic(execution) {
    const traffic = execution?.traffic;
    $('llm-traffic').hidden = !traffic;
    if (!traffic) { window.liveMap?.setTraffic([]); return; }
    if (trafficMission !== execution.mission_id) {
      trafficMission = execution.mission_id;
      window.trafficView.clear();
    }
    const count = (traffic.targets || []).filter(t => t.status === 'confirmed').length;
    $('llm-speeding-count').textContent = count;
    $('llm-speeding-count').hidden = !count;
    const waiting = !traffic.active && execution.tasks.some(t => ['queued', 'flying'].includes(t.monitor.status));
    const starting = execution.tasks.some(t => t.monitor.traffic.status === 'starting');
    $('llm-traffic-status').textContent = waiting ? '等待就位' : starting ? '正在启动' : traffic.active ? '监测中' : '已结束';
    window.trafficView.render(execution);
    const error = traffic.errors[0]?.error || execution.tasks.find(t => t.monitor.traffic.fusion?.error)?.monitor.traffic.fusion.error;
    $('llm-traffic-error').hidden = !error;
    $('llm-traffic-error').textContent = error ? '部分监控暂不可用' : '';
  }

  // 根据规划与飞行状态锁定输入，防止刷新或重复点击再次派发。
  function controls() {
    for (const element of [submit, input, followupSubmit, followupInput, ...flightButtons])
      element.disabled = pending || checking || flying || resetting || resetFailed || modelPending || !modelBackend;
    const modelLocked = modelPending || pending || checking || flying || resetting || resetFailed;
    modelSelect.disabled = modelLocked || !modelBackend;
    modelSelect.setAttribute('aria-busy', String(modelPending));
    modelRetry.disabled = modelLocked;
    if (flightDialog.open && flightButtons.some(button => button.disabled)) flightDialog.close();
    newTask.hidden = false;
    newTask.disabled = resetting || modelPending;
    newTask.textContent = resetting ? '正在复位…' : '新任务';
    followupForm.hidden = !needsClarification || flying || resetting || resetFailed;
  }

  // 读取任务使用短请求；超时只停止等待，不终止无人机控制进程。
  async function missionRequest(url, options = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 20000);
    try {
      const response = await fetch(url, {...options, cache: 'no-store', signal: controller.signal});
      const data = await response.json();
      if (!response.ok) throw Object.assign(Error(data.error || '无法读取执行进度。'), {statusCode: response.status});
      return data;
    } finally { clearTimeout(timer); }
  }

  function showBackend(data) {
    modelBackend = data.backend;
    modelInfo.textContent = data.model +
      (data.vision_model !== data.model ? `；视觉：${data.vision_model}` : '');
    modelInfo.dataset.error = 'false';
  }

  async function updateBackend(backend) {
    modelPending = true; controls();
    modelInfo.textContent = backend ? '正在切换模型…' : '正在读取模型…';
    modelInfo.dataset.error = 'false';
    modelRetry.hidden = true;
    try {
      showBackend(await missionRequest('/api/llm/backend', backend ? {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({backend})
      } : {}));
    } catch (error) {
      // 切换可能已写入但响应丢失，重新读取实际选择后再允许提交任务。
      if (backend) {
        try { showBackend(await missionRequest('/api/llm/backend')); }
        catch { modelBackend = null; }
      }
      const current = modelBackend ? `当前${modelBackend === 'ollama' ? '本地' : '云端'}模型；` : '';
      modelInfo.textContent = current + (error.statusCode === 404 ? '请重启网页服务并刷新，以加载模型切换功能。' : error.message);
      modelInfo.dataset.error = 'true';
    } finally {
      modelPending = false;
      modelSelect.value = modelBackend || '';
      modelRetry.hidden = modelInfo.dataset.error !== 'true';
      controls();
    }
  }
  modelSelect.onchange = () => {
    if (modelSelect.disabled || modelSelect.value === modelBackend) return;
    void updateBackend(modelSelect.value);
  };
  modelRetry.onclick = () => { if (!modelRetry.disabled) void updateBackend(); };

  // 展示真实巡查任务状态。
  function showMission(data) {
    const wasResetting = resetting;
    resetting = !!data.resetting;
    resetFailed = !!data.reset_error;
    if (resetting) {
      setStatus('busy', '正在终止后台任务并复位全部无人机，请稍候…'); controls(); return;
    }
    if (resetFailed) {
      flying = false;
      setStatus('error', `复位失败：${data.reset_error} 请点击「新任务」重试。`); controls(); return;
    }
    if (wasResetting && data.reset_completed && !data.execution) {
      clearTask();
      window.dispatchEvent(new Event('mission:reset'));
      setStatus('idle', '后台任务已终止，无人机已复位，轨迹已清除，可以输入新任务。'); controls(); return;
    }
    mission = data.execution;
    if (mission?.settings) showFlight(mission.settings);
    showVision(mission);
    showTraffic(mission);
    flying = !!data.starting || !!data.uncertain || !!mission?.traffic?.active ||
      !!mission && !['completed', 'failed'].includes(mission.status);
    if (mission || flying) needsClarification = false;
    if (mission) {
      $('llm-queue').hidden = false;
      const monitorLabels = {queued: '等待分配', flying: '飞往区域中心', hovering: '高空悬停',
        relocated: '已转往后续区域', failed: '失败', skipped: '未执行'};
      $('llm-queue').textContent = `共 ${mission.tasks.length} 个区域 · 自动选机：\n` +
        mission.tasks.map(task => `${task.sequence}. ${task.target.zone_id} → 巡查：${task.drone_id || '等待自动分配'}` +
          (task.monitor ? `；监控：${task.monitor.drone_id || '等待自动分配'}（${monitorLabels[task.monitor.status] || task.monitor.status}）` : '')).join('\n');
      const rescueLabels = {locating: '定位地面落点', dispatching: '正在派机', delivering: '执行药品投递',
        delivered: '投递完成，保持悬停', failed: '失败', uncertain: '执行状态未确认', stopped: '已停止'};
      const rescues = mission.rescues || [];
      if (rescues.length) $('llm-queue').textContent += '\n药品救援：\n' + rescues.map(rescue =>
        `${rescue.rescue_id} · ${rescue.zone_id} · ${rescue.image} → ${rescue.drone_id || '尚未分配'}（${rescueLabels[rescue.status] || rescue.status}）` +
        (rescue.target_position ? `；落点 [${rescue.target_position.map(value => value.toFixed(1)).join(', ')}] 米` : '') +
        (rescue.error ? `：${rescue.error}` : '')).join('\n');
      const delivering = rescues.some(rescue => ['dispatching', 'delivering'].includes(rescue.status));
      const responseLabels = {queued: '等待前一目标处置', dispatching: '正在派机', responding: '抵近并执行投网',
        resolved: '投网处置完成，保持悬停', failed: '失败', uncertain: '执行状态未确认', stopped: '已停止'};
      const responses = mission.responses || [];
      const phaseLabels = {approaching: '抵近目标', threat: '持刀威胁', following: '跑开与无人机跟随',
        confronting: '转身对抗', netting: '投网与倒地', resolved: '处置完成'};
      if (responses.length) $('llm-queue').textContent += '\n持刀人员处置：\n' + responses.map(response =>
        `${response.response_id} · ${response.zone_id} · ${response.image} → ${response.drone_id || '尚未分配'}（${response.status === 'responding' && phaseLabels[response.phase] || responseLabels[response.status] || response.status}）` +
        (response.person_id ? `；人物 ${response.person_id}` : '') +
        (response.error ? `：${response.error}` : '')).join('\n');
      const responding = responses.some(response => ['queued', 'dispatching', 'responding'].includes(response.status));
      const messages = {running: mission.tasks.some(task => task.status === 'analyzing') ?
        '巡查机已返航，正在完成照片分析。' : mission.tasks.some(task => task.monitor.status === 'flying')
          ? '人员巡查进行中 · 车辆监测等待监控机就位。' : '人员巡查与车辆监测正在进行。',
        completed: '搜索已完成 · 巡查机已返航降落，监控机保持高空悬停。',
        failed: '任务执行失败。' + (mission.tasks.find(task => task.error)?.error || rescues.find(rescue => rescue.error)?.error ||
          responses.find(response => response.error)?.error || '')};
      if (mission.status === 'running' && delivering)
        messages.running = '任务执行中 · 已发现病人，救援无人机正在执行药品投递。';
      if (mission.status === 'completed' && rescues.length)
        messages.completed = '巡查及药品救援已完成 · 救援机和监控机保持悬停。';
      if (mission.status === 'running' && responding)
        messages.running = delivering ? '任务执行中 · 药品投递与持刀人员投网处置正在进行。' :
          '任务执行中 · 已发现持刀人员，处置无人机正在抵近并投网。';
      if (mission.status === 'completed' && responses.length)
        messages.completed = '巡查及异常目标处置已完成 · 任务无人机保持悬停。';
      if (mission.traffic?.active && ['completed', 'failed'].includes(mission.status))
        messages[mission.status] += ' 空地联合车辆监测继续运行，点击「新任务」结束并复位。';
      setStatus(mission.status === 'failed' ? 'error' : flying ? 'busy' : 'ready', messages[mission.status] || '正在确认执行状态…');
      newTask.hidden = false;
      if (resultPanel.hidden && data.plan) {
        resultPanel.hidden = false; form.hidden = true;
        $('llm-submitted').textContent = '已恢复最近搜索任务';
        $('llm-answer').textContent = data.plan.message;
        $('llm-commands').textContent = JSON.stringify(data.plan.commands, null, 2);
        $('llm-command-panel').hidden = false;
      }
    } else if (flying) setStatus('busy', '正在确认任务派发状态，请勿重复提交。');
    if (data.uncertain) setStatus('error', '执行状态尚未确认，正在重新查询；请勿重复提交。');
    controls();
  }

  // 页面打开或刷新时恢复任务，运行期间每秒轮询；查询失败保持锁定并重试。
  async function pollMission(resetIdle = true) {
    clearTimeout(pollTimer);
    const current = generation;
    let snapshot = null;
    try {
      const data = await missionRequest('/api/llm/mission');
      if (current !== generation) return;
      snapshot = data;
      checking = false; showMission(data);
      if (resetIdle && !mission && !flying && !pending && !resetting && !resetFailed)
        setStatus('idle', data.reset_completed ? '后台任务已终止，无人机已复位，轨迹已清除，可以输入新任务。' : '输入任务后自动规划并开始搜索。');
    } catch (error) {
      if (current !== generation) return;
      checking = true; controls(); setStatus('error', `无法确认执行状态：${error.message} 正在重试。`);
    }
    if (checking || flying || resetting) pollTimer = setTimeout(pollMission, 1000);
    return snapshot;
  }

  function selectTab(selected) {
    for (const tab of tabs) {
      const active = tab === selected;
      tab.setAttribute('aria-selected', String(active));
      tab.tabIndex = active ? 0 : -1;
      $(tab.getAttribute('aria-controls')).hidden = !active;
    }
    window.dispatchEvent(new CustomEvent('sidebar:change', {detail: {panel: selected.getAttribute('aria-controls')}}));
  }
  for (const [index, tab] of tabs.entries()) {
    tab.onclick = () => selectTab(tab);
    tab.onkeydown = event => {
      const next = {ArrowLeft: (index + tabs.length - 1) % tabs.length, ArrowRight: (index + 1) % tabs.length, Home: 0, End: tabs.length - 1}[event.key];
      if (next === undefined) return;
      event.preventDefault(); selectTab(tabs[next]); tabs[next].focus();
    };
  }

  function setStatus(state, message) {
    const changed = status.textContent !== message;
    status.dataset.state = state;
    status.textContent = message;
    if (changed && state === 'error' && !$('llm-panel').hidden) status.scrollIntoView({block: 'nearest'});
  }

  async function plan(source, button) {
    if (pending || checking || flying || resetting || resetFailed || modelPending || !modelBackend) return;
    const current = generation;
    const instruction = source.value.trim();
    if (!instruction) { setStatus('error', '请先输入任务描述。'); source.focus(); return; }
    if (conversation.length > 12 || instruction.length + conversation.reduce((n, m) => n + m.content.length, 0) > 16000) {
      setStatus('error', '当前对话已达到长度上限，请点击「新任务」后描述完整要求。'); return;
    }
    if (location.protocol === 'file:') { setStatus('error', '请通过本地 HTTP 服务打开页面。'); return; }
    pending = true;
    controls();
    const buttonText = button.textContent;
    button.textContent = '正在规划…'; $('llm-panel').setAttribute('aria-busy', 'true');
    setStatus('busy', '正在读取无人机状态并查询地图，请稍候…');
    const controller = new AbortController();
    planningController = controller;
    const started = performance.now();
    const elapsed = () => ((performance.now() - started) / 1000).toFixed(1);
    const ticker = setInterval(() => {
      if (current !== generation || resetting) return;
      status.textContent = `正在查询地图并等待模型判断 · 已用 ${Math.floor((performance.now() - started) / 1000)} 秒`;
    }, 1000);
    const timeout = setTimeout(() => controller.abort(), 675000);
    try {
      const response = await fetch('/api/llm/plan', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({instruction, conversation,
          flight: {max_height: flightSettings.max_height, flight_speed: flightSettings.flight_speed,
            flight_height: flightSettings.flight_height}}), signal: controller.signal
      });
      if (!response.headers.get('Content-Type')?.includes('application/json')) {
        throw new Error('网页服务未返回有效数据，请重启 node web/serve.cjs 后刷新页面。');
      }
      const result = await response.json();
      if (current !== generation) return;
      if (!response.ok) throw new Error(result.error || '规划请求失败，请重试。');
      if (!['ready', 'needs_clarification'].includes(result.status) ||
          typeof result.message !== 'string' || !result.message.trim() || !Array.isArray(result.commands)) {
        throw new Error('服务返回格式异常，请重试。');
      }
      const ready = result.status === 'ready';
      needsClarification = result.status === 'needs_clarification';
      const answer = result.message;
      if (previousTurn) {
        for (const [label, text] of [['你', previousTurn.instruction], ['助手', previousTurn.answer]]) {
          const message = document.createElement('p');
          message.textContent = `${label}：${text}`; $('llm-messages').append(message);
        }
        $('llm-history').hidden = false;
      }
      const commands = result.commands;
      conversation.push({role: 'user', content: instruction}, {role: 'assistant', content: JSON.stringify({
        status: result.status, message: answer, tasks: commands.map(({sequence, ...task}) => task)})});
      previousTurn = {instruction, answer};
      $('llm-submitted').textContent = instruction;
      $('llm-answer-heading').textContent = ready ? '规划说明' : '需要你补充';
      $('llm-answer').textContent = answer;
      $('llm-elapsed').textContent = `${elapsed()} 秒`;
      $('llm-command-panel').hidden = !ready;
      $('llm-queue').hidden = !ready;
      if (ready) {
        $('llm-queue').textContent = `共 ${commands.length} 个区域 · 执行时自动选择空闲无人机：\n` +
          commands.map(c => `${c.sequence}. ${c.target.zone_id} 完整街区`).join('\n');
        $('llm-commands').textContent = JSON.stringify(commands.length === 1 ? commands[0] : {tasks: commands}, null, 2);
      } else $('llm-commands').textContent = '';
      form.hidden = true; newTask.hidden = false;
      followupInput.value = '';
      resultPanel.hidden = false;
      setStatus(ready ? 'busy' : 'question', ready ? '规划完成 · 正在确认执行状态。' : '等待补充信息 · 未生成指令。');
      if (ready) {
        if (!result.execution) throw Error('执行服务没有返回任务状态，请刷新页面核实。');
        showMission({execution: result.execution, plan: result});
        if (flying) { clearTimeout(pollTimer); pollTimer = setTimeout(pollMission, 1000); }
      }
      if (!$('llm-panel').hidden) resultPanel.scrollIntoView({block: 'nearest'});
    } catch (error) {
      if (current !== generation) return;
      const message = error.name === 'AbortError' ? '规划请求超时，正在核实是否已开始执行。' : error.message;
      setStatus('error', message);
      // 派发可能已成功但 HTTP 响应丢失；先查后台，避免盲目再次提交。
      checking = true; await pollMission(false);
      if (!checking && !flying) setStatus('error', message);
    } finally {
      clearTimeout(timeout); clearInterval(ticker);
      if (current === generation) {
        pending = false; planningController = null;
        controls(); button.textContent = buttonText;
        $('llm-panel').removeAttribute('aria-busy');
      }
    }
  };
  form.onsubmit = event => { event.preventDefault(); void plan(input, submit); };
  followupForm.onsubmit = event => {
    event.preventDefault();
    if (needsClarification) void plan(followupInput, followupSubmit);
  };
  function clearTask() {
    mission = null; flying = false; pending = false; checking = false;
    conversation = []; previousTurn = null;
    needsClarification = false;
    $('llm-messages').replaceChildren(); $('llm-history').hidden = true;
    $('llm-history').open = false; resultPanel.hidden = true;
    $('llm-commands').textContent = ''; $('llm-answer').textContent = ''; $('llm-queue').textContent = '';
    $('llm-submitted').textContent = ''; $('llm-elapsed').textContent = '';
    $('llm-answer-heading').textContent = '模型回答';
    $('llm-vision').hidden = true; $('llm-anomalies').replaceChildren();
    shownAnomalies.clear(); visionMission = null;
    $('llm-traffic').hidden = true; trafficMission = null;
    window.trafficView.clear();
    input.value = ''; followupInput.value = ''; form.hidden = false;
    if ($('llm-image-dialog').open) $('llm-image-dialog').close();
    input.focus();
  }

  newTask.onclick = async () => {
    if (resetting) return;
    generation++;
    const current = generation;
    planningController?.abort(); planningController = null;
    clearTimeout(pollTimer);
    pending = false; checking = false; resetting = true; resetFailed = false;
    submit.textContent = '规划并开始搜索 →'; followupSubmit.textContent = '发送补充 →';
    $('llm-panel').removeAttribute('aria-busy');
    controls(); setStatus('busy', '正在终止后台任务并复位全部无人机，请稍候…');
    try {
      const data = await missionRequest('/api/llm/reset', {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'
      });
      if (current !== generation) return;
      showMission(data);
      if (resetting) pollTimer = setTimeout(pollMission, 500);
    } catch (error) {
      if (current !== generation) return;
      if (error.statusCode === 404) {
        resetting = false; resetFailed = true;
        setStatus('error', '当前后台仍是旧版本，没有复位接口。请停止旧网页服务，重新运行 node web/serve.cjs 后刷新页面。');
        return;
      }
      setStatus('error', `无法确认复位请求：${error.message} 正在查询后台状态。`);
      const data = await pollMission(false);
      if (data && !data.resetting && !data.reset_error && !data.reset_completed) {
        resetting = false; resetFailed = true;
        setStatus('error', '未收到复位完成确认，请点击「新任务」重试。');
      }
    } finally { controls(); }
  };
  controls(); void pollMission(); void updateBackend();
})();
