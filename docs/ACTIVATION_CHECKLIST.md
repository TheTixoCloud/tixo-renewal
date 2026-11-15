# Minimal Activation Checklist

1. `python3 bin/renewal_cli.py store-credentials --node self` (ON the Virtualizor host; interactive, one-time).
2. `python3 bin/renewal_cli.py validate-credentials --node self` — confirms the key/URL work via a sentinel vpsid that cannot exist; **no real VPS is touched**. Expect `CREDENTIALS_LIKELY_VALID`.
3. Pick ONE real VPS. `preview --node self --vpsid <id>` repeatedly until you trust the number.
4. `touch renewal-config/WRITE_APPROVED && chmod 600 renewal-config/WRITE_APPROVED`.
5. `apply --node self --vpsid <id> --period-key <key> --new-quota-gb <value> --execute` — manual, interactive, typed `APPLY` confirmation, for that one VPS only.
6. Only after that has worked correctly for a real billing cycle, consider `apply-due`/the unattended timer (`docs/PRODUCTION_ACTIVATION.md`, step E) — highest risk, do last.
