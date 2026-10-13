"""复位范围、出生坐标转换、任务中止与失败清理的隔离验证。"""

import importlib
import math
import threading
import unittest
from unittest.mock import patch

from UAV.API import reset_uav
from UAV.utils.reset_state import initial_vehicle_pose
from UAV.utils.airsim_client import vehicle_lock

reset_module = importlib.import_module("UAV.API.reset_uav")


class FakeClient:
    def __init__(self, paused=False):
        self.paused = paused
        self.calls = []
        self.vehicles = ["UAV_01", "UAV_02"]
        self.control = {name: False for name in self.vehicles}
        self.poses = {}
        self.fail_method = None
        self.cancel_callback = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def call(self, method, *args, **kwargs):
        self.calls.append((method, args, kwargs))
        if method == self.fail_method:
            raise RuntimeError("RPC failure")
        if method == "listVehicles":
            return self.vehicles
        if method == "simIsPaused":
            return self.paused
        if method == "simPause":
            self.paused = args[0]
        if method == "enableApiControl":
            self.control[args[1]] = args[0]
        if method == "isApiControlEnabled":
            return self.control[args[0]]
        if method == "armDisarm":
            return False  # 已经上锁时的合法重复调用。
        if method == "simSetKinematics":
            self.poses[args[2]] = args[0]
        if method == "simRunConsoleCommand":
            return True
        if method == "cancelLastTask" and self.cancel_callback:
            self.cancel_callback(args[0])


class ResetTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        self.pose = {"position": {"x_val": 0.0, "y_val": 0.0, "z_val": -0.15},
                     "orientation": {"w_val": 1.0, "x_val": 0.0, "y_val": 0.0, "z_val": 0.0}}
        client_patch = patch.object(reset_module, "AirSimClient", return_value=self.client)
        pose_patch = patch.object(reset_module, "initial_vehicle_pose", return_value=self.pose)
        client_patch.start()
        self.pose_mock = pose_patch.start()
        self.addCleanup(client_patch.stop)
        self.addCleanup(pose_patch.stop)

    def test_default_resets_all_using_native_reset(self):
        self.assertIsNone(reset_uav())
        self.assertEqual([args[0] for method, args, _ in self.client.calls if method == "cancelLastTask"],
                         ["UAV_01", "UAV_02"])
        self.assertEqual(sum(method == "reset" for method, _, _ in self.client.calls), 1)
        self.assertFalse(self.client.poses)
        self.pose_mock.assert_not_called()
        for name in self.client.vehicles:
            self.assertFalse(vehicle_lock(name).locked())

    def test_specific_resets_only_selected_vehicle_without_global_reset(self):
        for identifier in (2, "2", "UAV_02"):
            with self.subTest(identifier=identifier):
                self.client.calls.clear()
                self.assertIsNone(reset_uav(identifier))
                self.assertNotIn("reset", [method for method, _, _ in self.client.calls])
                self.assertEqual(set(self.client.poses), {"UAV_02"})
                state = self.client.poses["UAV_02"]
                self.assertEqual(state["position"], self.pose["position"])
                self.assertEqual(state["orientation"], self.pose["orientation"])
                for field in ("linear_velocity", "angular_velocity", "linear_acceleration", "angular_acceleration"):
                    self.assertEqual(list(state[field].values()), [0, 0, 0])
                self.assertFalse(self.client.control["UAV_02"])
                self.assertFalse(self.client.paused)
                self.assertFalse(vehicle_lock("UAV_02").locked())
                self.assertTrue(all("UAV_01" not in str(args) for _, args, _ in self.client.calls))

    def test_invalid_identifier_does_not_modify_simulation(self):
        for identifier in (True, False, 0, -1, 1.5, [], "", "UAV_99"):
            with self.subTest(identifier=identifier):
                self.client.calls.clear()
                with self.assertRaises(ValueError):
                    reset_uav(identifier)
                self.assertEqual([method for method, _, _ in self.client.calls], ["listVehicles"])

    def test_failure_restores_pause_control_and_locks(self):
        for initially_paused in (False, True):
            with self.subTest(paused=initially_paused):
                self.client.paused = initially_paused
                self.client.fail_method = "simSetKinematics"
                with self.assertRaises(RuntimeError):
                    reset_uav(1)
                self.assertEqual(self.client.paused, initially_paused)
                self.assertFalse(self.client.control["UAV_01"])
                self.assertFalse(vehicle_lock("UAV_01").locked())

    def test_global_failure_releases_every_acquired_lock(self):
        self.client.fail_method = "reset"
        with self.assertRaises(RuntimeError):
            reset_uav()
        for name in self.client.vehicles:
            self.assertFalse(vehicle_lock(name).locked())

    def test_reset_waits_for_running_flight_to_stop(self):
        ready, cancelled = threading.Event(), threading.Event()
        lock = vehicle_lock("UAV_01")

        def flight():
            with lock:
                ready.set()
                cancelled.wait(timeout=2)

        thread = threading.Thread(target=flight)
        thread.start()
        self.assertTrue(ready.wait(timeout=2))
        self.client.cancel_callback = lambda name: cancelled.set()
        try:
            reset_uav(1)
            self.assertIn("UAV_01", self.client.poses)
            self.assertFalse(lock.locked())
        finally:
            cancelled.set()
            thread.join(timeout=2)

    def test_empty_fleet_is_noop(self):
        self.client.vehicles = []
        self.assertIsNone(reset_uav())
        self.assertEqual([method for method, _, _ in self.client.calls], ["listVehicles"])


class InitialPoseTests(unittest.TestCase):
    def make_client(self, config):
        import json

        class PoseClient:
            def call(self, method, *args):
                if method == "getSettingsString":
                    return json.dumps({"Vehicles": {"UAV_01": config}})
                if method == "simGetVehiclePose":
                    return {"position": {"x_val": 25, "y_val": 50, "z_val": -20.15}}
                if args[0] == "PlayerStart_0":
                    return {"orientation": {"w_val": math.sqrt(0.5), "x_val": 0,
                                            "y_val": 0, "z_val": math.sqrt(0.5)}}
                return {"position": {"x_val": 145, "y_val": -247, "z_val": -20}}

        return PoseClient()

    def test_spawn_position_accounts_for_distinct_local_origin_and_body_height(self):
        config = {"X": 120, "Y": -297, "Z": 0, "Roll": 0, "Pitch": 0, "Yaw": 90}
        pose = initial_vehicle_pose(self.make_client(config), "UAV_01")
        self.assertAlmostEqual(pose["position"]["x_val"], 0)
        self.assertAlmostEqual(pose["position"]["y_val"], 0)
        self.assertAlmostEqual(pose["position"]["z_val"], -0.15)
        self.assertAlmostEqual(pose["orientation"]["w_val"], math.sqrt(0.5))
        self.assertAlmostEqual(pose["orientation"]["z_val"], math.sqrt(0.5))

    def test_unspecified_rotation_inherits_reference_orientation(self):
        config = {"X": 120, "Y": -297, "Z": 0, "Roll": 0, "Pitch": 0}
        with patch("UAV.utils.reset_state.calibrate", return_value={"actor": "PlayerStart_0"}):
            pose = initial_vehicle_pose(self.make_client(config), "UAV_01")
        self.assertAlmostEqual(pose["orientation"]["w_val"], math.sqrt(0.5))
        self.assertAlmostEqual(pose["orientation"]["z_val"], math.sqrt(0.5))

    def test_unknown_spawn_height_is_rejected_instead_of_guessing_zero(self):
        config = {"X": 120, "Y": -297}
        with self.assertRaises(RuntimeError):
            initial_vehicle_pose(self.make_client(config), "UAV_01")


if __name__ == "__main__":
    unittest.main()
