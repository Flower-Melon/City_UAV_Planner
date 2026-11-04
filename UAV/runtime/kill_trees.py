"""巡查和监控无人机下视相机的树木过滤；同一文件也由 UE Python 加载执行。"""

import json
from pathlib import Path
import uuid


TREE_ASSET_ROOT = "/Game/Carla/Static/Vegetation/Trees/"
_sessions = {}  # 仅 UE 进程使用；每台无人机的 RGB capture 独立管理。


class CameraTreeFilter:
    def __init__(self, client, vehicle):
        self.client = client
        self.vehicle = vehicle
        self.session = uuid.uuid4().hex
        self.started = False
        self.log_path = None

    def _command(self, operation, *, reclaim_stale=False):
        from .airsim_state import _run_ue_json, editor_log

        if self.log_path is None:
            self.log_path = editor_log()

        token = "CITY_SEARCH_CAMERA_" + uuid.uuid4().hex + ":"
        request = {"operation": operation, "session": self.session, "vehicle": self.vehicle,
                   "reclaim_stale": reclaim_stale}
        source = Path(__file__).resolve()
        script = "\n".join([
            "def _city_search_command():",
            "    import sys, importlib.util, unreal, json",
            "    try:",
            "        name = 'city_search_camera_runtime'",
            "        stamp = " + repr(source.stat().st_mtime_ns),
            "        module = sys.modules.get(name)",
            "        if module is None or getattr(module, '_source_stamp', None) != stamp:",
            "            sessions = getattr(module, '_sessions', {})",
            "            spec = importlib.util.spec_from_file_location(name, " + repr(str(source)) + ")",
            "            module = importlib.util.module_from_spec(spec)",
            "            spec.loader.exec_module(module)",
            "            module._sessions = sessions",
            "            module._source_stamp = stamp",
            "            sys.modules[name] = module",
            "        result = {'ok': True, 'result': module._dispatch(" + repr(request) + ")}",
            "    except Exception as error:",
            "        result = {'ok': False, 'error': str(error)}",
            "    unreal.log(" + repr(token) + " + json.dumps(result))",
            "_city_search_command()",
        ])
        response = _run_ue_json(
            self.client, "exec(" + repr(script) + ")", token,
            "UE 未接受下视相机设置，请启用 PythonScriptPlugin",
            "UE 未返回下视相机设置结果，请检查 Output Log",
            log_path=self.log_path,
        )
        if not response["ok"]:
            raise RuntimeError("下视相机树木过滤失败：" + response["error"])
        return response["result"]

    def start(self, *, reclaim_stale=False):
        """reclaim_stale 只供已持有该无人机跨进程任务锁的调用方使用。"""
        # 即使回执丢失，close 也会按同一会话编号尝试撤销。
        self.started = True
        return self._command("start", reclaim_stale=reclaim_stale)

    def refresh(self):
        if not self.started:
            raise RuntimeError("下视相机树木过滤尚未初始化")
        return self._command("refresh")

    def close(self):
        if self.started:
            self._command("close")
            self.started = False


def _capture_component(unreal, world, vehicle):
    pawns = [a for a in unreal.GameplayStatics.get_all_actors_of_class(world, unreal.Pawn)
             if a.get_name() == vehicle]
    if len(pawns) != 1:
        raise RuntimeError("无法唯一定位无人机 " + vehicle)
    cameras = [c for c in pawns[0].get_components_by_class(unreal.ChildActorComponent)
               if c.get_name() == "BottomCenterCamera"]
    if len(cameras) != 1:
        raise RuntimeError("无法唯一定位 BottomCenterCamera")
    camera = cameras[0].get_editor_property("child_actor")
    if camera is None:
        raise RuntimeError("下视相机尚未生成")
    captures = [c for c in camera.get_components_by_class(unreal.SceneCaptureComponent2D)
                if c.get_name() == "SceneCaptureComponent"]
    if len(captures) != 1:
        raise RuntimeError("无法唯一定位下视 RGB SceneCaptureComponent")
    return captures[0]


def _refresh(unreal, world, state):
    capture = state["capture"]
    if not unreal.SystemLibrary.is_valid(capture):
        raise RuntimeError("下视相机已失效，请重新启动任务")
    # 取图前或监控机到位后扫描，覆盖飞行途中流式加载的分块和新组件。
    # StaticMeshComponent 查询包含 ISM/HISM，不能隐藏其共用的分块 Actor。
    count = 0
    for actor in unreal.GameplayStatics.get_all_actors_of_class(world, unreal.Actor):
        for component in actor.get_components_by_class(unreal.StaticMeshComponent):
            mesh = component.get_editor_property("static_mesh")
            if mesh is not None and mesh.get_path_name().startswith(TREE_ASSET_ROOT):
                capture.hide_component(component)
                count += 1
    if not count:
        raise RuntimeError("当前场景未找到树木组件，请核对 TREE_ASSET_ROOT")
    return {"tree_components": count}


def _dispatch(request):
    import unreal

    session, operation = request["session"], request["operation"]
    # Play 结束后对象失效，不把上一次仿真的所有权带入新场景。
    for key, state in list(_sessions.items()):
        if not unreal.SystemLibrary.is_valid(state["capture"]):
            del _sessions[key]
    if operation == "close":
        state = _sessions.get(session)
        if state is not None:
            # UE4.27 不向 Python 开放 HiddenComponents 的读写，但提供 hide/clear。
            # 当前 AirSim 的 RGB capture 初始列表为空，本模块独占管理该列表。
            # HiddenActors 独立保存，不在这里清除。
            state["capture"].clear_hidden_components()
            del _sessions[session]
        return {}
    if operation not in ("start", "refresh"):
        raise ValueError("未知下视相机操作：" + operation)
    world = unreal.EditorLevelLibrary.get_game_world()
    if world is None:
        raise RuntimeError("请先启动 UE Play 仿真")
    if operation == "start" and session not in _sessions:
        capture = _capture_component(unreal, world, request["vehicle"])
        previous = [key for key, state in _sessions.items() if state["capture"] == capture]
        if previous:
            if not request.get("reclaim_stale", False):
                raise RuntimeError("该无人机的下视相机已被另一会话占用")
            # 调用方已取得无人机跨进程独占锁，旧任务不再持有该机。
            # 进程强制退出会释放文件锁，但不会清理 UE 内存中的会话。
            capture.clear_hidden_components()
            for key in previous:
                del _sessions[key]
        _sessions[session] = {"capture": capture}
        try:
            return _refresh(unreal, world, _sessions[session])
        except Exception:
            # 初始化失败立即撤销部分设置；清理失败则保留会话供 close 重试。
            capture.clear_hidden_components()
            del _sessions[session]
            raise
    if session not in _sessions:
        raise RuntimeError("搜索相机会话不存在，请重新启动搜索")
    return _refresh(unreal, world, _sessions[session])
