import json
import os
import sys
from pathlib import Path
from typing import NoReturn

from dotenv import load_dotenv

from core.event_publisher import StdioPublisher
from core.executor import ExecutionManager
from core.session import Session

env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=env_path)


def main() -> None:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        _error_exit("DATABASE_URL environment variable is required", 2)

    session: Session | None = None
    execution_manager: ExecutionManager | None = None
    publisher = StdioPublisher()

    try:
        execution_manager = ExecutionManager(
            postgres_connection_string=database_url,
            publisher=publisher,
        )

        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue

            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue

            msg_type = msg.get("type")

            if msg_type == "control_request":
                request = msg.get("request", {})
                subtype = request.get("subtype")
                request_id = msg.get("request_id", "")

                if subtype == "initialize":
                    agent_definition = request.get("agent_definition", {})
                    input_payload = request.get("input_payload", {})

                    session = Session(
                        agent_definition=agent_definition,
                        input_payload=input_payload,
                        execution_manager=execution_manager,
                        publisher=publisher,
                    )
                    session.initialize()

                    publisher.publish_control_response(
                        request_id=request_id,
                        session_id=session.session_id,
                    )

                elif subtype == "interrupt":
                    publisher.publish_control_response(
                        request_id=request_id,
                    )

            elif msg_type == "user":
                if session is None:
                    publisher.publish_control_response(
                        request_id="",
                        subtype="error",
                        error="Session not initialized",
                    )
                    continue

                try:
                    user_content = ""
                    user_msg = msg.get("message", {})
                    raw_content = user_msg.get("content", "")
                    if isinstance(raw_content, str):
                        user_content = raw_content
                    elif isinstance(raw_content, list):
                        texts = [
                            b.get("text", "") for b in raw_content
                            if isinstance(b, dict) and b.get("type") == "text"
                        ]
                        user_content = " ".join(texts)

                    session.run_turn(user_content=user_content)
                except Exception as e:
                    publisher.publish_result(
                        session_id=session.session_id,
                        subtype="error_during_execution",
                        is_error=True,
                        result=str(e),
                    )
                    sys.exit(1)

    except KeyboardInterrupt:
        pass
    finally:
        if execution_manager:
            execution_manager.close()


def _error_exit(message: str, code: int = 1) -> NoReturn:
    print(message, file=sys.stderr)
    sys.exit(code)


if __name__ == "__main__":
    main()
