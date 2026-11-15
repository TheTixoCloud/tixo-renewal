# Investigating Virtualizor's native bandwidth reset (generic findings)

This project does not assume Virtualizor automatically resets bandwidth on a schedule it can
detect. During development, a real Virtualizor installation was inspected read-only
(`cat`/`grep`/`DESCRIBE`/`SELECT` only — no writes) to check for a provable native reset
signal. Summary of what was checked and found (host/IP details omitted — see your own
installation for the equivalent):

1. **Cron jobs**: Virtualizor's own `/etc/cron.d/virtualizor`-style entries were reviewed.
   One job (`calculate_bandwidth.php`, typically every few minutes) aggregates raw traffic
   counters into the VPS's `used_bandwidth` column — this is accounting, not a reset. No job
   observed was named or behaved like a dedicated bandwidth-reset task.
2. **Database schema**: the `vps` table has no `nextresetdate`/`bw_reset_day`-style column on
   the installation checked. The `plans` table's `bandwidth` field is a flat integer with no
   cycle/period metadata.
3. **Logs**: a `logs_vps`-style activity table logs suspend/unsuspend events (a VPS exceeding
   its quota) but never a "reset" or "renew" action type.
4. **Source code**: Virtualizor's PHP is typically IonCube-encoded on a stock install — it
   cannot be statically read to confirm or rule out an internal reset code path.
5. **Historical evidence**: on the installation checked, every historical quota change traced
   to a manual admin action (a receipt or a manual-fix record); no quota or usage change was
   ever observed without a corresponding human action.

**Conclusion used by this project:** absence of a detectable native reset mechanism, combined
with 100% of historical changes being manual, is the best-supported working hypothesis on a
typical installation — not proof. This project is therefore designed as an **explicitly
controlled renewal process** (you decide the policy, the software proposes/applies on a
verified billing anniversary) rather than one that tries to detect-and-reconcile a native
mechanism it cannot prove exists. If your installation differs (e.g. you find a real
`nextresetdate` column or a cron job that resets usage), update your policy and investigate
before enabling `auto_renewal_enabled` for the affected VPS.
