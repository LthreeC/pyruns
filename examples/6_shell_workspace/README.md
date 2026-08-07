# Shell Workspace Payloads

Use this directory when there is no single Python entrypoint yet and you want
Pyruns to manage commands.

```bash
cd examples/6_shell_workspace
pyr init
pyr exec -n bash-example -- ./payloads/bash_or_wsl.sh
pyr exec -n powershell-example -- .\payloads\powershell.ps1
```

These commands are tracked replacements for running the payloads directly:
Pyruns records stdout/stderr, elapsed time, exit code, and source state. Use
`pyr ui shell` if you also want to create or inspect tasks in the Web UI.

The payloads are intentionally simple:

- `bash_or_wsl.sh` for Bash, WSL, Git Bash, or Linux
- `powershell.ps1` for Windows PowerShell or PowerShell 7
- `cmd.cmd` for cmd.exe

Pyruns inherits the environment from the terminal that launched it. Persist a
few task-local values with one `-e` followed by multiple `KEY=VALUE` entries,
or load a larger set with `--env-file PATH`.
