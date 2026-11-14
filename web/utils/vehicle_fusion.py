"""单街区演示车辆融合：探头负责身份，高空 YOLO 负责沿途测速。"""

from collections import Counter
import json
import math
from pathlib import Path

from Scene.probe.get_probe_records import _read_probe_records

WINDOW_S = 60.0
SAMPLE_INTERVAL_S = 3.0
ROUTE_GATE_M = 14.0


def route_distance(point, route):
    return min(segment_distance(point, a, b) for a, b in zip(route, route[1:]))


def segment_distance(point, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = dx * dx + dy * dy
    fraction = max(0.0, min(1.0, ((point[0] - a[0]) * dx + (point[1] - a[1]) * dy) / length)) if length else 0.0
    return math.hypot(point[0] - a[0] - fraction * dx, point[1] - a[1] - fraction * dy)


def wrong_way(row, route):
    # 通行方向 approaching/departing 只表示相对镜头的方向，不等同于逆行。
    # 探头给出的车头方向与最近外围车道方向相反时，才认作逆行。
    a, b = min(zip(route, route[1:]), key=lambda pair: segment_distance(row['xy_m'], *pair))
    heading = math.radians(row['heading_deg'])
    return math.cos(heading) * (b[0] - a[0]) + math.sin(heading) * (b[1] - a[1]) < 0


class VehicleFusion:
    def __init__(self, zone_id, log_path, speed_limit_kmh):
        root = Path(__file__).resolve().parents[2]
        self.events_dir = Path(log_path).parent.parent / 'TrafficProbes'
        self.zone_id = zone_id
        self.probes = json.loads((root / 'Map/objects/traffic_probes.json').read_text(encoding='utf-8-sig'))['probes']
        self.limit = speed_limit_kmh
        self.cursor = None
        self.started_at = None
        self.last_time = None
        self.targets = {}
        self.probe_session = None
        self.pending_records = []

    def step(self, context, vehicles, alerts, observation_time=None):
        now = context['simulation_time_s']
        if self.last_time is not None and now < self.last_time:
            self.targets.clear()
            self.cursor = self.started_at = None
            self.pending_records.clear()
        self.last_time = now
        if self.started_at is None:
            self.started_at = now
        demos = {item['id']: item for item in context['targets'] if item['zone_id'] == self.zone_id}
        self.targets = {key: value for key, value in self.targets.items() if key in demos}
        for key in demos:
            self.targets.setdefault(key, {'target_id': key, 'plate': None, 'events': [], 'recent_events': [],
                                         'confirmed_at': None, 'confirmed_evidence': [],
                                         'position_m': None, 'last_seen_s': None, 'trail': []})
        error = None
        try:
            page = _read_probe_records(events_dir=self.events_dir, cursor=self.cursor,
                                       start=self.started_at)
            if self.probe_session is not None and page['session'] != self.probe_session:
                # 新 Play 场次的历史不能与上一场混合。
                self.targets.clear()
                self.cursor = self.started_at = None
                self.pending_records.clear()
                self.probe_session = page['session']
                return self.snapshot(now, demos, '探头场次已变化，正在重新关联')
            self.cursor, self.probe_session = page['cursor'], page['session']
            rows = self.pending_records + page['records']
            # UE查询时钟后探头仍可能写入；稍晚的记录留到下一帧，不能推进游标后丢弃。
            self.pending_records = [row for row in rows if row['t'] > now]
            for row in rows:
                key = row.get('truth_id')
                if key not in demos or row['t'] > now or now - row['t'] > WINDOW_S:
                    continue
                state, demo = self.targets[key], demos[key]
                if route_distance(row['xy_m'], demo['route']) > 65:
                    continue
                if row.get('read_ok') and row.get('plate'):
                    state['plate'] = row['plate']
                violations = []
                if row['speed_kmh'] > self.limit:
                    violations.append('speeding')
                if wrong_way(row, demo['route']):
                    violations.append('wrong_way')
                if violations:
                    self.add_event(state, {'source': 'probe', 't': row['t'], 'probe_id': row['probe'],
                                           'position_m': row['xy_m'], 'plate': row.get('plate'),
                                           'speed_kmh': row['speed_kmh'], 'violations': violations,
                                           'direction': row['direction']})
        except (OSError, ValueError) as failure:
            error = str(failure)
        for key, demo in demos.items():
            state = self.targets[key]
            # 仅在所属闭环附近选取唯一超速目标，不使用 Actor 的真实速度或驾驶模式。
            candidates = [v for v in vehicles if v['overspeed']
                          and route_distance(v['position_m'], demo['route']) <= ROUTE_GATE_M]
            if len(candidates) != 1:
                continue
            vehicle = candidates[0]
            stamp = observation_time if observation_time is not None else now
            state.update(position_m=vehicle['position_m'], last_seen_s=stamp,
                         speed_kmh=vehicle['speed_kmh'], track_id=vehicle['track_id'])
            state['trail'].append(vehicle['position_m'][:2])
            state['trail'] = state['trail'][-80:]
            evidence = next((a for a in alerts if a['track_id'] == vehicle['track_id']), None)
            if evidence:
                state['uav_event_id'] = evidence['event_id']
            self.add_event(state, {'source': 'uav', 't': stamp, 'track_id': vehicle['track_id'],
                                   'position_m': vehicle['position_m'], 'speed_kmh': vehicle['speed_kmh'],
                                   'violations': ['speeding'], 'uav_event_id': state.get('uav_event_id')})
        return self.snapshot(now, demos, error)

    @staticmethod
    def add_event(state, event):
        previous = next((e for e in reversed(state['events']) if e['source'] == event['source']), None)
        if previous and event['t'] - previous['t'] < SAMPLE_INTERVAL_S:
            return
        state['events'].append(event)
        state['events'].sort(key=lambda e: e['t'])
        state['recent_events'] = sorted(state['recent_events'] + [event], key=lambda e: e['t'])[-2:]

    def snapshot(self, now, demos, error):
        records = []
        for key, state in self.targets.items():
            events = [event for event in state['events'] if now - event['t'] <= WINDOW_S]
            state['events'] = events
            counts = Counter(event['source'] for event in events)
            if state['confirmed_at'] is None and counts['uav'] and counts['probe']:
                state['confirmed_at'] = now
                state['confirmed_evidence'] = list(events)
            status = 'confirmed' if state['confirmed_at'] is not None else 'observing' if events else 'waiting'
            records.append({**state, 'plate': state['plate'] if status == 'confirmed' else None,
                            'status': status, 'zone_id': self.zone_id,
                            'source_counts': {'uav': counts['uav'], 'probe': counts['probe']},
                            'local_probes': [p for p in self.probes
                                             if route_distance(p['xy_m'], demos[key]['route']) <= 65]})
        return {'window_s': WINDOW_S, 'simulation_time_s': now, 'targets': records,
                'probe_session': self.probe_session, 'error': error,
                'association': '单街区闭环关联 · 探头 truth_id 辅助归属'}
