'use strict';

const {randomUUID} = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const BriefingHistory = require('./briefing-history.cjs');

const taskLabels = {queued: '等待派发', running: '正在准备或前往巡查区域', analyzing: '已返航，照片分析中',
  completed: '巡查与分析完成', failed: '执行中断', skipped: '未执行'};
const monitorLabels = {running: '车辆监测中', paused: '监测暂停', failed: '车辆监测中断',
  stopped: '车辆监测已停止'};
const actionLabels = {queued: '等待执行', dispatching: '正在准备', delivering: '正在送药', responding: '现场处理中',
  returning: '无人机正在返航', delivered: '药品投递并返航完成', resolved: '现场处理并返航完成',
  failed: '执行失败', uncertain: '结果待确认', stopped: '操作已停止'};
const finalTask = new Set(['completed', 'failed', 'skipped']);
const finalPursuit = new Set(['complete', 'failed', 'stopped']);
const suspended = new Set(['resetting', 'reset_failed', 'reset', 'cancelled']);
const violations = {speeding: '超速', wrong_way: '逆向行驶'};
const position = value => Array.isArray(value) && value.length >= 2 && value.slice(0, 2).every(Number.isFinite)
  ? value.slice(0, 2) : null;
const alertKey = (task, id) => task + ':' + id;
const hasSpeeding = events => events?.some(event => event.violations?.includes('speeding'));

class MissionFeed {
  constructor({root, runtime, getExecutor, getMonitoredTargets}) {
    this.catalogPath = path.join(root, 'Map', 'zones', 'functional_zones.json');
    this.directory = path.join(root, 'output', 'mission-briefings');
    this.history = new BriefingHistory(root);
    this.runtime = runtime;
    this.getExecutor = getExecutor;
    this.getMonitoredTargets = getMonitoredTargets;
    this.session = null;
    this.zones = null;
  }

  begin(instruction = '', fleet = null) {
    this.clear();
    const session = {id: randomUUID().replaceAll('-', ''), missionId: null, phase: 'planning', instruction,
      revision: 0, eventId: 0, events: [], pending: new Map(), cases: new Map(), facts: new Map(),
      report: null, failures: 0, running: false, timer: null, logError: false, dataError: false,
      reports: [], reportDirectories: [], savedReports: new Map(), historyError: false,
      pursuit: null, escapes: new Map(), linkedAlerts: new Set(), aliases: new Map(), confirmedSpeeding: null};
    this.session = session;
    this.planningFleet(session, fleet);
    if (instruction) this.stage('planning', '收到巡查指令', instruction);
    return session;
  }

  planningFleet(session, fleet) {
    if (this.session !== session || !Array.isArray(fleet)) return;
    session.initialFleet = {online: fleet.length, available: fleet.filter(drone => drone.available === true).length};
  }

  stage(phase, title, detail = '', meta = '') {
    const session = this.session;
    if (!session) return;
    session.phase = phase;
    session.current = {...session.current, phase};
    if (suspended.has(phase)) {
      clearTimeout(session.timer);
      session.timer = null;
      session.pending.clear();
      session.report = null;
      this.runtime.cancel('briefing');
    }
    this.record(session, [{kind: 'flow', title, detail, meta}]);
  }

  bind(session, missionId, tasks = []) {
    if (this.session !== session) return;
    session.missionId = missionId;
    session.reportDirectories = this.history.regionDirectories(tasks);
    this.history.save(session);
    this.refresh();
  }

  saveOutput(session, report) {
    const output = {...report, sequence: session.reports.length + 1, at: Date.now(),
      session_id: session.id, mission_id: session.missionId, instruction: session.instruction};
    session.reports.push(output);
    this.history.save(session);
    return output;
  }

  record(session, changes) {
    if (!changes.length) return;
    const at = Date.now();
    const events = changes.map(event => ({...event, id: ++session.eventId, at}));
    for (const event of events) {
      if (event.subject && session.cases.has(event.subject)) {
        const entry = session.cases.get(event.subject);
        entry.at = at;
        entry.event_id = event.id;
        entry.history = [...entry.history, event].slice(-12);
      }
      if (!suspended.has(session.phase)) session.pending.set(event.id, event);
    }
    session.events = [...session.events, ...events].slice(-60);
    session.revision++;
    if (!session.logError) {
      try {
        if (session.revision === 1) fs.mkdirSync(this.directory, {recursive: true});
        const filename = path.join(this.directory, session.id + '.jsonl');
        fs.appendFileSync(filename, JSON.stringify({mission_id: session.missionId, revision: session.revision,
          current: this.context(session), events}) + '\n', 'utf8');
      } catch (error) {
        session.logError = true;
        console.warn('无法写入巡查记录 (' + (error.code || error.name) + ')');
      }
    }
    if (session.failures >= 2) session.failures = 0;
    this.schedule(session);
  }

  zoneName(id) {
    if (!id) return '';
    if (!this.zones) {
      try {
        const data = JSON.parse(fs.readFileSync(this.catalogPath, 'utf8').replace(/^\uFEFF/, ''));
        this.zones = new Map(data.features.map(zone => [zone.id, zone.properties.name]));
      } catch { this.zones = new Map(); }
    }
    return this.zones.get(id) || id;
  }

  refresh() {
    const executor = this.getExecutor();
    const session = this.session || (executor.pursuit && this.begin());
    if (!session || suspended.has(session.phase)) return;
    if (!session.instruction && !session.missionId) session.phase = 'observation';
    const mission = executor.mission?.mission_id === session.missionId ? executor.mission : null;
    try {
      this.capture(session, mission, executor);
      session.dataError = false;
    } catch (error) {
      if (!session.dataError) console.warn('无法读取巡查事件 (' + error.name + ')');
      session.dataError = true;
    }
  }

  capture(session, mission, executor) {
    const changes = [];
    const fact = (key, value, kind, title, detail = '', zone = null, meta = '') => {
      if (session.facts.get(key) === value) return;
      session.facts.set(key, value);
      changes.push({kind, title, detail, zone, meta});
    };
    if (mission) {
      session.phase = executor.uncertain ? 'uncertain' : mission.status;
      const zones = mission.tasks.map(task => {
        const zone = task.target.zone_id, name = this.zoneName(zone);
        const assigned = task.status === 'running' && task.drone_id && task.monitor.drone_id;
        const search = task.status === 'running' && task.search_started ? '正在搜索' :
          assigned ? '无人机已分配' : taskLabels[task.status];
        const aircraft = [task.drone_id && `巡查机 ${task.drone_id}`,
          task.monitor.drone_id && `监控机 ${task.monitor.drone_id}`].filter(Boolean).join(' · ');
        if (assigned || task.search_started && task.status === 'running' || task.status === 'analyzing' || finalTask.has(task.status))
          fact('search:' + task.task_id, search, task.status === 'failed' ? 'issue' : 'flow',
            name + ' · ' + search, task.error, zone, aircraft);
        const monitor = task.monitor.traffic.status;
        const monitorLabel = monitorLabels[monitor];
        // 临时校时和稳定过程不改变上次已通报的监测状态。
        if (monitorLabel && (monitor !== 'stopped' || session.facts.has('monitor:' + task.task_id)))
          fact('monitor:' + task.task_id, monitorLabel, monitor === 'failed' ? 'issue' : 'flow',
            name + ' · ' + monitorLabel, task.monitor.traffic.error, zone, task.monitor.drone_id ? `监控机 ${task.monitor.drone_id}` : '');
        const issues = task.vision.status === 'failed' || task.vision.status === 'completed_with_errors';
        if (issues) fact('vision:' + task.task_id, true, 'issue', name + '有图像待复核',
          '部分照片的识别或定位未完成。', zone);
        return {zone, name, search, monitor: monitorLabel || '', search_drone: task.drone_id, monitor_drone: task.monitor.drone_id};
      });
      if (['uncertain', 'completed', 'failed'].includes(session.phase))
        fact('mission', session.phase, session.phase === 'completed' ? 'flow' : 'issue',
          {uncertain: '部分执行状态待确认', completed: '巡查与计划内处理已结束', failed: '本轮巡查有未完成事项'}[session.phase],
          executor.trafficActive ? '车辆监测仍在运行。' : '');
      session.current = {phase: session.phase, zones, monitoring: executor.trafficActive,
        photos: mission.tasks.reduce((sum, task) => sum + task.vision.total, 0),
        analyzed: mission.tasks.reduce((sum, task) => sum + task.vision.analyzed, 0)};
      this.people(session, mission, executor.targets, changes);
    }
    this.vehicles(session, mission, session.missionId || !session.instruction ? executor.pursuit : null, changes);
    this.record(session, changes);
  }

  saveCase(session, changes, entry, signature, eventTitle, detail) {
    const previous = session.cases.get(entry.id);
    const changed = previous?.signature !== signature;
    if (!changed) return;
    session.cases.set(entry.id, {...entry, signature, at: previous?.at, history: previous?.history || []});
    changes.push({kind: entry.kind, subject: entry.id, title: eventTitle, detail, zone: entry.zone});
  }

  people(session, mission, targets, changes) {
    const actions = new Map([...mission.rescues, ...mission.responses].map(action => [action.target_id, action]));
    const byTarget = new Map(targets.map(target => [target.target_id, target]));
    const publish = (id, record, detection, target = null) => {
      const action = actions.get(target?.target_id);
      const actionStatus = action?.status === 'responding' && action.phase === 'returning' ? 'returning' : action?.status;
      const status = actionLabels[actionStatus] || (target ? '已关联仿真人物位置' : '身份待核验');
      const title = !target ? '人员线索待核验' :
        target.status === 'patient' ? '发现医疗救助对象' : '发现持刀人员';
      const zone = record.zone_id, where = this.zoneName(zone);
      const point = position(target?.position_m);
      const description = target ? detection.label || record.description :
        '照片中发现人员异常线索，需要核验画面中的人物身份。';
      this.saveCase(session, changes, {id, kind: 'person', title, status, zone, where, position: point,
        description, person_id: target?.person_id, location_error: detection.location_error,
        action: action && {label: action.rescue_id ? '药品救援' : '现场处置',
          status: actionStatus, drone: action.drone_id, error: action.error},
        image: {url: record.image_url, name: record.image, bbox: detection.bbox, at: record.observed_at,
          drone: record.drone_id}}, JSON.stringify([title, status, record.image, description, point,
          action?.drone_id, action?.error, detection.location_error]),
      where + ' · ' + (action ? title + ' · ' + status : title), [description, action?.error || detection.location_error].filter(Boolean).join('。'));
    };
    for (const target of targets) {
      const record = target.first_observation;
      const detection = record.detections[0];
      publish('person:' + (target.evidence_id || target.target_id), record, detection, target);
    }
    for (const task of mission.tasks) {
      for (const record of task.vision.anomalies) {
        for (const [index, detection] of record.detections.entries()) {
          const key = detection.evidence_id || record.task_id + ':' + record.image + ':' + index;
          const id = 'person:' + key, target = byTarget.get(detection.target_id);
          if (target) {
            if (key !== (target.evidence_id || target.target_id) && session.cases.delete(id))
              session.aliases.set(id, 'person:' + (target.evidence_id || target.target_id));
            continue;
          }
          if (record.analysis_source === 'simulation') continue;
          publish(id, record, detection);
        }
      }
    }
  }

  pursuitState(session, pursuit) {
    if (!pursuit) return null;
    if (session.pursuit?.id !== pursuit.pursuit_id)
      session.pursuit = {id: pursuit.pursuit_id, visible: true, candidate: true, since: Date.now()};
    const state = session.pursuit;
    if (pursuit.status === 'tracking' && typeof pursuit.target_visible === 'boolean') {
      if (state.candidate !== pursuit.target_visible) {
        state.candidate = pursuit.target_visible;
        state.since = Date.now();
      }
      if (Date.now() - state.since >= (state.candidate ? 650 : 2200)) state.visible = state.candidate;
    }
    const labels = {preparing: '追踪准备中', tracking: state.visible ? '追踪中' : '暂失画面',
      returning: '无人机正在返航', complete: '追踪结束', failed: '追踪未完成', stopped: '追踪已停止'};
    let title = labels[pursuit.status] || '追踪状态待确认';
    if (pursuit.status === 'tracking' && pursuit.target?.intercept_state === 'braking') title = '车辆正在减速';
    if (pursuit.outcome === 'intercepted') title = pursuit.status === 'returning' ? '车辆已停，机队返航中' : '车辆已停';
    return {title, status: pursuit.status, id: pursuit.pursuit_id, vehicle_id: pursuit.vehicle_id,
      active: !finalPursuit.has(pursuit.status), error: title === '追踪未完成' ? pursuit.error : null,
      position: position(pursuit.target?.position_m)};
  }

  vehicles(session, mission, pursuit, changes) {
    const tasks = mission?.tasks || [];
    const alerts = new Map(tasks.flatMap(task => task.monitor.traffic.alerts)
      .map(alert => [alertKey(alert.task_id, alert.event_id), alert]));
    const records = this.getMonitoredTargets(tasks);
    // 每轮任务只通报一次超速确认，场景和追踪状态继续使用原车辆卡。
    const locked = session.confirmedSpeeding;
    const firstConfirmation = locked ? null : records
      .filter(record => record.status === 'confirmed' && hasSpeeding(record.confirmed_evidence))
      .sort((a, b) => (a.confirmed_at ?? Infinity) - (b.confirmed_at ?? Infinity))[0];
    const tracked = this.pursuitState(session, pursuit);
    const seen = new Set();
    const publish = (id, entry, phase) => {
      const summary = [...new Set(entry.events.flatMap(event => event.violations || []))].sort()
        .map(type => violations[type]).filter(Boolean).join('、');
      const details = [summary, ...entry.events.map(event =>
        (event.source === 'uav' ? '无人机' : '路口') + ' ' + Math.round(event.speed_kmh) + ' km/h'),
      entry.pursuit?.error].filter(Boolean).join(' · ');
      const completedSpeeding = entry.record?.status === 'confirmed' && hasSpeeding(entry.events);
      this.saveCase(session, changes, {...entry, id, kind: 'vehicle'}, JSON.stringify([entry.status, entry.title,
        [...new Set(entry.events.map(event => event.source))].sort(), summary, completedSpeeding ? null : entry.alert?.image_url,
        entry.pursuit?.id, entry.pursuit?.title, entry.escaped_at]),
      [entry.title, phase].filter(Boolean).join(' · '), details);
      seen.add(id);
    };
    for (const record of records) {
      const events = (record.status === 'confirmed' ? record.confirmed_evidence : record.events) || [];
      if (!events.length) continue;
      const id = 'vehicle:' + record.target_id;
      if (id === locked?.id || (locked || firstConfirmation) && record !== firstConfirmation && hasSpeeding(events)) continue;
      const uav = events.find(event => event.source === 'uav');
      const proofId = uav && (uav.uav_event_id || record.uav_event_id);
      const proofKey = proofId && alertKey(record.task_id, proofId);
      const alert = alerts.get(proofKey) || session.cases.get(id)?.alert;
      for (const event of events) {
        if (event.source !== 'uav') continue;
        const linked = alertKey(record.task_id, event.uav_event_id || record.uav_event_id);
        if (alerts.has(linked)) {
          session.linkedAlerts.add(linked);
          if (session.cases.delete('capture:' + linked)) session.aliases.set('capture:' + linked, id);
        }
      }
      const previous = session.cases.get(id);
      const latestPursuit = tracked?.vehicle_id === record.target_id ? tracked : previous?.pursuit;
      const escapedAt = session.escapes.get(record.target_id)?.at;
      const status = record.status === 'confirmed' ? '已确认违规' : '待核验车辆线索';
      const phase = record === firstConfirmation ? status :
        latestPursuit && latestPursuit.title !== previous?.pursuit?.title ? latestPursuit.title :
        escapedAt && escapedAt !== previous?.escaped_at ? '已启动逃逸演示' : status;
      const evidence = events.filter((event, index) => events.findIndex(item => item.source === event.source) === index);
      publish(id, {title: record.plate || '车辆线索', status, zone: record.zone_id, where: this.zoneName(record.zone_id),
        position: position(record.position_m) || position(evidence.at(-1)?.position_m),
        events, alert, pursuit: latestPursuit, escaped_at: escapedAt, target_id: record.target_id,
        record: {target_id: record.target_id, zone_id: record.zone_id, plate: record.plate, status: record.status,
          drone_id: record.drone_id, local_probes: record.local_probes || [], trail: (record.trail || []).slice(-32),
          position_m: record.position_m, events: evidence, confirmed_at: record.confirmed_at}},
      phase);
      if (record === firstConfirmation) session.confirmedSpeeding = {id, proofKey};
    }
    if (firstConfirmation) {
      const discarded = new Set();
      for (const [id, entry] of session.cases) {
        if (id === session.confirmedSpeeding.id || entry.pursuit || entry.escaped_at || !hasSpeeding(entry.events)) continue;
        session.cases.delete(id);
        discarded.add(id);
      }
      const retained = event => !discarded.has(session.aliases.get(event.subject) || event.subject);
      session.events = session.events.filter(retained);
      for (const [id, event] of session.pending) if (!retained(event)) session.pending.delete(id);
    }
    if (locked?.proofKey && alerts.has(locked.proofKey)) {
      const confirmed = session.cases.get(locked.id);
      confirmed.alert ||= alerts.get(locked.proofKey);
    }
    for (const [key, alert] of session.confirmedSpeeding ? [] : alerts) {
      if (session.linkedAlerts.has(key)) continue;
      const event = {source: 'uav', speed_kmh: alert.speed_kmh, t: alert.simulation_time_s,
        violations: ['speeding'], position_m: alert.position_m};
      publish('capture:' + key, {title: '无人机超速线索', status: '等待关联路口记录', zone: alert.zone_id,
        where: this.zoneName(alert.zone_id), position: position(alert.position_m), events: [event], alert,
        record: {drone_id: alert.drone_id, events: [event]}}, '等待核验');
    }
    const vehicleIds = new Set([...session.escapes.keys(), ...(tracked ? [tracked.vehicle_id] : [])]);
    for (const vehicle of vehicleIds) {
      const id = 'vehicle:' + vehicle;
      if (seen.has(id)) continue;
      const previous = session.cases.get(id), escape = session.escapes.get(vehicle);
      const view = tracked?.vehicle_id === vehicle ? tracked : previous?.pursuit;
      const zone = previous?.zone || (tracked?.vehicle_id === vehicle ? pursuit.zone_id : escape?.zone);
      const status = view?.title || '已启动逃逸演示';
      publish(id, {...previous, title: previous?.title || '场景车辆', status: previous?.status || status, zone,
        where: this.zoneName(zone), target_id: vehicle, position: view?.position || previous?.position,
        events: previous?.events || [], pursuit: view, escaped_at: escape?.at}, status);
    }
    for (const [id, entry] of session.cases) {
      if (seen.has(id) || entry.record?.status !== 'observing') continue;
      if (session.confirmedSpeeding && hasSpeeding(entry.events)) continue;
      publish(id, {...entry, status: '当前观测已过期，待补证', record: {...entry.record, status: 'expired'}}, '等待新观测');
    }
  }

  escape(target) {
    const session = this.session || this.begin();
    if (suspended.has(session.phase)) return;
    if (!session.instruction && !session.missionId) session.phase = 'observation';
    if (!target.id) return;
    session.escapes.set(target.id, {zone: target.zone_id, at: Date.now()});
    this.refresh();
  }

  context(session) {
    const cases = [...session.cases.values()].sort((a, b) => b.event_id - a.event_id).slice(0, 16);
    return {instruction: session.instruction, initial_fleet: session.initialFleet, ...session.current, phase: session.phase,
      cases: cases.map(entry => ({id: entry.id, title: entry.title, area: entry.where, status: entry.status,
        position_xy_m: entry.position, note: entry.description, location_error: entry.location_error,
        action: entry.action, pursuit: entry.pursuit?.title, escape_started: !!entry.escaped_at,
        has_image: !!(entry.image?.url || entry.alert?.image_url),
        sources: entry.record?.events.map(event => ({source: event.source, speed_kmh: event.speed_kmh,
          violations: event.violations?.map(type => violations[type]).filter(Boolean), time: event.t}))}))};
  }

  schedule(session, delay = 350) {
    if (session.running || session.timer || !session.pending.size || session.failures >= 2) return;
    session.timer = setTimeout(() => {
      session.timer = null;
      void this.summarize(session);
    }, delay);
    session.timer.unref();
  }

  async summarize(session) {
    const revision = session.revision;
    const through = session.eventId;
    const pending = [...session.pending.values()];
    const triggers = [...pending.filter(event => !event.subject), ...pending.filter(event => event.subject)]
      .slice(-20).sort((a, b) => a.id - b.id);
    const events = [...new Map([...session.events.slice(-12), ...triggers].map(event => [event.id, event])).values()];
    session.running = true;
    let retry = false;
    try {
      const previous = session.report;
      const report = await this.runtime.request('mission_briefing', {
        current: this.context(session), events, trigger_ids: triggers.map(event => event.id),
        previous_update: previous && {summary: previous.summary, next_focus: previous.next_focus}
      }, {channel: 'briefing', timeoutMs: 50000});
      if (this.session !== session || suspended.has(session.phase)) return;
      session.failures = 0;
      if (session.revision !== revision) return;
      for (const id of session.pending.keys()) if (id <= through) session.pending.delete(id);
      for (const [id, note] of Object.entries(report.case_notes || {})) {
        const entry = session.cases.get(id);
        if (entry) entry.note = note;
      }
      session.report = this.saveOutput(session, {...report, revision, kind: 'briefing'});
    } catch {
      if (this.session !== session || suspended.has(session.phase)) return;
      if (session.revision === revision) {
        session.failures++;
        retry = true;
      }
    } finally {
      session.running = false;
      if (this.session === session && !suspended.has(session.phase)) this.schedule(session, retry ? 2500 : 250);
    }
  }

  snapshot() {
    const session = this.session;
    if (!session) return {phase: 'idle'};
    const status = session.dataError ? 'data_error' : session.failures >= 2 ? 'unavailable' :
      session.running || session.timer ? 'generating' : session.report ? 'ready' : 'waiting';
    return {session_id: session.id, phase: session.phase, status, log_error: session.logError,
      report_count: session.reports.length, history_error: session.historyError,
      current: session.current, report: session.report?.revision === session.revision ? session.report : null,
      events: session.events, aliases: Object.fromEntries(session.aliases),
      cases: [...session.cases.values()].map(({signature, ...entry}) => ({...entry, pursuit: entry.pursuit &&
        {...entry.pursuit, latest: entry.pursuit.id === session.pursuit?.id}}))};
  }

  clear() {
    clearTimeout(this.session?.timer);
    if (this.session?.historyError) this.history.save(this.session);
    this.session = null;
    this.runtime.cancel('briefing');
  }

  close() { this.clear(); }
}

module.exports = MissionFeed;
