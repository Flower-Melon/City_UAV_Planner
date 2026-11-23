"""无人机飞行执行与共用任务锁；调用者负责持有对应无人机的锁。"""

import errno
import hashlib
import math
import os
from pathlib import Path
import tempfile
import threading
import time

from .airsim_state import uses_pose_flight, vector
from ..utils.plan_flight_path import _coordinates, _positive, plan_flight_path

if os.name == "nt":
    import msvcrt
else:
    import fcntl

class _TaskLock:
    """同一工程的线程/进程共享任务锁，进程退出后由系统释放。"""

    def __init__(self, name):
        key = os.path.normcase(str(Path(__file__).resolve().parents[2])) + ":" + name
        self.path = Path(tempfile.gettempdir()) / ("city-uav-" + hashlib.sha256(key.encode()).hexdigest() + ".lock")
        self.thread_lock = threading.Lock()
        self.stream = None

    def acquire(self, blocking=True, timeout=-1):
        deadline = time.monotonic() + timeout if timeout >= 0 else None
        if not self.thread_lock.acquire(blocking, timeout):
            return False
        stream = None
        try:
            stream = self.path.open("a+b")
            if self.path.stat().st_size == 0:
                stream.write(b"0")
                stream.flush()
            while True:
                try:
                    stream.seek(0)
                    if os.name == "nt":
                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self.stream = stream
                    return True
                except OSError as error:
                    if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                        raise
                    if not blocking or deadline is not None and time.monotonic() >= deadline:
                        stream.close()
                        self.thread_lock.release()
                        return False
                    time.sleep(0.02)
        except BaseException:
            if stream is not None:
                stream.close()
            self.thread_lock.release()
            raise

    def release(self):
        if self.stream is None:
            raise RuntimeError("释放了未持有的任务锁")
        try:
            self.stream.close()
        finally:
            self.stream = None
            self.thread_lock.release()

    def locked(self):
        if not self.acquire(blocking=False):
            return True
        self.release()
        return False

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *args):
        self.release()


_LOCKS = {}
_REGISTRY_LOCK = threading.Lock()


def vehicle_lock(name):
    with _REGISTRY_LOCK:
        if name not in _LOCKS:
            _LOCKS[name] = _TaskLock(name)
        return _LOCKS[name]


_AXES = ("x_val", "y_val", "z_val")
_ZERO = dict.fromkeys(_AXES, 0.0)
FLIGHT_PERIOD = 1 / 60
_MAX_FRAME_TIME = 0.05
_MAX_STEP_M = 1.5
_YAW_SPEED = math.radians(120)
_STATE_POLL = 0.1
ROUTE_TOLERANCE_M = 10.0


def write_pose(client, name, pose, *, ignore_collision=False):
    """写入局部 NED 位姿，由 AirSim 物理线程按自身节拍更新传感器。"""
    # simSetKinematics 会在 RPC 线程更新传感器；ExternalPhysicsEngine
    # 也会更新同一个队列。simSetVehiclePose 不触发这次额外更新。
    client.call("simSetVehiclePose", pose, ignore_collision, name)


def pose_yaw(pose):
    q = pose["orientation"]
    w, x, y, z = (q[k + "_val"] for k in "wxyz")
    return math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z))


def wait_for_flight_tick(started):
    time.sleep(max(0, FLIGHT_PERIOD - (time.monotonic() - started)))


class FlightControl:
    """持有任务锁时使用。沿给定的世界坐标航点连续推进位姿。"""

    route_tolerance = ROUTE_TOLERANCE_M

    def __init__(self, client, frame, name, origin):
        self.client, self.frame, self.name, self.origin = client, frame, name, origin
        self.controlling = False
        self.position = None
        self.yaw = 0.0
        self.diagnostics = {}
        self.collision_timestamp = None
        self.next_state_check = 0.0
        self.last_pose_at = 0.0

    def prepare(self):
        """从当前 UE 位姿接管。外部物理模式让悬停和进程退出后的位姿稳定。"""
        if not uses_pose_flight(self.frame.settings):
            raise RuntimeError('演示飞控需要 AirSim 设置 "PhysicsEngineName": "ExternalPhysicsEngine"，请退出并重新进入 Play')
        pose = self.client.call("simGetVehiclePose", self.name)
        self.position = self._world(vector(pose["position"]))
        self.yaw = pose_yaw(pose)
        self.collision_timestamp = self.client.call("simGetCollisionInfo", self.name).get("time_stamp")
        self.client.call("cancelLastTask", self.name)
        self.client.call("enableApiControl", True, self.name)
        self._check_control()
        self.last_pose_at = time.monotonic() - FLIGHT_PERIOD
        self.controlling = True

    def _ned(self, point):
        x, y, z = self.origin
        scale = self.frame.scale
        return ((point[0] - x) / scale, (point[1] - y) / scale, (z - point[2]) / scale)

    def _world(self, ned):
        x, y, z = self.origin
        scale = self.frame.scale
        return (x + ned[0] * scale, y + ned[1] * scale, z - ned[2] * scale)

    def _collision(self):
        collision = self.client.call("simGetCollisionInfo", self.name)
        if (collision.get("has_collided") and collision.get("time_stamp") != self.collision_timestamp):
            self.collision_timestamp = collision.get("time_stamp")
            self.stop()
            raise RuntimeError(f"{self.name} 检测到碰撞，飞行停止")

    def _check_control(self):
        if not self.client.call("isApiControlEnabled", self.name):
            self.controlling = False
            raise RuntimeError(f"{self.name} 的 API 控制权已释放")

    def step(self, target, speed, *, yaw=None):
        """执行一帧，按短时间步长推进；长延迟时最多移动一步。"""
        if not self.controlling:
            raise RuntimeError("执行飞行前必须先 prepare")
        now = time.monotonic()
        if now >= self.next_state_check:
            self._check_control()
            self._collision()
            self.next_state_check = now + _STATE_POLL
        # 上一帧尚未在 UE 生效时等待，避免暂停或卡顿期间堆积位移。
        pose = self.client.call("simGetVehiclePose", self.name)
        actual = self._world(vector(pose["position"]))
        if (math.dist(self.position, actual) > 0.10
                or abs(math.remainder(self.yaw - pose_yaw(pose), math.tau)) > math.radians(1.0)):
            return False
        delta = tuple(b - a for a, b in zip(self.position, target))
        distance = math.hypot(*delta)
        dt = min(_MAX_FRAME_TIME, now - self.last_pose_at)
        limit = min(speed * dt, _MAX_STEP_M)
        arrived = distance <= limit
        position = (tuple(target) if arrived else
                    tuple(a + d * limit / distance for a, d in zip(self.position, delta)))
        desired_yaw = yaw
        if desired_yaw is None and math.hypot(*delta[:2]) > 1e-3:
            desired_yaw = math.atan2(delta[1], delta[0])
        turn = math.remainder(desired_yaw - self.yaw, math.tau) if desired_yaw is not None else 0.0
        angle_limit = _YAW_SPEED * dt
        heading = self.yaw + max(-angle_limit, min(angle_limit, turn))
        if position != self.position or heading != self.yaw:
            write_pose(self.client, self.name, {
                "position": dict(zip(_AXES, self._ned(position))),
                "orientation": dict(_ZERO, w_val=math.cos(heading / 2), z_val=math.sin(heading / 2)),
            })
            self.position, self.yaw = position, heading
            self.last_pose_at = now
        return arrived and (yaw is None or abs(turn) <= angle_limit)

    def move_to(self, point, speed, *, yaw=None, on_progress=None):
        """飞到一个世界坐标点；显式指定航向时同时完成转向。"""
        speed = _positive(speed, "飞行速度")
        while True:
            started = time.monotonic()
            arrived = self.step(point, speed, yaw=yaw)
            if on_progress is not None:
                on_progress(self.position)
            wait_for_flight_tick(started)
            if arrived:
                return

    def climb(self, altitude, speed):
        self.move_to((*self.position[:2], altitude), speed)

    def turn_to(self, yaw):
        self.move_to(self.position, 1.0, yaw=yaw)
        self.confirm_position(self.position, yaw=yaw)

    def hold(self, duration=0.25):
        time.sleep(duration)
        self._check_control()
        self._collision()

    def fly_route(self, route, *, on_progress=None):
        """沿给定折线飞行，按水平里程和世界 XYZ 回调。"""
        points = [_coordinates(p, 4, "航点") for p in route]
        if not points:
            raise ValueError("航线不能为空")
        for point in points:
            _positive(point[3], "航点速度")
        if not self.controlling:
            raise RuntimeError("执行航线前必须先 prepare")
        if math.dist(self.position, points[0][:3]) > self.route_tolerance:
            raise RuntimeError("航线起点偏离当前位置，请重新规划")
        progress = 0.0

        def report(position):
            on_progress(progress + math.dist(start[:2], position[:2]), position)

        try:
            if math.dist(self.position, points[0][:3]) > 1e-4:
                self.move_to(points[0][:3], points[0][3])
            for start, end in zip(points, points[1:]):
                self.move_to(end[:3], end[3], on_progress=report if on_progress is not None else None)
                progress += math.dist(start[:2], end[:2])
            self.confirm_position(points[-1][:3])
        except BaseException:
            try:
                self.stop()
            except Exception:
                pass
            raise

    def confirm_position(self, target, *, yaw=None, landing=False):
        """等待 UE 画面应用最后一帧后核对到达。"""
        deadline = time.monotonic() + 1.0
        target_ned = self._ned(target)
        while True:
            pose = self.client.call("simGetVehiclePose", self.name)
            actual = vector(pose["position"])
            horizontal = math.dist(actual[:2], target_ned[:2]) * self.frame.scale
            vertical = abs(actual[2] - target_ned[2]) * self.frame.scale
            heading_ok = yaw is None or abs(math.remainder(yaw - pose_yaw(pose), math.tau)) <= math.radians(1.0)
            if max(horizontal, vertical) <= 0.15 and heading_ok:
                self._check_control()
                if not landing:
                    self._collision()
                return
            self._collision()
            if time.monotonic() >= deadline:
                self.diagnostics.update(actual_ned=actual, target_ned=target_ned,
                                        horizontal_error_m=horizontal, vertical_error_m=vertical)
                raise RuntimeError(f"{self.name} 位姿未到位，水平误差 {horizontal:.2f} 米，高度误差 {vertical:.2f} 米")
            time.sleep(FLIGHT_PERIOD)

    def return_home(self, max_height, flight_speed):
        """沿规划的航线返航，在出生点上空逐步降落。"""
        home = self.frame.home(self.name)
        ground = self.frame.ground_z(home)
        approach = (*home[:2], max(home[2] + 1.0, ground + min(3.0, max_height)))
        self.fly_route(plan_flight_path(self.position, approach, max_height, flight_speed))
        self.land(home[:2], speed=min(2.0, flight_speed))

    def land(self, home_xy, *, speed=2.0):
        home = self.frame.home(self.name)
        if (math.dist(self.position[:2], home_xy) > self.route_tolerance
                or math.dist(home[:2], home_xy) > self.route_tolerance):
            raise RuntimeError("无人机未到达返航基点上空，无法降落")
        self.move_to((*home[:2], self.position[2]), speed)
        self.move_to(home, speed)
        self.finish_landing(home)

    def finish_landing(self, home, *, yaw=None):
        self.confirm_position(home, yaw=yaw, landing=True)
        self.client.call("enableApiControl", False, self.name)
        self.controlling = False

    def stop(self):
        """停止推进并保留当前位姿；碰撞时停在 UE 实际可达的位置。"""
        if self.controlling:
            pose = self.client.call("simGetVehiclePose", self.name)
            actual = self._world(vector(pose["position"]))
            if math.dist(self.position, actual) > 0.15:
                write_pose(self.client, self.name, pose)
                self.position, self.yaw = actual, pose_yaw(pose)
