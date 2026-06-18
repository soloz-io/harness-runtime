Ask User / Interrupt
#	Test	SDK expectation
B1	Definition has ask_user in node tools, agent calls it, harness emits result {subtype:"interrupted", interrupt: {type:"ask_user", questions:[...], tool_call_id:"..."}}	Critical. SDK receives the interrupt with questions to present to the user
B2	interrupt field is present on the ResultFrame with correct shape: {type:"ask_user", questions: [{question, type, choices?, required?}], tool_call_id}	SDK renders the question in UI
B3	Without ask_user in node tools, agent never calls it → normal result {subtype:"success"}	No interrupt if not configured
B4	Multiple consecutive ask_user calls within one turn → each produces its own interrupted result	SDK handles multiple interrupts (queues or yields each)