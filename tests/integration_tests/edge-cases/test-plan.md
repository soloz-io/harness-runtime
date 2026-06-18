Edge Cases
#	Test	SDK expectation
F1	Harness receives initialize with BOTH resume_payload AND agent_definition → resumes existing session, ignores new definition	SDK never sends this, but harness shouldn't crash
F2	Harness receives initialize with resume_payload but no prior session → graceful error (no crash)	Won't crash on out-of-order messages
F3	Simulated concurrent sessions (two separate harness subprocesses) → each operates independently	No cross-session state leakage
F4	Harness process killed mid-stream → SDK detects exit, cleans up	Process lifecycle handled