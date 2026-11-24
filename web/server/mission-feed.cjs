'use strict';

const {randomUUID} = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');

const taskStates = {queued: '等待派发', running: '巡查进行中', analyzing: '巡查机已返航，照片分析中',
  completed: '巡查与分析结束', failed: '执行中断', skipped: '未执行'};
const monitorStates = {flying: '监控机前往区域', hovering: '高空监控机已就位', relocated: '监控机已转场', failed: '监控未就位'};
const trafficStates = {running: '车辆监测中', paused: '监测暂停', retrying: '监测重试中', failed: '车辆监测中断',
  stabilizing: '等待监测稳定', timing_unstable: '测速时序不稳定', stopped: '车辆监测已停止'};
const actionStates = {queued: '等待执行', dispatching: '准备中', delivering: '救援进行中', returning: '返航中',
  delivered: '投递完成并降落', responding: '现场流程进行中', resolved: '现场流程结束并降落',
  failed: '执行失败', uncertain: '等待确认', stopped: '已停止'};
const pursuitStates = {preparing: '准备中', tracking: '执行中', returning: '返航中', complete: '已结束', failed: '中断', stopped: '已停止'};
const finalTask = new Set(['completed', 'failed', 'skipped']);
const modelInterval = 15000;

function updateFact(facts, key, kind, title, detail = '', zone = null) {
  facts.set(key, {kind, title, detail, zone});
}

// Reads the executor's state without moving any of its work onto the briefing channel.
class MissionFeed {
  constructor({root, runtime, getExecutor, getMonitoredTargets}) {
    this.catalogPath = path.join(root, 'Map', 'zones', 'functional_zones.json');
    this.directory = path.join(root, 'output', 'mission-briefings');
    this.runtime = runtime;
    this.getExecutor = getExecutor;
    this.getMonitoredTargets = getMonitoredTargets;
    this.session = null;
    this.timer = null;
    this.zones = null;
  }

  start() {
    if (this.timer) return;
    this.timer = setInterval(() => this.tick(), 850);
    this.timer.unref();
  }

  begin() {
    this.clear();
    const session = {id: randomUUID().replaceAll('-', ''), missionId: null,
      phase: 'planning', revision: 0, eventId: 0, events: [], metrics: null, facts: new Map(),
      lastPhotoLog: 0, lastAttempt: 0, running: false, failures: 0, retryAt: 0, report: null, updates: [],
      logError: false, dataError: false};
    this.session = session;
    this.stage('planning', '读取机队状态', '正在确认可用无人机。');
    return session;
  }

  stage(phase, title, detail = '') {
    const session = this.session;
    if (!session) return;
    session.phase = phase;
    this.record(session, [{kind: 'flow', title, detail}], {...session.current, phase});
  }

  bind(session, missionId) {
    if (!session || this.session !== session) return;
    session.missionId = missionId;
    this.tick();
  }

  record(session, changes, current) {
    if (!changes.length) return;
    const at = Date.now();
    const events = changes.map(event => ({...event, id: ++session.eventId, at}));
    session.events = [...session.events, ...events].slice(-12);
    session.revision++;
    if (session.logError) return;
    try {
      if (session.revision === 1) fs.mkdirSync(this.directory, {recursive: true});
      const filename = path.join(this.directory, `${session.id}.jsonl`);
      fs.appendFileSync(filename, JSON.stringify({mission_id: session.missionId, revision: session.revision, current, events}) + '\n', 'utf8');
    } catch (error) {
      session.logError = true;
      console.warn(`无法写入任务战报日志 (${error.code || error.name})`);
    }
  }

  zoneName(id) {
    if (!this.zones) {
      try {
        const data = JSON.parse(fs.readFileSync(this.catalogPath, 'utf8').replace(/^\uFEFF/, ''));
        this.zones = new Map(data.features.map(zone => [zone.id, zone.properties.name]));
      } catch { this.zones = new Map(); }
    }
    return this.zones.get(id) || id;
  }

  capture(session, executor) {
    const mission = executor.mission;
    const facts = new Map();
    const metrics = {zones: mission.tasks.length, zones_done: 0, photos: 0, analyzed: 0, abnormal_photos: 0,
      vision_failed: 0, vision_uncertain: 0, vision_issue_zones: 0,
      people: executor.targets.length, vehicles: 0, vehicles_pending: 0, probe_issue_zones: 0};
    const zones = [];
    for (const task of mission.tasks) {
      const zone = task.target.zone_id;
      const name = this.zoneName(zone);
      const monitor = task.monitor;
      const observation = monitor.traffic;
      const vision = task.vision;
      metrics.zones_done += Number(task.status === 'completed');
      metrics.photos += vision.total;
      metrics.analyzed += vision.analyzed;
      metrics.abnormal_photos += vision.abnormal;
      metrics.vision_failed += vision.failed;
      metrics.vision_uncertain += vision.uncertain;
      metrics.vision_issue_zones += Number(!!vision.error || !!vision.errors.length || !!vision.uncertain);
      metrics.probe_issue_zones += Number(!!observation.fusion?.error);
      if (zones.length < 12) zones.push({name, status: taskStates[task.status], search_drone: task.drone_id,
        monitor_drone: monitor.drone_id, monitor: monitorStates[monitor.status], traffic: trafficStates[observation.status]});
      const detail = [task.drone_id && `巡查机 ${task.drone_id}`, monitor.drone_id && `监控机 ${monitor.drone_id}`].filter(Boolean).join(' · ');
      updateFact(facts, `task:${task.task_id}`, 'zone', `${name} · ${taskStates[task.status]}`, detail, zone);
      if (monitorStates[monitor.status] || trafficStates[observation.status])
        updateFact(facts, `monitor:${task.task_id}`, 'traffic', `${name} · ${trafficStates[observation.status] || monitorStates[monitor.status]}`,
          [monitor.drone_id, monitorStates[monitor.status]].filter(Boolean).join(' · '), zone);
    }
    for (const target of this.getMonitoredTargets(mission.tasks)) {
      metrics.vehicles += Number(target.status === 'confirmed');
      metrics.vehicles_pending += Number(target.status === 'observing');
    }
    const aircraft = mission.status === 'running' ? '执行中' : mission.status === 'completed' ? '已完成' : '执行异常';
    const monitoring = executor.trafficActive ? '车辆监测继续运行' : executor.trafficSuspended ? '车辆监测已暂停' : '';
    session.phase = executor.uncertain ? 'uncertain' : mission.status;
    updateFact(facts, 'mission', 'flow', executor.uncertain ? '执行状态等待核实' : `巡查流程${aircraft}`, monitoring);
    metrics.medical_clues = executor.targets.filter(target => target.status === 'patient').length;
    if (metrics.people) updateFact(facts, 'people', 'attention', '识别到独立人员线索',
      `${metrics.people} 个线索，含 ${metrics.medical_clues} 个医疗求助线索。`);
    if (metrics.vehicles || metrics.vehicles_pending) updateFact(facts, 'vehicles', 'attention', '车辆联合研判更新',
      `${metrics.vehicles} 个已确认，${metrics.vehicles_pending} 个待核验。`);
    if (metrics.vision_issue_zones) updateFact(facts, 'vision-issues', 'attention', '部分照片分析需要复核',
      `涉及 ${metrics.vision_issue_zones} 个巡查区域，${metrics.vision_failed} 张分析失败，${metrics.vision_uncertain} 张判断不确定。`);
    if (metrics.probe_issue_zones) updateFact(facts, 'probe-issues', 'attention', '部分路口监测记录暂不可用',
      `涉及 ${metrics.probe_issue_zones} 个巡查区域。`);
    const actions = {};
    for (const [name, jobs] of [['药品救援', mission.rescues], ['现场处置', mission.responses]]) {
      if (!jobs.length) continue;
      const totals = new Map();
      for (const job of jobs) {
        const status = actionStates[job.status];
        if (status) totals.set(status, (totals.get(status) || 0) + 1);
      }
      actions[name] = Object.fromEntries(totals);
      updateFact(facts, name, 'flow', `${name}进度更新`, [...totals].map(([label, count]) => `${label} ${count} 项`).join(' · '));
    }
    const pursuit = pursuitStates[executor.pursuit?.status];
    if (pursuit) updateFact(facts, 'pursuit', 'flow', `车辆追踪流程${pursuit}`);
    const now = Date.now();
    const taskSignature = mission.tasks.map(task => task.status).join(',');
    const settled = taskSignature !== session.taskSignature && mission.tasks.every(task => finalTask.has(task.status));
    session.taskSignature = taskSignature;
    if (metrics.photos && (settled || now - session.lastPhotoLog >= modelInterval) &&
        (metrics.photos !== session.loggedPhotos || metrics.analyzed !== session.loggedAnalyzed)) {
      session.lastPhotoLog = now;
      session.loggedPhotos = metrics.photos;
      session.loggedAnalyzed = metrics.analyzed;
      updateFact(facts, 'photos', 'vision', '巡查影像进度更新',
        `已拍摄 ${metrics.photos} 张，已分析 ${metrics.analyzed} 张，其中 ${metrics.abnormal_photos} 张包含异常线索。`);
    }
    const changes = [];
    for (const [key, event] of facts) {
      const signature = JSON.stringify(event);
      if (session.facts.get(key) === signature) continue;
      session.facts.set(key, signature);
      changes.push(event);
    }
    for (const [key, title] of [['vehicles', '当前已无待核验或已确认车辆'],
      ['vision-issues', '照片分析复核提示已清除'], ['probe-issues', '已恢复读取路口监测记录']]) {
      if (!facts.has(key) && session.facts.delete(key)) changes.push({kind: 'attention', title, detail: ''});
    }
    session.metrics = {...metrics, monitoring: executor.trafficActive};
    session.current = {phase: session.phase, metrics: session.metrics, zones, actions, pursuit};
    this.record(session, changes, session.current);
  }

  tick() {
    const session = this.session;
    if (!session?.missionId || ['resetting', 'reset_failed'].includes(session.phase)) return;
    const executor = this.getExecutor();
    if (executor.mission?.mission_id !== session.missionId) return;
    try {
      this.capture(session, executor);
      session.dataError = false;
    } catch (error) {
      if (!session.dataError) console.warn(`无法读取任务战报执行状态 (${error.name})`);
      session.dataError = true;
      return;
    }
    const now = Date.now();
    if (session.running || session.logError || session.report?.revision === session.revision ||
        now < session.retryAt || now - session.lastAttempt < modelInterval) return;
    void this.summarize(session);
  }

  async summarize(session) {
    const revision = session.revision;
    session.running = true;
    session.lastAttempt = Date.now();
    try {
      const previous = session.report;
      const previous_update = previous && {revision: previous.revision, summary: previous.summary, next_focus: previous.next_focus};
      const report = await this.runtime.request('mission_briefing', {session_id: session.id, revision, previous_update},
        {channel: 'briefing', timeoutMs: 135000});
      if (this.session !== session || session.phase === 'resetting' || session.phase === 'reset_failed') return;
      if (previous && (previous.summary !== report.summary || previous.next_focus !== report.next_focus))
        session.updates = [previous, ...session.updates].slice(0, 5);
      session.report = {...report, revision, at: Date.now()};
      session.failures = 0;
      session.retryAt = 0;
    } catch {
      if (this.session !== session || session.phase === 'resetting' || session.phase === 'reset_failed') return;
      session.failures++;
      session.retryAt = Date.now() + Math.min(120000, 15000 * 2 ** session.failures);
    } finally {
      session.running = false;
    }
  }

  snapshot() {
    const session = this.session;
    if (!session) return {phase: 'idle'};
    const stale = !!session.report && session.report.revision < session.revision;
    const status = session.logError ? 'log_error' : session.dataError ? 'data_error' :
      session.running ? 'generating' : session.failures ? 'retrying' :
      stale ? 'pending' : session.report ? 'ready' : 'waiting';
    return {session_id: session.id, phase: session.phase, status, stale,
      metrics: session.metrics, report: session.report, updates: session.updates, events: session.events};
  }

  clear() {
    this.session = null;
    this.runtime.cancel('briefing');
  }

  close() {
    clearInterval(this.timer);
    this.timer = null;
    this.clear();
  }
}

module.exports = MissionFeed;
