Frame Protocol — NDJSON exchange
#	Test	SDK expectation
A1	SDK sends control_request {subtype:"initialize", agent_definition, input_payload} → harness replies with control_response {subtype:"success", session_id}	Session is created, graph starts
A2	After A1, SDK sends user {content:"hello"} → harness emits assistant frames, stream_event deltas, then result {subtype:"success"}	Standard text generation works
A3	Incoming frames carry correct session_id across all frame types	SDK identifies which session each frame belongs to
A4	Harness rejects unknown msg_type with a control_response {subtype:"error"} or ignores gracefully	Harness doesn't crash on unexpected input