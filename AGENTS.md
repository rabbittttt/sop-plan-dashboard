# Repository workflow

- After completing user-requested code or documentation changes, run the relevant checks, commit the intended source files, and push to `origin/main` without asking again.
- Use the Git proxy configured for the Codex environment. Check the current Git proxy setting instead of hardcoding an address.
- Keep `input_file/`, `data/`, `output_file/`, generated test files, and local runtime dependencies out of Git. Review staged files before every public push.
- If authentication, network access, or branch rules prevent a push, report the exact blocker and leave the local commit intact.
