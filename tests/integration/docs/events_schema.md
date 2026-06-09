### 1. `on_state_update`

This event provides a complete snapshot of the agent's state at the end of a significant step, such as after an agent or tool has finished running. It is the most valuable event for understanding the agent's memory and file system at any given point.

#### Schema (`on_state_update`)
```json
{
  "type": "object",
  "properties": {
    "event_type": {
      "type": "string",
      "const": "on_state_update"
    },
    "data": {
      "type": "object",
      "properties": {
        "messages": {
          "type": "string",
          "description": "A serialized string representation of the entire message history list up to this point."
        },
        "files": {
          "type": "object",
          "description": "A dictionary where keys are file paths and values are file objects.",
          "additionalProperties": {
            "type": "object",
            "properties": {
              "content": {
                "type": "array",
                "items": { "type": "string" }
              },
              "created_at": { "type": "string", "format": "date-time" },
              "modified_at": { "type": "string", "format": "date-time" }
            },
            "required": ["content", "created_at", "modified_at"]
          }
        },
        "todos": {
            "type": "array",
            "description": "A list of pending tasks or actions for the agent. This may not always be present."
        }
      },
      "required": ["messages", "files"]
    }
  },
  "required": ["event_type", "data"]
}
```

#### Example (`on_state_update`)
This example is from the end of the run, showing the final state of messages and files.
```json
{
  "event_type": "on_state_update",
  "data": {
    "messages": "[HumanMessage(...), AIMessage(...), ToolMessage(...), ... AIMessage(content='I have completed creating a simple hello world agent...')]",
    "files": {
      "/user_request.md": {
        "content": [
          "Create a simple hello world agent that greets users."
        ],
        "created_at": "2025-11-19T18:28:01.545142+00:00",
        "modified_at": "2025-11-19T18:28:01.545142+00:00"
      },
      "/orchestrator_plan.md": {
        "content": [
          "Mission: Create a simple hello world multi-agent system where an agent greets users.",
          "..."
        ],
        "created_at": "2025-11-19T18:28:05.050503+00:00",
        "modified_at": "2025-11-19T18:28:05.050503+00:00"
      }
      // ... other files
    }
  }
}
```

---

### 2. `on_llm_stream`

This event provides a real-time stream of the Language Model's output. It's highly granular, with each event representing a small chunk of text or a piece of a tool call. This is useful for displaying "typing" effects or for real-time debugging of the model's reasoning process.

#### Schema (`on_llm_stream`)
```json
{
  "type": "object",
  "properties": {
    "event_type": {
      "type": "string",
      "const": "on_llm_stream"
    },
    "data": {
      "type": "object",
      "properties": {
        "raw_event": {
          "type": "string",
          "description": "A serialized string representation of a tuple containing an AIMessageChunk and a metadata dictionary."
        }
      },
      "required": ["raw_event"]
    }
  },
  "required": ["event_type", "data"]
}
```

#### Explanation of `raw_event` Content
The `raw_event` string is a `repr()` of a Python tuple. To parse it, you would need to evaluate it (e.g., using `ast.literal_eval` in Python). The tuple contains:
1.  **AIMessageChunk**: An object with fields like `content` (the text token), `tool_call_chunks` (for streaming tool calls), and `response_metadata` (which contains `finish_reason` on the last chunk).
2.  **Metadata Dictionary**: Contains internal LangGraph tracing info like `thread_id`, `langgraph_step`, and `langgraph_node`.

#### Example (`on_llm_stream`)
This is the final chunk of an LLM stream, indicating the model has finished generating its response.
```json
{
  "event_type": "on_llm_stream",
  "data": {
    "raw_event": "(AIMessageChunk(content='', additional_kwargs={}, response_metadata={'finish_reason': 'stop', 'model_name': 'gpt-4.1-mini-2025-04-14', 'system_fingerprint': 'fp_4c2851f862', 'service_tier': 'default', 'model_provider': 'openai'}, id='lc_run--9efdc7fc-3993-4654-bef8-caf37c90ee32', chunk_position='last'), {'thread_id': 'test-job-456', ...})"
  }
}
```

---

### 3. `end`

This is the simplest event. It is a terminal signal indicating that the graph execution for a given `thread_id` has completely finished. Its `data` payload is currently empty but should not be assumed to always be.

#### Schema (`end`)
```json
{
  "type": "object",
  "properties": {
    "event_type": {
      "type": "string",
      "const": "end"
    },
    "data": {
      "type": "object",
      "description": "An empty object. Reserved for potential future use, like providing a final summary."
    }
  },
  "required": ["event_type", "data"]
}
```

#### Example (`end`)
```json
{
  "event_type": "end",
  "data": {}
}
```