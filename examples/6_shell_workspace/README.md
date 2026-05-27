# Shell Workspace Payloads

Use this directory when there is no single Python entrypoint yet and you want
Pyruns to manage commands.

```bash
cd examples/6_shell_workspace
pyr
```

Then open Generator in Shell mode and paste one payload from `payloads/`.

The payloads are intentionally simple:

- `bash_or_wsl.sh` for Bash, WSL, Git Bash, or Linux
- `powershell.ps1` for Windows PowerShell or PowerShell 7
- `cmd.cmd` for cmd.exe

Pyruns inherits the environment from the terminal that launched it. You can
also add per-task env vars from the task detail panel before running.
