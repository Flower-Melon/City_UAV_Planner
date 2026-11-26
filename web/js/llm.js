'use strict';

(() => {
  const $ = id => document.getElementById(id);
  const form = $('llm-form'), input = $('llm-instruction'), submit = $('llm-submit');
  const followupForm = $('llm-followup-form'), followupInput = $('llm-followup-input');
  const followupSubmit = $('llm-followup-submit'), newTask = $('llm-new-task');
  const status = $('llm-status'), clarification = $('llm-clarification');
  const modelSelect = $('llm-model-select'), modelInfo = $('llm-model-info');
  const modelRetry = $('llm-model-retry');
  let modelBackend = null, modelPending = false;
  let pending = false, conversation = [], previousTurn = null;
  let needsClarification = false;
  let checking = true, flying = false, mission = null, pollTimer;
  let resetting = false, resetFailed = false, generation = 0, planningController = null;
  $('llm-image-close').onclick = () => $('llm-image-dialog').close();

  function showTraffic(execution) {
    const traffic = execution?.traffic;
    window.liveMap?.setTraffic((traffic?.targets || []).filter(record => record.events.length || record.status === 'confirmed'));
    if (traffic?.pursuit) window.pursuitView.update(traffic.pursuit);
  }

  // 根据规划与飞行状态锁定输入，防止刷新或重复点击再次派发。
  function controls() {
    for (const element of [submit, input, followupSubmit, followupInput])
      element.disabled = pending || checking || flying || resetting || resetFailed || modelPending || !modelBackend;
    form.hidden = !!mission || needsClarification;
    $('task-presets').hidden = form.hidden;
    for (const button of $('task-presets').querySelectorAll('button')) button.disabled = input.disabled;
    const modelLocked = modelPending || pending || checking || flying || resetting || resetFailed;
    modelSelect.disabled = modelLocked || !modelBackend;
    modelSelect.setAttribute('aria-busy', String(modelPending));
    modelRetry.disabled = modelLocked;
    newTask.hidden = false;
    newTask.disabled = resetting || modelPending;
    newTask.textContent = resetting ? '正在复位…' : pending || flying ? '结束并复位' : '全局复位';
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

  window.addEventListener('map:ready', () => {
    if (mission) {
      showTraffic(mission);
      window.dispatchEvent(new CustomEvent('workspace:mission', {detail: mission}));
    }
  });

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
      setStatus('error', `复位失败：${data.reset_error} 请重试复位。`); controls(); return;
    }
    if (data.reset_completed && !data.execution && (wasResetting || (!pending && form.hidden))) {
      clearTask();
      window.dispatchEvent(new Event('mission:reset'));
      setStatus('idle', '后台任务已终止，无人机已复位，可以输入新任务。');
      controls();
      if (!$('llm-panel').hidden) input.focus();
      return;
    }
    mission = data.execution;
    showTraffic(mission);
    window.dispatchEvent(new CustomEvent('workspace:mission', {detail: mission}));
    flying = !!data.starting || !!data.uncertain || !!mission?.traffic?.active ||
      !!mission?.traffic?.pursuit && !['complete', 'failed', 'stopped'].includes(mission.traffic.pursuit.status) ||
      !!mission && !['completed', 'failed'].includes(mission.status);
    if (mission || flying) needsClarification = false;
    if (mission) {
      const rescues = mission.rescues || [];
      const delivering = rescues.some(rescue => ['dispatching', 'delivering'].includes(rescue.status));
      const responses = mission.responses || [];
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
        messages.completed = '巡查及药品救援已完成 · 救援机已返航降落。';
      if (mission.status === 'running' && responding)
        messages.running = delivering ? '任务执行中 · 药品投递与持刀人员投网处置正在进行。' :
          '任务执行中 · 已发现持刀人员，处置无人机正在抵近并投网。';
      if (mission.status === 'completed' && responses.length)
        messages.completed = '巡查及异常目标处置已完成 · 处置机已返航降落。';
      if (mission.status === 'running' && (rescues.some(rescue => rescue.status === 'returning') ||
          responses.some(response => response.status === 'responding' && response.phase === 'returning')))
        messages.running = '任务执行中 · 无人机正在返航降落。';
      if (mission.traffic?.active && ['completed', 'failed'].includes(mission.status))
        messages[mission.status] += ' 空地联合车辆监测继续运行，可结束并复位。';
      const pursuit = mission.traffic?.pursuit;
      const pursuitMessage = {preparing: '正在调度追踪机队。',
        tracking: pursuit?.target?.intercept_state === 'braking' ? '异常车辆正在减速停车。' : '无人机正在协同追踪车辆。',
        returning: pursuit?.outcome === 'intercepted' ? '车辆已截停，无人机正在归位。' : '无人机正在归位。',
        complete: '车辆追踪任务已完成。',
        failed: '车辆追踪未完成。', stopped: '车辆追踪已结束。'}[pursuit?.status];
      setStatus(pursuit?.status === 'failed' || !pursuit && mission.status === 'failed' ? 'error' : flying ? 'busy' : 'ready',
        pursuitMessage || messages[mission.status] || '正在确认执行状态…');
      newTask.hidden = false;
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
        setStatus('idle', data.reset_completed ? '无人机已复位，可以创建巡查任务。' : '等待创建巡查任务');
    } catch (error) {
      if (current !== generation) return;
      checking = true; controls(); setStatus('error', `无法确认执行状态：${error.message} 正在重试。`);
    }
    if (checking || flying || resetting) pollTimer = setTimeout(pollMission, 1000);
    return snapshot;
  }

  function setStatus(state, message) {
    status.dataset.state = state;
    if (status.textContent !== message) status.textContent = message;
  }

  async function plan(source, button) {
    if (pending || checking || flying || resetting || resetFailed || modelPending || !modelBackend) return;
    const current = generation;
    const instruction = source.value.trim();
    if (!instruction) { setStatus('error', '请先输入任务描述。'); source.focus(); return; }
    if (conversation.length > 12 || instruction.length + conversation.reduce((n, m) => n + m.content.length, 0) > 16000) {
      setStatus('error', '当前对话已达到长度上限，请复位后描述完整要求。'); return;
    }
    if (location.protocol === 'file:') { setStatus('error', '请通过本地 HTTP 服务打开页面。'); return; }
    pending = true;
    controls();
    const buttonLabel = button === submit ? submit.querySelector('span') : button;
    const buttonText = buttonLabel.textContent;
    buttonLabel.textContent = '正在规划…'; $('llm-panel').setAttribute('aria-busy', 'true');
    setStatus('busy', '正在读取无人机状态并查询地图，请稍候…');
    const controller = new AbortController();
    planningController = controller;
    const timeout = setTimeout(() => controller.abort(), 675000);
    let focusFollowup = false;
    try {
      const response = await fetch('/api/llm/plan', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({instruction, conversation, flight: window.getMissionFlightSettings()}), signal: controller.signal
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
      if (ready && !result.execution) throw Error('执行服务没有返回任务状态，请刷新页面核实。');
      needsClarification = !ready;
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
      clarification.textContent = needsClarification ? answer : '';
      newTask.hidden = false;
      followupInput.value = '';
      setStatus(ready ? 'busy' : 'question', ready ? '规划完成 · 正在确认执行状态。' : '等待补充信息 · 未生成指令。');
      if (ready) {
        showMission({execution: result.execution});
        if (flying) { clearTimeout(pollTimer); pollTimer = setTimeout(pollMission, 1000); }
      }
      focusFollowup = needsClarification;
    } catch (error) {
      if (current !== generation) return;
      const message = error.name === 'AbortError' ? '规划请求超时，正在核实是否已开始执行。' : error.message;
      setStatus('error', message);
      // 派发可能已成功但 HTTP 响应丢失；先查后台，避免盲目再次提交。
      checking = true; await pollMission(false);
      if (!checking && !flying) setStatus('error', message);
    } finally {
      clearTimeout(timeout);
      if (current === generation) {
        pending = false; planningController = null;
        controls(); buttonLabel.textContent = buttonText;
        $('llm-panel').removeAttribute('aria-busy');
        if (focusFollowup && !followupForm.hidden) followupInput.focus();
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
    input.value = ''; followupInput.value = ''; form.hidden = false;
    $('llm-messages').replaceChildren(); $('llm-history').hidden = true;
    $('llm-history').open = false;
    clarification.textContent = '';
    showTraffic(null);
    window.dispatchEvent(new CustomEvent('workspace:mission', {detail: null}));
    if ($('llm-image-dialog').open) $('llm-image-dialog').close();
  }

  newTask.onclick = async () => {
    if (resetting) return;
    generation++;
    const current = generation;
    planningController?.abort(); planningController = null;
    clearTimeout(pollTimer);
    pending = false; checking = false; resetting = true; resetFailed = false;
    submit.querySelector('span').textContent = '规划并开始巡查'; followupSubmit.textContent = '发送补充 →';
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
        setStatus('error', '未收到复位完成确认，请重试复位。');
      }
    } finally { controls(); }
  };
  window.addEventListener('vehicle-pursuit:change', () => {
    if (!resetting) void pollMission();
  });
  controls(); void pollMission(); void updateBackend();
})();
