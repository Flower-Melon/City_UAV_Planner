"""规划执行层：按无人机排队调用 search；不把飞行控制交给模型。"""

from copy import deepcopy
import math
from numbers import Real
import threading
import uuid

from .functions.catalog import ZoneCatalog
from .functions.commands import Target
from .functions.fleet import read_live_fleet


# 校验本地飞行配置，避免无效参数进入后台后才报错。
# 输入为高度、速度或间隔；返回 float，非法值抛出 ValueError。
def _positive(value, name):
    if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name}必须为正的有限数值")
    return float(value)


class MissionExecutor:
    """同一实例管理一个飞行任务；应用应复用实例，避免重复派发。"""

    # 保存可替换的搜索接口、状态来源和地图；初始化时不连接无人机。
    # 默认真实执行，测试可注入模拟依赖；任务状态仅保存在当前进程内。
    def __init__(self, search_fn=None, fleet_reader=None, catalog=None):
        self.search_fn = search_fn
        self.fleet_reader = fleet_reader or read_live_fleet
        self.catalog = catalog
        self.lock = threading.RLock()
        self.thread = None
        self.stop_event = threading.Event()
        self.done = threading.Event()
        self.mission = None

    # 校验完整规划并复核真实可用状态，再启动后台执行；已有飞行任务时拒绝重复派发。
    # 输入为 ready 规划和明确高度；返回任务快照，同机按序、不同机并行。
    def start(self, plan, flight_height, flight_speed=5.0, capture_interval=3.0):
        settings = {"flight_height": _positive(flight_height, "flight_height"),
                    "flight_speed": _positive(flight_speed, "flight_speed"),
                    "capture_interval": _positive(capture_interval, "capture_interval")}
        with self.lock:
            if self.mission is not None and not self.done.is_set():
                raise RuntimeError("已有飞行任务正在执行，请先完成或取消")
            if not isinstance(plan, dict) or plan.get("status") != "ready" or plan.get("executed") is not False:
                raise ValueError("只接受尚未执行且通过校验的 ready 规划")
            commands = plan.get("commands")
            if not isinstance(commands, list) or not commands:
                raise ValueError("规划必须包含非空 commands")
            catalog = self.catalog if self.catalog is not None else ZoneCatalog()
            tasks, sequences, targets = [], {}, set()
            for command in commands:
                if not isinstance(command, dict) or command.get("action") != "SearchArea":
                    raise ValueError("执行层只支持 SearchArea")
                name, sequence = command.get("drone_id"), command.get("sequence")
                if not isinstance(name, str) or not name.strip() or name != name.strip():
                    raise ValueError("drone_id 必须是实际无人机名称")
                if type(sequence) is not int or sequence < 1 or sequence in sequences.setdefault(name, set()):
                    raise ValueError("同一无人机的 sequence 必须是正整数且不能重复")
                sequences[name].add(sequence)
                raw = command.get("target")
                if not isinstance(raw, dict) or not raw.get("zone_id"):
                    raise ValueError("target 必须包含真实 zone_id")
                target = Target.model_validate({k: raw[k] for k in ("zone_id", "scope", "buffer_m") if k in raw})
                if command.get("zone_id", target.zone_id) != target.zone_id:
                    raise ValueError("zone_id 与 target.zone_id 不一致")
                catalog.query(target, [])
                if target.search_key() in targets:
                    raise ValueError("同一搜索范围不能重复派发")
                targets.add(target.search_key())
                tasks.append({"drone_id": name, "sequence": sequence,
                              "target": target.model_dump(exclude_defaults=True), "status": "queued"})
            if any(numbers != set(range(1, len(numbers)+1)) for numbers in sequences.values()):
                raise ValueError("同一无人机的 sequence 必须从 1 开始连续编号")
            # 仅在接收整批任务时核实空闲状态；后续任务允许从上一任务结束的悬停位置继续。
            live = {drone["name"]: drone for drone in self.fleet_reader()}
            if any(name not in live or live[name].get("available") is not True for name in sequences):
                raise RuntimeError("规划中的无人机已离线或不可用，请重新规划")
            self.stop_event = threading.Event()
            self.done = threading.Event()
            self.mission = {"mission_id": uuid.uuid4().hex, "status": "running", "executed": False,
                            "settings": settings, "tasks": sorted(tasks, key=lambda t: (t["drone_id"], t["sequence"])),
                            "analysis_status": "managed_by_search"}
            self.thread = threading.Thread(target=self._run, name="mission-executor", daemon=False)
            try:
                self.thread.start()
            except Exception as error:
                self.mission.update(status="failed", error_type=type(error).__name__)
                for task in self.mission["tasks"]:
                    task["status"] = "skipped"
                self.done.set()
                raise
            return deepcopy(self.mission)

    # 为每架无人机启动一个顺序队列，等待飞行队列结束后汇总状态。
    # 只等待 search 的飞行返回，不等待它所启动的后台视觉识别。
    def _run(self):
        try:
            threads = []
            for name in sorted({task["drone_id"] for task in self.mission["tasks"]}):
                thread = threading.Thread(target=self._fly, args=(name,), name=f"mission-{name}", daemon=False)
                threads.append(thread)
                thread.start()
            for thread in threads:
                thread.join()
        except Exception as error:
            with self.lock:
                self.mission["error_type"] = type(error).__name__
            self.stop_event.set()
            for thread in threads:
                if thread.ident is not None:
                    thread.join()
        finally:
            with self.lock:
                for task in self.mission["tasks"]:
                    if task["status"] == "queued":
                        task["status"] = "cancelled" if self.stop_event.is_set() else "skipped"
                states = {task["status"] for task in self.mission["tasks"]}
                self.mission["status"] = ("failed" if "failed" in states or "error_type" in self.mission else
                                          "cancelled" if "cancelled" in states else "completed")
                self.done.set()

    # 顺序调用一架无人机的搜索任务，失败后跳过该机剩余队列，其他无人机继续。
    # 输入为无人机名称；更新共享任务状态，不重新读取落地状态或等待图片识别。
    def _fly(self, name):
        tasks = [task for task in self.mission["tasks"] if task["drone_id"] == name]
        failed = False
        for task in tasks:
            with self.lock:
                if failed or self.stop_event.is_set():
                    task["status"] = "cancelled" if self.stop_event.is_set() else "skipped"
                    continue
                task["status"] = "running"
            try:
                search = self.search_fn
                if search is None:
                    from UAV.API.search import search
                with self.lock:
                    self.mission["executed"] = True
                search(name, deepcopy(task["target"]), **self.mission["settings"], stop_event=self.stop_event)
                with self.lock:
                    task["status"] = "completed"
            except (InterruptedError, KeyboardInterrupt):
                self.stop_event.set()
                with self.lock:
                    task["status"] = "cancelled"
                failed = True
            except BaseException as error:
                with self.lock:
                    task.update(status="failed", error_type=type(error).__name__, error=str(error))
                failed = True

    # 返回当前任务的独立快照，避免界面或调用者修改内部队列。
    # 可带任务 ID 校验；无任务或 ID 不匹配时抛出异常。
    def status(self, mission_id=None):
        with self.lock:
            if self.mission is None or mission_id is not None and mission_id != self.mission["mission_id"]:
                raise ValueError("任务不存在，或不属于当前执行进程")
            return deepcopy(self.mission)

    # 请求停止整个任务，当前 search 会在飞行检查点取消并制动，后续区域不再执行。
    # 返回取消请求后的快照；请求取消不等于已经悬停，应继续查询最终状态。
    def cancel(self, mission_id=None):
        with self.lock:
            self.status(mission_id)
            if not self.done.is_set():
                self.stop_event.set()
                self.mission["status"] = "cancelling"
            return deepcopy(self.mission)

    # 显式等待飞行队列结束，供命令行使用；等待时不持有内部锁。
    # 输入为可选超时秒数；返回最终快照，超时抛出异常但不会自动取消飞行。
    def wait(self, timeout=None):
        with self.lock:
            self.status()
            thread, done, mission = self.thread, self.done, self.mission
        if not done.wait(timeout):
            raise TimeoutError("飞行任务仍在执行")
        if thread.ident is not None:
            thread.join()
        with self.lock:
            return deepcopy(mission)
