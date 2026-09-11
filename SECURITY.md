# Security

## Reporting a vulnerability

Use GitHub private vulnerability reporting for security issues. Include the
affected version or commit, the smallest reproducible description, and the
expected impact. Please allow time for assessment before public disclosure.

Do not post secrets, host profiles, environment files, private origins,
application data, logs containing sensitive values, or exploit details in a
public issue. If a report accidentally exposes a credential, revoke or rotate
it independently; deleting a comment does not make a disclosed secret safe.

## Security boundary

Local Web is a single-user macOS hosting framework, not an authentication
gateway. `trusted-lan` exposes apps to every device that can reach the host.
`tailscale-serve` relies on externally administered Tailscale identity and
policy, and intentionally has no Funnel or LAN fallback. Applications remain
responsible for authorization on sensitive actions and for protecting their
own durable data.

Private host state lives under ignored `config/local/`. Keep it mode-restricted,
back it up using the supported host commands, and never commit it. See
[`docs/architecture.md`](docs/architecture.md) and
[`docs/operations/host-profile.md`](docs/operations/host-profile.md) for the
complete trust and recovery model.
