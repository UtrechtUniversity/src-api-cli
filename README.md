A simple Python SDK and CLI for SURF ResearchCloud.

- Import the package from [src/researchcloud/](/Users/3060845/Code/uu/src/api_explorations/src/researchcloud).
- Use [cli.py](/Users/3060845/Code/uu/src/api_explorations/src/researchcloud/cli.py) as the CLI module, or the installed `researchcloud` command.
- Install as a package via [pyproject.toml](/Users/3060845/Code/uu/src/api_explorations/pyproject.toml).
- Run the linter with `.venv/bin/ruff check .`.

For `get-application-offerings`, omitting `--type` means that all application
types are included. Use `--type` when you want to restrict the results to one
application type.

Use `get-workspace-status --id <id>` to quickly print a workspace's current
status, and `pause-workspace --id <id>` / `resume-workspace --id <id>` to
pause or resume a workspace. All three support `--dry-run`.
