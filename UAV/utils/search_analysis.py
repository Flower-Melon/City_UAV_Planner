"""顺序读取落盘清单，独立识别并更新报告；不连接或控制无人机。"""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import threading
import time


# 先写临时文件再替换摘要，避免报告只写入一半就被读取。
# 输入为任务目录和摘要对象；保存 summary.json，无返回值。
def write_summary(directory, summary):
    path = Path(directory)/"summary.json"
    temporary = path.with_suffix(".part")
    temporary.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


class SearchAnalysis:
    # 锁定任务分析目录，恢复已有结果并准备独立识别线程。
    # 输入为任务目录、分析回调及重试选项；创建线程但不启动，也不连接飞行控制。
    def __init__(self, directory, analyzer, *, close_analyzer=False, retry_failed=False, lock=None):
        self.directory, self.analyzer = Path(directory), analyzer
        self.close_analyzer, self.retry_failed = close_analyzer, retry_failed
        self.capture_done = threading.Event()
        self.done = threading.Event()
        self.results, self.error, self.summary, self.report_error = {}, None, None, None
        self.lock = lock or threading.RLock()
        # 操作系统锁随进程退出自动释放，避免续跑程序重复识别同一个任务。
        self.lease = (self.directory/".analysis.lock").open("a+b")
        self.lease.write(b"\0")
        self.lease.flush()
        self.lease.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.lease.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            ledger = self.directory/"analyses.jsonl"
            if ledger.exists():
                with ledger.open("r+b") as stream:
                    while True:
                        offset = stream.tell()
                        line = stream.readline()
                        if not line:
                            break
                        if not line.endswith(b"\n"):
                            # 进程中断留下的末尾半条结果不算完成，续跑会重新识别这张图。
                            stream.truncate(offset)
                            break
                        record = json.loads(line)
                        self.results[record["frame_id"]] = record
        except BaseException:
            self.lease.close()
            raise
        self.thread = threading.Thread(target=self._run, daemon=False, name="search-analysis")

    # 启动读取图片清单的后台识别线程。
    # 无返回值；识别在该线程内按图片顺序处理，不占用飞行线程。
    def start(self):
        self.thread.start()

    # 保存拍照与飞行结束状态，并通知后台不再等待新增图片。
    # 输入为飞行摘要；无返回值，不等待任何未完成的模型请求。
    def complete_capture(self, summary):
        """发布飞行结束状态后唤醒后台；调用者不等待任何模型请求。"""
        with self.lock:
            self.summary = dict(summary)
            self.summary.update(analysis_status="running", **self.stats())
            if self.summary["flight_status"] == "completed":
                self.summary["status"] = "analysis_pending"
            try:
                write_summary(self.directory, self.summary)
            finally:
                self.capture_done.set()

    # 在共享锁保护下追加识别结果或异常事件。
    # 输入为日志文件名和记录对象；写入当前任务目录，无返回值。
    def _log(self, filename, value):
        with self.lock:
            with (self.directory/filename).open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(value, ensure_ascii=False, allow_nan=False)+"\n")

    # 持续读取已完整落盘的图片清单，拍照结束前等待新增记录。
    # 逐个产出照片元数据；结束时若发现半条记录则报错，避免误报全部完成。
    def _frames(self):
        manifest = self.directory/"frames.jsonl"
        manifest.touch(exist_ok=True)
        with manifest.open(encoding="utf-8") as stream:
            while True:
                offset = stream.tell()
                line = stream.readline()
                if line.endswith("\n"):
                    yield json.loads(line)
                else:
                    stream.seek(offset)
                    if self.capture_done.is_set():
                        if line:
                            raise ValueError("图片清单末行不完整，不能声称全部分析完成")
                        return
                    self.capture_done.wait(.05)

    # 分析一张图片，失败最多尝试两次，并保存耗时、结果及异常事件。
    # 输入为照片元数据；更新结果缓存和日志，异常只记录，暂不控制无人机处置。
    def _analyze(self, metadata):
        started = time.monotonic()
        stamp = datetime.now(timezone.utc)
        attempt_seconds = []
        for attempt in range(2):
            attempt_started = time.monotonic()
            try:
                result = self.analyzer(self.directory/metadata["image"], dict(metadata))
                from llm.vision import validate_analysis
                result = {**validate_analysis(result), "analysis_state": "completed"}
                break
            except Exception as error:
                result = {"analysis_state": "failed", "error_type": type(error).__name__}
            finally:
                attempt_seconds.append(round(time.monotonic()-attempt_started, 3))
        record = {"frame_id": metadata["frame_id"], "image": metadata["image"], **result,
                  "attempts": attempt+1, "attempt_seconds": attempt_seconds,
                  "analysis_started_at_utc": stamp.isoformat(),
                  "analysis_seconds": round(time.monotonic()-started, 3)}
        captured = metadata.get("captured_at_utc")
        if captured:
            record["queue_wait_seconds"] = max(0, round((stamp-datetime.fromisoformat(captured)).total_seconds(), 3))
        self._log("analyses.jsonl", record)
        if result.get("status") == "abnormal":
            self._log("events.jsonl", {"type": "anomaly", "frame_id": metadata["frame_id"],
                "image": metadata["image"], "description": result["description"],
                "observer_position_m": metadata.get("position_m"), "handling": "recorded"})
        with self.lock:
            self.results[metadata["frame_id"]] = record

    # 统计当前识别成功、失败、待处理、异常及不确定的图片数量。
    # 无输入；返回统计字典，在共享锁内读取已完成记录。
    def stats(self):
        with self.lock:
            records = list(self.results.values())
            images = (self.summary or {}).get("images", 0)
            return {"analyzed": sum(r["analysis_state"] == "completed" for r in records),
                    "analysis_failed": sum(r["analysis_state"] == "failed" for r in records),
                    "analysis_pending": max(0, images-len(records)),
                    "anomalies": sum(r.get("status") == "abnormal" for r in records),
                    "uncertain": sum(r.get("status") == "uncertain" for r in records)}

    # 顺序消费图片清单，跳过已完成记录，并在拍照结束后汇总最终报告。
    # 作为识别线程入口；退出时释放模型和目录锁，整个过程不控制飞行。
    def _run(self):
        try:
            for metadata in self._frames():
                previous = self.results.get(metadata["frame_id"])
                if previous and (previous["analysis_state"] == "completed" or not self.retry_failed):
                    continue
                self._analyze(metadata)
        except Exception as error:
            self.error = type(error).__name__
        finally:
            # 分析失败也不能触发飞行控制；等主流程发布自己的飞行结果再汇总。
            self.capture_done.wait()
            try:
                summary = {**self.summary, **self.stats(), "analysis_error": self.error,
                           "finished_at_utc": datetime.now(timezone.utc).isoformat()}
                incomplete = bool(self.error or summary["analysis_failed"] or summary["analysis_pending"])
                summary["analysis_status"] = "incomplete" if incomplete else "completed"
                summary["status"] = summary["flight_status"]
                if summary["flight_status"] == "completed":
                    summary["status"] = "incomplete" if incomplete or summary.get("capture_error") or not summary["images"] else "completed"
                write_summary(self.directory, summary)
            except Exception as error:
                self.report_error = type(error).__name__
            finally:
                try:
                    if self.close_analyzer:
                        self.analyzer.close()
                finally:
                    self.lease.close()
                    self.done.set()

    # 供主动等待报告的调用者阻塞到识别完成，支持超时。
    # 返回最终摘要；超时或报告写入失败则抛出异常，飞行主流程不会调用它。
    def wait(self, timeout=None):
        """仅供显式等待报告的调用者/测试；飞行主流程不调用。"""
        if not self.done.wait(timeout):
            raise TimeoutError("图片仍在后台分析，尚未生成最终报告")
        self.thread.join()
        if self.report_error:
            raise RuntimeError(f"识别报告保存失败：{self.report_error}")
        return json.loads((self.directory/"summary.json").read_text(encoding="utf-8"))


# 恢复已停止拍照任务的离线图片分析，可选择重试之前失败的照片。
# 输入为目录和可选分析器；等待后返回最终摘要，不重新飞行、不启动拍照。
def resume_analysis(directory, analyzer=None, *, retry_failed=False):
    """只处理已停止拍照的任务；无需AirSim，不重新飞行。"""
    directory = Path(directory)
    summary = json.loads((directory/"summary.json").read_text(encoding="utf-8"))
    summary.setdefault("flight_status", summary["status"])
    # 拍照期间进程被终止时，不能凭图片反推航路已完成或机体已经停止。
    if summary["flight_status"] == "running":
        summary.update(flight_status="unknown", flight_completed=False)
    with (directory/"frames.jsonl").open(encoding="utf-8") as stream:
        summary["images"] = sum(line.endswith("\n") for line in stream)
    owned = analyzer is None
    if owned:
        from llm.vision import VisionAnalyzer
        analyzer = VisionAnalyzer()
    try:
        worker = SearchAnalysis(directory, analyzer, close_analyzer=owned, retry_failed=retry_failed)
    except BaseException:
        if owned:
            analyzer.close()
        raise
    worker.start()
    worker.complete_capture(summary)
    return worker.wait()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="继续识别已落盘图片，不控制无人机")
    parser.add_argument("directory", type=Path)
    parser.add_argument("--retry-failed", action="store_true")
    args = parser.parse_args()
    print(json.dumps(resume_analysis(args.directory, retry_failed=args.retry_failed), ensure_ascii=False, indent=2))
