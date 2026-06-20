import json
import os
import subprocess

agent_definition = {"nodes": [{"config": {"model": {"model_name": "deepseek-v4-flash"}}}]}

init_req = {
    "type": "control_request",
    "request_id": "req_1",
    "request": {
        "subtype": "initialize",
        "agent_definition": agent_definition,
        "input_payload": {},
        "resume_payload": {"decisions": [{"type": "approve"}]},
        "session_id": "sess_d6b6778a5589411f93af49f1",
    },
}

p = subprocess.Popen(
    ["python", "cli.py"],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    cwd="/Users/arun_subramanian/Projects/soloz-io/waypoint/reference/harness-runtime",
    env={
        **os.environ,
        "DATABASE_URL": "postgresql://waypoint:waypoint@localhost:5433/waypoint_test",
    },
)
out, err = p.communicate(json.dumps(init_req).encode() + b"\n")
print("STDOUT:", out.decode())
print("STDERR:", err.decode())
print("EXIT CODE:", p.returncode)
