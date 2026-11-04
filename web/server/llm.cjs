'use strict';
// 网页任务流程统一入口：模型规划、巡查监控、视觉分析及病人药品投递。
const {spawn} = require('node:child_process');
const {randomUUID} = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const PythonRuntime = require('./python-runtime.cjs');
const TERMINAL = new Set(['completed', 'failed']);
const DEFAULT_FLIGHT = Object.freeze({max_height: 60, flight_speed: 30, flight_height: 15});
const TARGET_MERGE_DISTANCE_M = 5;
const TARGET_MERGE_HEIGHT_M = 2;

// 相同任务按照片编号排序；跨任务优先比较曝光时间，不依赖模型响应顺序。
function observedBefore(left, right) {
  if (left.task_id === right.task_id) return left.image < right.image;
  if (/^[1-9]\d*$/.test(left.capture_time_ns) && /^[1-9]\d*$/.test(right.capture_time_ns)) {
    const a = BigInt(left.capture_time_ns), b = BigInt(right.capture_time_ns);
    if (a !== b) return a < b;
  }
  return left.sequence < right.sequence || left.sequence === right.sequence && left.image < right.image;
}

function requestError(message, statusCode) {
  return Object.assign(new Error(message), {statusCode});
}

function positive(value, name) {
  if (typeof value !== 'number' || !Number.isFinite(value) || value <= 0)
    throw requestError(`${name}必须为正的有限数值`, 400);
  return value;
}

function flightSettings(flight = {}) {
  if (!flight || typeof flight !== 'object' || Array.isArray(flight))
    throw requestError('飞行参数必须为对象。', 400);
  const labels = {max_height: '最大飞行高度', flight_speed: '飞行速度', flight_height: '巡查高度'};
  const settings = Object.fromEntries(Object.entries(DEFAULT_FLIGHT)
    .map(([key, fallback]) => [key, positive(flight[key] ?? fallback, labels[key])]));
  if (settings.max_height < 15) throw requestError('最大飞行高度不得低于 15 米。', 400);
  return settings;
}

function send(response, code, result) {
  if (response.destroyed || response.writableEnded) return;
  response.writeHead(code, {'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store'});
  response.end(JSON.stringify(result));
}

function planningFleet(state, now = Date.now()) {
  if (!state?.connected || !Array.isArray(state.drones) || !state.drones.length ||
      state.drones.some(d => !Number.isFinite(d.updated_at) || now - d.updated_at > 1000 ||
        d.updated_at > now || typeof d.available !== 'boolean' ||
        !Array.isArray(d.position_m) || d.position_m.length !== 3 || !d.position_m.every(Number.isFinite))) return null;
  return state.drones.map(({name, position_m, available}) => ({name, position_m: [...position_m], available}));
}

// 按区域排队；每组配备巡查机与高空监控机，监控机可在组内转场复用。
class MissionExecutor {
  constructor({root, runtime, timeoutMs = 15000, visionPollMs = 500}) {
    this.root = root;
    this.runtime = runtime;
    this.timeoutMs = timeoutMs;
    this.mission = null;
    this.starting = false;
    this.uncertain = false;
    this.closed = false;
    this.runPromise = null;
    this.visionPollMs = visionPollMs;
    this.imageFiles = new Map();
    this.rescuePromises = [];
    this.targets = [];
  }

  assertIdle() {
    if (this.closed) throw requestError('执行服务已停止，请重启网页服务。', 503);
    if (this.starting || this.uncertain || this.mission && !TERMINAL.has(this.mission.status))
      throw requestError('已有搜索任务正在执行或等待确认，请先查看进度。', 409);
  }

  validate(plan) {
    if (!plan || plan.status !== 'ready' ||
        !Array.isArray(plan.commands) || !plan.commands.length)
      throw requestError('只接受通过校验的 ready 搜索规划。', 400);
    const map = JSON.parse(fs.readFileSync(path.join(this.root, 'Map', 'zones', 'functional_zones.json'), 'utf8').replace(/^\uFEFF/, ''));
    const zoneIds = new Set(map.features.map(feature => feature.id));
    const sequences = new Set(), targets = new Set();
    const tasks = plan.commands.map(command => {
      if (!command || typeof command !== 'object') throw requestError('搜索任务必须是对象。', 400);
      const sequence = command.sequence, raw = command.target;
      if (Object.hasOwn(command, 'drone_id'))
        throw requestError('搜索任务不接受 drone_id，无人机由执行端自动选择。', 400);
      if (!Number.isSafeInteger(sequence) || sequence < 1)
        throw requestError('sequence 必须为正整数。', 400);
      if (sequences.has(sequence)) throw requestError('sequence 不能重复。', 400);
      sequences.add(sequence);
      if (!raw || typeof raw.zone_id !== 'string' || !zoneIds.has(raw.zone_id))
        throw requestError('target 必须包含真实的 zone_id。', 400);
      if (Object.keys(raw).some(key => key !== 'zone_id'))
        throw requestError('target 仅接受 zone_id，搜索按完整街区道路覆盖。', 400);
      const key = raw.zone_id;
      if (targets.has(key)) throw requestError('同一搜索范围不能重复派发。', 400);
      targets.add(key);
      return {drone_id: null, sequence, target: {zone_id: raw.zone_id}, status: 'queued'};
    });
    if ([...sequences].some(number => number > sequences.size))
      throw requestError('sequence 必须从 1 开始连续编号。', 400);
    return tasks.sort((a, b) => a.sequence - b.sequence);
  }

  async start(plan, flight) {
    this.assertIdle(); this.starting = true;
    try {
      const settings = flightSettings(flight);
      const tasks = this.validate(plan);
      const fleet = await this.runtime.request('fleet', {}, {channel: 'fleet', timeoutMs: this.timeoutMs});
      const available = new Set(Array.isArray(fleet) ? fleet.filter(drone => drone.available === true).map(drone => drone.name) : []);
      if (available.size < 2)
        throw requestError('区域搜索和高空监控至少需要两架可用无人机，请稍后重试。', 409);
      this.parallelism = Math.min(Math.floor(available.size / 2), tasks.length);
      if (this.closed) throw requestError('执行服务已停止。', 503);
      this.mission = {mission_id: randomUUID().replaceAll('-', ''), status: 'running', executed: false,
        settings, tasks, rescues: []};
      this.imageFiles.clear();
      this.rescuePromises = [];
      this.targets = [];
      tasks.forEach((task, index) => {
        task.task_id = `task_${index + 1}`;
        task.capture_directory = null;
        task.monitor = {status: 'queued', drone_id: null, position_m: null, flight_height: null};
        task.vision = {status: 'queued', total: 0, analyzed: 0, filtered: 0, llm_analyzed: 0, normal: 0, abnormal: 0,
          patient: 0, armed_person: 0,
          uncertain: 0, failed: 0, anomalies: [], errors: []};
      });
      this.runPromise = this.run();
      return this.status();
    } finally { this.starting = false; }
  }

  async run() {
    try {
      const queue = [...this.mission.tasks];
      // 等待全部工作组结束，确保不会在收尾后继续新增救援任务。
      const workers = await Promise.allSettled(Array.from({length: this.parallelism}, (_, index) => this.fly(queue, index)));
      const failure = workers.find(result => result.status === 'rejected');
      if (failure) throw failure.reason;
    } catch (error) {
      this.mission.error_type = error.errorType || error.name;
    } finally {
      // 投递独立于视觉分析并行执行；全部已派发投递返回后才结束全流程。
      const rescues = await Promise.allSettled(this.rescuePromises);
      const rescueFailure = rescues.find(result => result.status === 'rejected');
      if (rescueFailure) this.mission.error_type = rescueFailure.reason.errorType || rescueFailure.reason.name;
      for (const task of this.mission.tasks) if (task.status === 'queued') {
        task.status = 'skipped'; task.vision.status = 'skipped'; task.monitor.status = 'skipped';
      }
      this.mission.status = this.mission.error_type || this.mission.tasks.some(task => task.status !== 'completed') ||
        this.mission.rescues.some(rescue => rescue.status !== 'delivered') ? 'failed' : 'completed';
    }
  }

  async fly(queue, workerId) {
    let failed = false;
    let monitorDroneId = null, previousMonitor = null;
    while (queue.length) {
      if (failed || this.closed || this.uncertain) return;
      const task = queue.shift();
      task.status = 'running'; this.mission.executed = true;
      let finished = false;
      const analysis = this.analyzeTask(task, () => finished, workerId).catch(error => {
        task.vision.status = 'failed'; task.vision.error = error.message;
      });
      const monitoring = this.startMonitor(task, workerId, monitorDroneId);
      try {
        // 先确认监控机已占用，再启动巡查选机；两条航线仍并行飞行。
        await monitoring.selected;
        monitorDroneId = task.monitor.drone_id;
        if (previousMonitor) previousMonitor.status = 'relocated';
        previousMonitor = task.monitor;
        if (this.closed || this.uncertain) throw Error('任务已停止，未启动区域巡查。');
        const result = await this.runtime.request('search', {
          target: structuredClone(task.target),
          flight: {...this.mission.settings}
        }, {channel: `search:${workerId}`, timeoutMs: 0, onEvent: event => {
          if (event?.type === 'drone_selected') this.setSelectedDrone(task, event.drone_id);
          if (event?.type === 'capture_directory') this.setCaptureDirectory(task, event.capture_directory);
        }});
        if (result?.completed !== true) throw Object.assign(Error('搜索进程未确认任务完成。'), {uncertain: true});
        this.setSelectedDrone(task, result.drone_id);
        this.setCaptureDirectory(task, result.capture_directory);
        task.status = 'analyzing';
      } catch (error) {
        Object.assign(task, {status: 'failed', error_type: error.errorType || error.name, error: error.message});
        this.uncertain ||= !!error.uncertain;
        failed = true;
      } finally {
        finished = true;
        const [monitorError] = await Promise.all([monitoring.completed, analysis]);
        if (monitorError) {
          Object.assign(task, {status: 'failed', error_type: monitorError.errorType || monitorError.name,
            error: `高空监控失败：${monitorError.message}`});
          failed = true;
        }
        if (task.status === 'analyzing') task.status = 'completed';
      }
    }
  }

  startMonitor(task, workerId, droneId) {
    const monitor = task.monitor;
    monitor.status = 'flying';
    let selected, selectionFailed;
    const selection = new Promise((resolve, reject) => { selected = resolve; selectionFailed = reject; });
    const assign = name => {
      if (typeof name !== 'string' || !name.trim() || name !== name.trim() ||
          monitor.drone_id && monitor.drone_id !== name || droneId && droneId !== name)
        throw Error('执行端返回的监控无人机ID无效或发生变更。');
      monitor.drone_id = name;
      selected();
    };
    // 始终将失败转换为结果，避免等待巡查完成期间出现未处理的Promise拒绝。
    const completed = this.runtime.request('monitor', {
      zone_id: task.target.zone_id, drone_id: droneId
    }, {channel: `monitor:${workerId}`, timeoutMs: 0, onEvent: event => {
      if (event?.type === 'drone_selected') assign(event.drone_id);
    }}).then(result => {
      if (result?.hovering !== true || !Array.isArray(result.position_m) || result.position_m.length !== 3 ||
          !result.position_m.every(Number.isFinite) || !Number.isFinite(result.flight_height) || result.flight_height <= 0)
        throw Object.assign(Error('监控进程未确认到位悬停。'), {uncertain: true});
      assign(result.drone_id);
      Object.assign(monitor, {status: 'hovering', position_m: result.position_m, flight_height: result.flight_height});
      return null;
    }).catch(error => {
      Object.assign(monitor, {status: 'failed', error_type: error.errorType || error.name, error: error.message});
      this.uncertain ||= !!error.uncertain;
      selectionFailed(error);
      return error;
    });
    return {selected: selection, completed};
  }

  setSelectedDrone(task, name) {
    if (typeof name !== 'string' || !name.trim() || name !== name.trim() ||
        task.drone_id && task.drone_id !== name || task.monitor.drone_id === name)
      throw Error('执行端返回的无人机ID无效或发生变更。');
    task.drone_id = name;
  }

  setCaptureDirectory(task, directory) {
    if (typeof directory !== 'string' || !directory) throw Error('搜索进程未返回照片目录。');
    const actual = fs.realpathSync(path.resolve(this.root, directory));
    const relative = path.relative(fs.realpathSync(path.join(this.root, 'output')), actual);
    if (!/^\d{6,}[\\/]capture$/.test(relative)) throw Error('搜索照片目录必须为output/编号/capture。');
    const normalized = path.relative(this.root, actual);
    if (task.capture_directory && task.capture_directory !== normalized ||
        this.mission.tasks.some(other => other !== task && other.capture_directory === normalized))
      throw Error('搜索任务照片目录重复或发生变更。');
    task.capture_directory = normalized;
  }

  readFrames(task) {
    if (!task.capture_directory) return {directory: null, frames: []};
    const directory = path.join(this.root, task.capture_directory);
    const manifest = path.join(directory, 'frame.json');
    if (!fs.existsSync(manifest)) return {directory, frames: []};
    const frames = JSON.parse(fs.readFileSync(manifest, 'utf8'));
    if (!Array.isArray(frames) || frames.some(frame => !frame || !/^\d{6}\.jpg$/.test(frame.image)))
      throw Error('巡查照片记录无效，视觉分析已停止。');
    return {directory, frames};
  }

  async analyzeTask(task, finished, workerId) {
    const seen = new Set();
    const summary = task.vision;
    summary.status = 'running';
    while (!this.closed) {
      const {directory, frames} = this.readFrames(task);
      summary.total = frames.length;
      for (const frame of frames) {
        if (this.closed) break;
        if (seen.has(frame.image)) continue;
        seen.add(frame.image);
        const record = {image: frame.image, source_image: `../capture/${frame.image}`,
          drone_id: task.drone_id, task_id: task.task_id,
          sequence: task.sequence, capture_time_ns: frame.capture_time_ns || null,
          zone_id: task.target.zone_id, position_m: frame.position_m, edge_id: frame.edge_id};
        try {
          // 每个目标请求由模型客户端单独限制超时，整帧不再共用固定的两分钟额度。
          const result = await this.runtime.request('vision', {
            image_path: path.relative(this.root, path.join(directory, frame.image))
          }, {channel: `vision:${workerId}`, timeoutMs: 0});
          if (this.closed) break;
          if (!result || !['normal', 'abnormal', 'uncertain'].includes(result.status) ||
              typeof result.description !== 'string' || !Array.isArray(result.detections) ||
              typeof result.llm_skipped !== 'boolean' || !Number.isSafeInteger(result.yolo_detection_count) ||
              typeof result.llm_requested !== 'boolean' || typeof result.analysis_failed !== 'boolean' ||
              result.yolo_detection_count < 0 || result.llm_skipped !== (result.yolo_detection_count === 0) ||
              (result.status === 'abnormal') !== (result.detections.length > 0) ||
              result.detections.some(detection => !detection || !['patient', 'armed_person'].includes(detection.status) ||
                typeof detection.label !== 'string' || !detection.label.trim() ||
                !Array.isArray(detection.bbox) || detection.bbox.length !== 4 ||
                detection.bbox.some(v => !Number.isFinite(v) || v < 0 || v > 1000) ||
                detection.bbox[0] >= detection.bbox[2] || detection.bbox[1] >= detection.bbox[3]))
            throw Error('视觉模型返回无效判断或异常框。');
          Object.assign(record, result);
          if (result.llm_skipped) {
            summary.analyzed++; summary.filtered++; summary.normal++;
            continue;
          }
          record.yolo_report = `../yolo_detection/${path.parse(frame.image).name}.json`;
          record.analysis_file = `${path.parse(frame.image).name}.json`;
          if (result.analysis_failed)
            throw Error('本图所有目标的意图识别失败：' + (result.target_errors?.[0]?.error || result.description));
          summary.analyzed++; summary[result.status]++;
          summary.llm_analyzed++;
          // 图片可能同时包含病人和持刀人员，按目标分别计数并保留每个框的类别。
          for (const detection of result.detections) summary[detection.status]++;
          for (const error of result.target_errors || []) {
            summary.errors.push({image: frame.image, error: `目标${error.target_index}：${error.error}`});
          }
          if (result.status === 'abnormal') {
            record.annotated_image = `abnormal/${frame.image}`;
            const id = `${task.task_id}:${frame.image}`;
            this.imageFiles.set(id, path.join(directory, frame.image));
            record.image_url = `/api/llm/image?mission_id=${this.mission.mission_id}&id=${encodeURIComponent(id)}`;
            summary.anomalies.push(record);
            const matched = new Set();
            for (const detection of record.detections) {
              if (this.closed) break;
              await this.registerTarget(task, record, detection, directory, workerId, matched);
            }
          }
        } catch (error) {
          if (this.closed) break;
          summary.failed++;
          summary.errors.push({image: frame.image, error: error.message});
        }
      }
      // 完成标记可能在等待模型期间改变，重新读取清单后才能确认没有尾帧。
      if (finished() && this.readFrames(task).frames.every(frame => seen.has(frame.image))) break;
      await new Promise(resolve => setTimeout(resolve, this.visionPollMs));
    }
    summary.status = this.closed ? 'stopped' : summary.failed || summary.errors.length ? 'completed_with_errors' : 'completed';
  }

  async registerTarget(task, record, detection, directory, workerId, matched) {
    let position = null, locationError = null;
    try {
      const result = await this.runtime.request('locate_target', {
        image_path: path.relative(this.root, path.join(directory, record.image)), bbox: [...detection.bbox]
      }, {channel: `location:${workerId}`, timeoutMs: this.timeoutMs});
      if (this.closed) return;
      position = result?.target_position;
      if (!Array.isArray(position) || position.length !== 3 || !position.every(Number.isFinite))
        throw Error('未返回有效目标地面坐标。');
    } catch (error) {
      if (this.closed) return;
      position = null;
      locationError = `目标地面定位失败：${error.message}`;
      task.vision.errors.push({image: record.image, error: locationError});
    }
    // 匹配与登记间没有await，多个视觉工作组也不会为同一位置重复建立目标。
    // 同一照片内一对一匹配，避免把并排出现的不同人员合并。
    let target = null, nearest = Infinity;
    if (position) for (const candidate of this.targets) {
      if (candidate.status !== detection.status || !candidate.position_m || matched.has(candidate.target_id)) continue;
      const distance = Math.hypot(candidate.position_m[0] - position[0], candidate.position_m[1] - position[1]);
      if (distance <= TARGET_MERGE_DISTANCE_M && distance < nearest &&
          Math.abs(candidate.position_m[2] - position[2]) <= TARGET_MERGE_HEIGHT_M) {
        target = candidate; nearest = distance;
      }
    }
    const observation = {...record, description: detection.label, detections: [structuredClone(detection)]};
    if (!target) {
      target = {target_id: `target_${this.targets.length + 1}`, status: detection.status,
        position_m: position && [...position], sightings: 0, rescue_id: null,
        first_observation: observation, last_observation: observation, location_error: locationError};
      this.targets.push(target);
    } else {
      if (observedBefore(observation, target.first_observation)) target.first_observation = observation;
      if (observedBefore(target.last_observation, observation)) {
        target.last_observation = observation;
        target.position_m = [...position];
      }
    }
    target.sightings++;
    matched.add(target.target_id);
    detection.target_id = target.target_id;
    detection.position_m = position && [...position];
    // 只有首次登记的病人才创建救援；失败、执行中及已完成均不会被重复帧再次派发。
    if (target.status === 'patient' && !target.rescue_id) this.rescuePatient(target);
    detection.rescue_id = target.rescue_id;
  }

  rescuePatient(target) {
    const record = target.first_observation;
    const rescue = {rescue_id: `rescue_${this.mission.rescues.length + 1}`, target_id: target.target_id,
      task_id: record.task_id, zone_id: record.zone_id, image: record.image,
      bbox: [...record.detections[0].bbox], drone_id: null,
      requested_position: target.position_m && [...target.position_m],
      target_position: target.position_m && [...target.position_m],
      status: target.position_m ? 'dispatching' : 'failed'};
    if (target.location_error) rescue.error = target.location_error;
    target.rescue_id = rescue.rescue_id;
    this.mission.rescues.push(rescue);
    if (rescue.status === 'dispatching') {
      // 每个病人使用独立进程立即派发，不排队、不等待药箱、不复用此前的救援机。
      this.rescuePromises.push(this.deliverRescue(rescue));
    }
  }

  async deliverRescue(rescue) {
    const channel = `delivery:${rescue.rescue_id}`;
    const setTarget = position => {
      const requested = rescue.requested_position;
      if (!Array.isArray(position) || position.length !== 3 || !position.every(Number.isFinite) ||
          Math.hypot(position[0] - requested[0], position[1] - requested[1]) > 5.01 ||
          Math.abs(position[2] - requested[2]) > 1.01)
        throw Error('执行端返回的药品落点无效或超出病人附近的投递范围。');
      rescue.target_position = [...position];
    };
    const assign = name => {
      if (typeof name !== 'string' || !name.trim() || name !== name.trim() ||
          rescue.drone_id && rescue.drone_id !== name ||
          this.mission.rescues.some(other => other !== rescue && other.drone_id === name) ||
          this.mission.tasks.some(task => task.monitor.drone_id === name))
        throw Error('执行端返回的救援无人机ID无效或重复。');
      rescue.drone_id = name;
      rescue.status = 'delivering';
    };
    try {
      if (this.closed || this.uncertain) throw Error('任务已停止或执行状态未确认，未派出救援机。');
      const {max_height, flight_speed} = this.mission.settings;
      const result = await this.runtime.request('deliver_medicine', {
        zone_id: rescue.zone_id, target_position: [...rescue.target_position], flight: {max_height, flight_speed}
      }, {channel, timeoutMs: 0, onEvent: event => {
        if (event?.type === 'drone_selected') assign(event.drone_id);
        if (event?.type === 'delivery_target_selected') setTarget(event.target_position);
      }});
      if (result?.delivered !== true)
        throw Object.assign(Error('救援进程未确认药品投递完成。'), {uncertain: true});
      assign(result.drone_id);
      setTarget(result.target_position);
      rescue.status = 'delivered';
    } catch (error) {
      this.uncertain ||= !!error.uncertain;
      Object.assign(rescue, {status: this.closed ? 'stopped' : error.uncertain ? 'uncertain' : 'failed',
        error_type: error.errorType || error.name, error: error.message});
    } finally {
      this.runtime.cancel(channel);
    }
  }

  status() {
    if (!this.mission) return null;
    const mission = structuredClone(this.mission);
    const summaries = mission.tasks.map(task => task.vision);
    mission.vision = Object.fromEntries(['total', 'analyzed', 'filtered', 'llm_analyzed', 'normal', 'abnormal',
      'patient', 'armed_person', 'uncertain', 'failed']
      .map(key => [key, summaries.reduce((sum, item) => sum + item[key], 0)]));
    mission.vision.status = summaries.some(item => ['queued', 'running'].includes(item.status)) ? 'running' :
      summaries.some(item => item.status === 'failed' || item.failed || item.errors.length) ? 'completed_with_errors' : 'completed';
    mission.vision.anomalies = summaries.flatMap(item => item.anomalies);
    mission.vision.targets = structuredClone(this.targets).sort((a, b) =>
      observedBefore(a.first_observation, b.first_observation) ? -1 :
        observedBefore(b.first_observation, a.first_observation) ? 1 : 0);
    mission.vision.unique_targets = this.targets.length;
    mission.vision.unique_patient = this.targets.filter(target => target.status === 'patient').length;
    mission.vision.unique_armed_person = this.targets.filter(target => target.status === 'armed_person').length;
    mission.vision.errors = summaries.flatMap(item => item.error ? [{error: item.error}] : item.errors);
    return mission;
  }
  close() { this.closed = true; }
}

// 网页接口只在此组织“取状态 → 规划 → 派发”，进度直接来自任务对象。
class LlmController {
  constructor({root, getState = () => null, clearTrails = () => {}, spawnProcess = spawn,
    timeoutMs = 660000, executionTimeoutMs = 15000, runtime = null}) {
    this.runtime = runtime || new PythonRuntime({root, spawnProcess});
    this.executor = new MissionExecutor({root, runtime: this.runtime, timeoutMs: executionTimeoutMs});
    this.getState = getState;
    this.clearTrails = clearTrails;
    this.timeoutMs = timeoutMs;
    this.executionTimeoutMs = executionTimeoutMs;
    this.plan = null;
    this.planning = false;
    this.generation = 0;
    this.resetting = false;
    this.resetError = null;
    this.resetCompleted = false;
    this.resetPromise = null;
    this.closed = false;
  }

  start() { this.runtime.start(); }

  snapshot() {
    return {execution: this.executor.status(), plan: this.plan && structuredClone(this.plan),
      starting: this.executor.starting, uncertain: this.executor.uncertain,
      resetting: this.resetting, reset_error: this.resetError, reset_completed: this.resetCompleted};
  }

  beginReset() {
    if (this.resetPromise) return this.resetPromise;
    this.resetting = true;
    this.resetError = null;
    this.resetCompleted = false;
    this.generation++;
    this.planning = false;
    const previous = this.executor;
    previous.close();
    this.resetPromise = (async () => {
      try {
        await this.runtime.terminateAll();
        if (previous.runPromise) await previous.runPromise;
        if (this.closed) return;
        const result = await this.runtime.request('reset', {
          reset_delivery: !!previous.mission?.rescues.some(rescue => rescue.drone_id)
        }, {channel: 'reset', timeoutMs: 120000});
        if (result?.reset !== true) throw Error('无人机复位未确认，请重试。');
        await this.runtime.terminateAll();
        if (this.closed) return;
        await this.clearTrails();
        this.executor = new MissionExecutor({root: previous.root, runtime: this.runtime, timeoutMs: this.executionTimeoutMs});
        this.plan = null;
        this.resetCompleted = true;
      } catch (error) {
        this.resetError = error.message;
      } finally {
        this.resetting = false;
        this.resetPromise = null;
      }
    })();
    return this.resetPromise;
  }

  validate(data) {
    const instruction = data?.instruction, conversation = data?.conversation ?? [];
    if (typeof instruction !== 'string' || !instruction.trim() || instruction.length > 4000 || instruction.includes('\0'))
      throw requestError('请输入 1–4000 字的任务描述。', 400);
    if (!Array.isArray(conversation) || conversation.length > 12 || conversation.length % 2 ||
        conversation.some((message, i) => !message || typeof message !== 'object' ||
          Object.keys(message).sort().join(',') !== 'content,role' || message.role !== (i % 2 ? 'assistant' : 'user') ||
          typeof message.content !== 'string' || !message.content.trim() || message.content.includes('\0')) ||
        instruction.length + conversation.reduce((n, message) => n + message.content.length, 0) > 16000)
      throw requestError('对话记录无效或过长，请开始新任务。', 400);
    const flight = flightSettings(data.flight);
    return {instruction: instruction.trim(), conversation, flight};
  }

  handle(request, response, url) {
    if (url.pathname === '/api/llm/plan') return this.handlePlan(request, response);
    if (url.pathname === '/api/llm/mission') return this.handleMission(request, response);
    if (url.pathname === '/api/llm/image') return this.handleImage(request, response, url);
    if (url.pathname === '/api/llm/reset') return this.handleReset(request, response);
    send(response, 404, {error: '接口不存在'});
  }

  async handlePlan(request, response) {
    if (request.method !== 'POST') {
      response.setHeader('Allow', 'POST'); send(response, 405, {error: '请使用 POST 提交任务。'}); return;
    }
    if (request.headers.origin && request.headers.origin !== `http://${request.headers.host}`) {
      send(response, 403, {error: '请从本地地图页面提交任务。'}); return;
    }
    if (request.headers['content-type']?.split(';')[0].trim().toLowerCase() !== 'application/json') {
      send(response, 415, {error: '任务必须使用 JSON 格式提交。'}); return;
    }
    try {
      if (this.resetting || this.resetError) throw requestError('请先等待无人机复位完成；复位失败时点击新任务重试。', 409);
      if (this.planning) throw requestError('已有任务正在规划，请等待完成。', 409);
      this.executor.assertIdle();
    } catch (error) { send(response, error.statusCode || 409, {error: error.message}); return; }
    this.planning = true;
    this.resetCompleted = false;
    const generation = this.generation;
    const obsolete = () => generation !== this.generation;
    let cancelled = false, dispatching = false, phase = 'input';
    const disconnected = () => {
      if (!obsolete() && !response.writableEnded && !dispatching) {
        cancelled = true; this.runtime.cancel('planning'); this.runtime.cancel('fleet');
      }
    };
    response.once('close', disconnected);
    try {
      const chunks = []; let size = 0;
      for await (const chunk of request) {
        size += chunk.length;
        if (size > 65536) throw requestError('任务内容过长。', 413);
        chunks.push(chunk);
      }
      let data;
      try { data = JSON.parse(Buffer.concat(chunks).toString('utf8')); }
      catch { throw requestError('无法读取任务，请提交有效 JSON。', 400); }
      const {instruction, conversation, flight} = this.validate(data);
      if (obsolete()) throw requestError('任务已取消。', 409);
      if (cancelled || response.destroyed || this.closed) return;
      phase = 'fleet';
      const fleet = planningFleet(this.getState()) || await this.runtime.request('fleet', {},
        {channel: 'fleet', timeoutMs: this.executionTimeoutMs});
      if (obsolete()) throw requestError('任务已取消。', 409);
      if (cancelled || this.closed) return;
      phase = 'plan';
      const result = await this.runtime.request('plan', {instruction, conversation, fleet},
        {channel: 'planning', timeoutMs: this.timeoutMs});
      if (obsolete()) throw requestError('任务已取消。', 409);
      if (cancelled || this.closed) return;
      if (!result || !['ready', 'needs_clarification'].includes(result.status) ||
          typeof result.message !== 'string' || !result.message.trim() ||
          !Array.isArray(result.commands) || (result.status === 'ready') !== (result.commands.length > 0))
        throw Error('规划进程返回格式异常，请重试。');
      if (result.status === 'ready') {
        phase = 'dispatch'; dispatching = true; this.plan = structuredClone(result);
        const execution = await this.executor.start(result, flight);
        if (obsolete()) throw requestError('任务已取消。', 409);
        send(response, 200, {...result, execution});
      } else send(response, 200, result);
    } catch (error) {
      send(response, error.statusCode || (phase === 'plan' ? 502 : 503), {error: error.message});
    } finally {
      if (!obsolete()) this.planning = false;
      response.removeListener('close', disconnected);
    }
  }

  handleMission(request, response) {
    if (request.method !== 'GET') {
      response.setHeader('Allow', 'GET'); send(response, 405, {error: '请求方法错误。'}); return;
    }
    if (request.headers.origin && request.headers.origin !== `http://${request.headers.host}`) {
      send(response, 403, {error: '请从本地地图页面操作任务。'}); return;
    }
    send(response, this.closed ? 503 : 200, {...this.snapshot(), ...(this.closed ? {error: '执行服务已停止。'} : {})});
  }

  handleReset(request, response) {
    if (request.method !== 'POST') {
      response.setHeader('Allow', 'POST'); send(response, 405, {error: '请使用 POST 创建新任务。'}); return;
    }
    if (request.headers.origin && request.headers.origin !== `http://${request.headers.host}`) {
      send(response, 403, {error: '请从本地地图页面操作。'}); return;
    }
    if (request.headers['content-type']?.split(';')[0].trim().toLowerCase() !== 'application/json') {
      send(response, 415, {error: '请使用 JSON 请求。'}); return;
    }
    request.resume();
    if (this.closed) { send(response, 503, {error: '执行服务已停止。'}); return; }
    void this.beginReset();
    send(response, 202, this.snapshot());
  }

  handleImage(request, response, url) {
    if (!['GET', 'HEAD'].includes(request.method)) {
      response.setHeader('Allow', 'GET, HEAD'); send(response, 405, {error: '请求方法错误。'}); return;
    }
    const mission = this.executor.mission;
    const filename = mission?.mission_id === url.searchParams.get('mission_id') &&
      this.executor.imageFiles.get(url.searchParams.get('id'));
    if (!filename) { send(response, 404, {error: '本次任务不存在该异常图片。'}); return; }
    try {
      const actual = fs.realpathSync(filename);
      const base = fs.realpathSync(path.join(this.executor.root, 'output'));
      const relative = path.relative(base, actual);
      if (!relative || relative.startsWith('..') || path.isAbsolute(relative)) throw Error('Invalid path');
      const stat = fs.statSync(actual);
      response.writeHead(200, {'Content-Type': 'image/jpeg', 'Content-Length': stat.size,
        'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'});
      if (request.method === 'HEAD') response.end();
      else fs.createReadStream(actual).on('error', () => response.destroy()).pipe(response);
    } catch { send(response, 404, {error: '异常图片无法读取。'}); }
  }

  close() {
    if (this.closed) return;
    this.closed = true;
    this.executor.close();
    this.runtime.close();
  }
}

module.exports = LlmController;
module.exports.MissionExecutor = MissionExecutor;
