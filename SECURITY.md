# Privacy and service boundary

Antigen Studio is a local single-user service, bound to 127.0.0.1. It checks Host/Origin and requires a same-origin JSON request header for mutations. These checks are not multi-user authentication. Use an SSH tunnel for remote access; do not publish the HTTP port or rely on a reverse proxy alone for authorization.

SSH keys, passwords and tokens stay in the operator's SSH configuration/agent. Application YAML contains only references to SSH aliases and filesystem paths. Uploads, task names, sequences, results, environment identities and controller logs may contain private information; they belong in ignored project directories. Inspect logs before sharing them in an issue.

Public source includes only allowlisted 1UBQ inputs, code, synthetic tests, templates and documentation. Local configuration is ignored, and `scripts/check_public_tree.py` checks candidate Git files for credential material and host-specific content. It supports additional operator-supplied `--deny` strings without storing them. Static scans are not proof that arbitrary future content is free of secrets.

Environment dependencies and deployed releases are read-only while jobs run. Recovery and cancellation are scoped to known project jobs and exact identities. Keep operating system accounts and project-owned directories appropriately protected; the local user can execute configured SSH commands and must be trusted.

If a credential is accidentally published, revoke/rotate it and review repository history; deleting the visible file alone is insufficient. Report vulnerabilities through the hosting platform's private security reporting channel when available. Do not include credentials or private sequences in public reports.
