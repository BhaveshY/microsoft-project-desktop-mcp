# Agent instructions

- Target Windows 11, Python 3.10+, and classic Microsoft Project Desktop COM automation.
- Run `scripts/verify-ms-project-mcp.ps1` before handoff. It must not activate Project.
- Run the real desktop smoke only with explicit `-AllowWriteFixture` consent; it launches Project and writes a disposable MPP.
- Keep the public MCP surface at eight typed tools. Do not expose arbitrary COM, VBA, macros, or generic field mutation.
- Preserve expected-state checks, idempotency, confirmation, native reread, server/user ownership, and STA isolation.
- Never treat fake COM tests as proof that a real Microsoft Project installation was verified.
