# Writer Request Shape vs. Documented API (2026-11-14)

- `listvs` (read) and `managevps` (write, `bandwidth`+`vpsid`) are both officially documented
  Virtualizor Admin API actions — matches this project's `writer.py`.
- `listvs`'s real response shape is now **empirically confirmed** (not just documented) via a
  live, read-only `diagnose-api` call: top-level keys `servers`/`stats`/`time_taken`/`timenow`/`title`, HTTP 200, `Content-Type: text/html` (not `application/json` — a real quirk; classification no longer depends on it).
- **Open discrepancy**: `theme_edit=1`/`editvps=1` in our `managevps` POST body are **not** in
  the documented parameter list. They come from `/root/virt-api-test.py`, a real script that
  has actually applied production quota changes successfully. Kept as-is — empirical proof on
  this exact server outweighs a generic doc summary, but removing them later should only be
  done after testing, not assumed safe.

Sources: [List Virtual Servers](https://www.virtualizor.com/docs/admin-api/list-virtual-servers/), [Manage VPS](https://www.virtualizor.com/docs/admin-api/api-manage-vps/)
