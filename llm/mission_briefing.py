"""Generate a cited, read-only briefing from a patrol's execution journal."""

import json
from pathlib import Path
import re


class MissionBriefing:
    def __init__(self, client, model, journal_directory):
        self.client = client
        self.model = model
        self.directory = Path(journal_directory).resolve()
        self.prompt = (Path(__file__).parent / "prompts" / "mission_briefing.txt").read_text(encoding="utf-8")

    def summarize(self, session_id, revision, previous_update=None):
        if not isinstance(session_id, str) or not re.fullmatch(r"[a-f0-9]{32}", session_id):
            raise ValueError("无效的任务日志编号。")
        if type(revision) is not int or revision < 1:
            raise ValueError("无效的任务日志版本。")
        journal = (self.directory / f"{session_id}.jsonl").resolve(strict=True)
        if journal.parent != self.directory:
            raise ValueError("任务日志路径无效。")
        with journal.open("rb") as stream:
            end = stream.seek(0, 2)
            start = max(0, end - 128 * 1024)
            stream.seek(max(0, start - 1))
            if start and stream.read(1) != b"\n":
                stream.readline(end - start)
            data = stream.read(end - stream.tell())
        # The executor may have been appending a newer revision when the file size was sampled.
        lines = data.split(b"\n")[:-1]

        revisions = [json.loads(line) for line in lines]
        revisions = [entry for entry in revisions if entry["revision"] <= revision]
        if not revisions or revisions[-1]["revision"] != revision:
            raise ValueError("指定的任务日志版本尚不可用。")
        previous_revision = previous_update["revision"] if previous_update else 0
        event_revisions = [(event, entry["revision"]) for entry in revisions for event in entry["events"]][-24:]
        events = [dict(event, new_since_previous=recorded_at > previous_revision)
                  for event, recorded_at in event_revisions]
        response = self.client.responses.create(
            model=self.model,
            instructions=self.prompt,
            input=[{"role": "user", "content": json.dumps(
                {"current": revisions[-1]["current"], "events": events, "previous_update": previous_update},
                ensure_ascii=False
            )}],
            temperature=0,
            text={"format": {"type": "json_object"}},
            store=False,
        )
        if response.status != "completed":
            raise ValueError("模型未完成本次战报。")
        try:
            result = json.loads(response.output_text)
            summary = result["summary"].strip()
            focus = result["next_focus"].strip()
            citations = result["evidence"]
            evidence = {event["id"]: event for event, _ in event_revisions}
            if not (0 < len(summary) <= 240 and 0 < len(focus) <= 140 and isinstance(citations, list)
                    and 1 <= len(citations) <= 3 and all(type(item) is int and item in evidence for item in citations)
                    and len(set(citations)) == len(citations)):
                raise ValueError
        except (ValueError, TypeError, KeyError, AttributeError):
            raise ValueError("模型返回的战报或日志引用无效。") from None
        return {"summary": summary, "next_focus": focus, "evidence": [evidence[item] for item in citations],
                "model": self.model}
