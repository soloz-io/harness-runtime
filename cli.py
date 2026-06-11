import json
import logging
import os
import sys
from pathlib import Path
from typing import NoReturn

import structlog
from dotenv import load_dotenv

from core.event_publisher import StdioPublisher
from core.executor import ExecutionManager
from core.session import Session

env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=env_path)

# ---------------------------------------------------------------------------
# Logging configuration
# ---------------------------------------------------------------------------
# File logging (dev/test debugging, disabled unless HARNESS_LOG_FILE is set)
_log_level = os.getenv("HARNESS_LOG_LEVEL")
_log_file = os.getenv("HARNESS_LOG_FILE")

if _log_level and _log_file:
    _log_dir = Path(_log_file).parent
    _log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=_log_file,
        level=getattr(logging, _log_level.upper(), logging.DEBUG),
        force=True,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

# Structured stderr logging (production stdout must be pure NDJSON)
structlog.configure(
    logger_factory=structlog.PrintLoggerFactory(sys.stderr),
    cache_logger_on_first_use=True,
)


def main() -> None:
    # Disable deepagents' built-in OpenAI Responses API default.
    # The Responses API doesn't work with non-OpenAI models (e.g. deepseek
    # via OpenAI-compatible endpoint), causing 404 on sub-agent delegation.
    from deepagents.profiles.provider import ProviderProfile, register_provider_profile
    register_provider_profile(
        "openai",
        ProviderProfile(init_kwargs={"use_responses_api": False}),
    )

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
                    publisher.publish_result(
                        session_id="",
                        subtype="error_during_execution",
                        is_error=True,
                        result="Session not initialized",
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
                except Exception:
                    # Executor already published error result frame
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
