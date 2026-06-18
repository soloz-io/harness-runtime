Resume After Interrupt
#	Test	SDK expectation
C1	SDK sends control_request {subtype:"initialize", resume_payload: {status:"answered", answers:["yes"]}} → harness emits frames continuing from the interrupt point, then result {subtype:"success"}	Critical. Graph resumes, ask_user tool sees the answers, agent continues
C2	Resume payload with {status:"cancelled"} → ask_user receives "(cancelled)" answers, agent handles	Cancellation flows through correctly
C3	Resume payload with {status:"error", error:"..."} → ask_user receives "(error: ...)" answers	Error payload is propagated
C4	Resume payload with wrong number of answers → harness doesn't crash, fills missing with "(no answer)"	Graceful degradation
C5	Resume payload with malformed/dict structure → harness doesn't crash, returns explicit error answers	Robust to bad input
C6	Resume on a session that was NOT interrupted → harness handles gracefully (no-op or error response, not crash)	Won't double-resume