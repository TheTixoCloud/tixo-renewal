# Production Activation Checklist

**Nothing in `install.sh`, `dry-run`, `preview`, `status`, `backup`, or `rollback` ever writes
to Virtualizor.** Activating real writes is a separate, multi-step, operator-only process.
This project's own automation (including any AI assistant working on this repo) will not
perform these steps for you — they require your deliberate judgment at each stage.

## Prerequisites before you even consider this

- [ ] You have run `dry-run` and `preview` repeatedly, for real, on this host, and the
      numbers match your own manual calculation for at least one real VPS.
- [ ] You have read `docs/native-reset-investigation.md` and separately confirmed (or
      investigated and ruled out) whether YOUR Virtualizor installation has any native reset
      mechanism this project cannot see.
- [ ] You understand that `apply`/`apply-due` write via Virtualizor's own admin API
      (`act=managevps`), not direct SQL, and have reviewed `implementation/writer.py`.
- [ ] You have a working backup (`renewal_cli.py backup`) and have tested `rollback` at least
      once in a non-production scenario.

## Step A — One-off manual apply (lowest risk, start here)

```bash
python3 bin/renewal_cli.py apply --node self --vpsid <id> --period-key <key> \
    --new-quota-gb <value> --execute
```

This still requires the `WRITE_APPROVED` marker (Step B) and prompts interactively for
credentials and a typed `APPLY` confirmation every time. Use this for your first few real
renewals before considering anything unattended.

## Step B — Create the approval marker (your explicit, one-time decision)

```bash
touch /opt/tixo-renewal/renewal-config/WRITE_APPROVED
chmod 600 /opt/tixo-renewal/renewal-config/WRITE_APPROVED
```

Nothing automated creates this file. Removing it immediately re-blocks all writes.

## Step C — Enable a specific policy for auto-renewal (per-VPS or per-host, your choice)

Edit `policy.json`: set `auto_renewal_enabled: true` at the narrowest scope you're
comfortable with — ideally one specific VPS first, not global.

## Step D — Unattended credentials (only if you want a systemd timer, not required for Step A)

```bash
python3 bin/renewal_cli.py store-credentials --node self
```

Always interactive, always writes to a 0600 file under `renewal-config/credentials/`, never
automatic.

## Step E — Enable the unattended renewal timer (highest risk — review carefully)

```bash
cat /opt/tixo-renewal/systemd/virt-renewal-apply.service   # read it yourself first
sudo cp /opt/tixo-renewal/systemd/virt-renewal-apply.* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now virt-renewal-apply.timer
```

This is the ONLY step that creates a truly unattended write path. Nothing before this point
does. Consider running Step A manually for at least one full billing cycle before this step.

**v1.10.3 convenience note:** once you've deliberately decided to proceed past Step A (i.e.
you're ready for Steps B, C, and E together, globally), `renewal_cli.py enable-auto-renewal`
(or the menu's "Enable automatic renewal" screen) performs exactly Steps B and E as one
confirmed action, plus setting `auto_renewal_enabled=true` globally -- it does NOT replace
the graduated, per-VPS approach above if you want that level of control; it's there for an
operator who has already decided on a global rollout and wants the three steps done
together rather than tracked separately (see installer.enable_auto_renewal's docstring for
exactly what it does).

## Rollback of this activation

```bash
sudo systemctl disable --now virt-renewal-apply.timer
rm /opt/tixo-renewal/renewal-config/WRITE_APPROVED
```

Removing `WRITE_APPROVED` alone is sufficient to block all further writes, even if the timer
is still enabled. Equivalently, `renewal_cli.py disable-auto-renewal` does both of these
together, plus setting `auto_renewal_enabled=false`.
