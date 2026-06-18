Checkpointer / Session Persistence
#	Test	SDK expectation
E1	Interrupt → checkpoint is saved with the ask_user interrupt value	Checkpoint contains the exact AskUserRequest dict
E2	Resume → checkpoint is restored, Command(resume=...) returns the resume_payload to the interrupt() call site	No data loss between interrupt and resume
E3	Multiple turns with interrupts → each turn checkpoints and resumes independently	Correct for multi-step workflows