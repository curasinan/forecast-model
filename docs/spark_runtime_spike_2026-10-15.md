# The Gateway spike on the Spark (2026-10-11 and 2026-10-15)

The question of the spike: does IB Gateway run on the Spark (aarch64) inside a
container with IBC and a virtual display, and can the account holder log in to
it through a remote screen over the tailnet, with nothing left running at the
end? The runtime this prepares is described in `docs/spark_runbook.md`
section 7.

**Verdict: GO.** All nine checks passed. The project that passed is pinned in
`deploy/spark/ibgw/`.

## When, how long

- 2026-10-11, 21:54 to 23:04: the user and the directories (made by hand
  earlier), the compose project, the first start, checks 1, 2, 7 and 9, and the
  stop half of check 8. Paused before the login at the account holder's
  request, to look at the security of the remote-screen path first.
- 2026-10-15, 15:20 to 23:05, with pauses: the owner rule again (the Spark had
  rebooted on 2026-10-12), the login, checks 3, 4, 5, 6, 7 and 9, the kill
  switch and the start after it. Hands-on time about three hours in all.

## Image

- Tag tried: `ghcr.io/gnzsnz/ib-gateway:stable`, pulled 2026-10-11.
- Digest, from `docker inspect` on the Spark and pinned in
  `deploy/spark/ibgw/docker-compose.yml`:
  `ghcr.io/gnzsnz/ib-gateway@sha256:1e2236804ac949b789197c225a048c0c891c9958f948c87166226e5b647ff581`
- Versions the container log prints before any login: IB Gateway
  `Version: 10.50.1f` (the parameter line IBC starts with), x11vnc `0.9.16`.
  The image prints its IBC version blank (`Starting IBC version  on ...`);
  the IBC build is the one inside this digest.

## The nine checks

| # | Check | Output line | Result |
| --- | --- | --- | --- |
| 1 | The image runs | `Up About a minute (healthy)`; `248.7MiB / 4GiB` before the login, `1.518GiB / 4GiB` after it; `aarch64`; `ss`: `127.0.0.1:4002` and `127.0.0.1:5900` only; from the Zenbook `Test-NetConnection` on 4002 and 5900: `False` over the tailnet and `False` over the LAN | pass |
| 2 | The remote screen opens | the Gateway's login dialog over `ssh -L 5900:127.0.0.1:5900` and a VNC viewer on `localhost:5900`, user and password fields empty | pass |
| 3 | The manual paper login | the Gateway's main window: `Interactive Brokers API Server: connected`, `Market Data Farm: ON: usfarm`; the phone was asked for a second factor and it was answered, so the weekly login needs the phone at hand (assumption A3: a prompt comes) | pass |
| 4 | The API answers `trading` | `server time: 2026-10-15 12:36:48+00:00`, `accounts seen: 1` (again at 12:40Z, 12:50Z and 18:39Z) | pass |
| 5 | One sweep lands | `sweep done: rows=2426 universe=2426 seconds=694.8`; `1 file(s), 2426 rows` under the scratch root (the Zenbook's last sweeps: `rows=2372` in 669-677 s). 2,303 rows carry a bid and ask; 123 do not (`Error 10197: No market data during competing live session`, which the Zenbook's own recorder log shows by the thousand on ordinary days: the account holder's phone app is the competing session) | pass |
| 6 | The auto-restart keeps the session | restart time set to `10:10 PM`; at 22:10:00 `IBC: detected dialog entitled: Restart in progress`, the Gateway relaunched itself with its restart token (`autorestart file found ... authentication will not be required`), `Found Gateway main window` at 22:10:04; at 22:12:31, with no login typed since 21:38: `after restart: 2026-10-15 19:12:31+00:00 True`. The container itself did not restart (RestartCount 0) | pass |
| 7 | The owner rule, from three sides | `trading: connected`; the operator user `rc=1`; `container: rc=1` (the Open WebUI container, host network); measured on both days, the second time after the rule was re-added by hand following the reboot | pass |
| 8 | The kill switch, and a start after it | `docker compose stop` -> `0` running containers, `after stop: rc=1`, `0` listeners on 4002 and 5900 (2026-10-11, and 2026-10-15 at 22:13 and 23:05). The start after it (22:53): `autorestart file not found: full authentication will be required`, then `Login dialog WINDOW_OPENED: LoginState is LOGGED_OUT`, the LOGIN DIALOG with empty fields; no autorestart file in the settings volume (the daily restart consumes it). The account holder then typed the login once more, which was not part of the check; the container was stopped again two minutes later | pass |
| 9 | The kill order | `ibgw -900`, `qwenflash -500` | pass |

## What the running image agreed with

- Every environment variable of the compose file was honoured as written:
  `TRADING_MODE=paper` (`IBC: Setting Trading mode = paper`), empty
  `TWS_USERID`/`TWS_PASSWORD` (the login dialog waits with empty fields),
  `AUTO_RESTART_TIME` (`Auto restart time already set to 10:10 PM`, and the
  restart came at that minute), `VNC_SERVER_PASSWORD` (asked by the viewer),
  `TWS_SETTINGS_PATH` with the `./jts` volume (the settings and the restart
  token live there), `TIME_ZONE`/`TZ`.
- The container port is `4004`: the image's socat relays it to the Gateway's
  own `4002` inside the container (its log line
  `socat ... connect(5, AF=2 127.0.0.1:4002)` while the Gateway is still
  starting), and the health check on `4004` goes green. So the host's
  `127.0.0.1:4002` -> container `4004` mapping is the right one, and the
  Gateway sees every API client as local.
- Docker's `userland-proxy` is at its default (`true`; there is no
  `/etc/docker/daemon.json`): two `docker-proxy` processes listen on
  `127.0.0.1:4002` and `127.0.0.1:5900`. The owner rule's `-o lo` match and the
  session check's port leg both rely on this.

## Other things seen

- A `Shutdown` from the Gateway's own window exits the container; docker
  (`restart: unless-stopped`) brought it back within a minute at the login
  dialog (`LoginState is LOGGED_OUT`, RestartCount 1). A restarted container
  never logs in by itself: the restart token exists only around the daily
  restart and is consumed by it.
- During that minute the VNC viewer reports that the server closed the
  connection before a session was established; reconnecting afterwards works.
- The VNC viewer flags the connection as "not secure": that is the VNC layer's
  own judgement. The viewer talks to the ssh client on the Zenbook's loopback,
  the tunnel runs inside the tailnet, and on the Spark the server is bound to
  loopback behind the owner rule; 5900 is reachable from nowhere else
  (check 1).
- `AUTO_RESTART_TIME` is read when the container starts: changing it means
  `docker compose up -d` (a recreate) and a new login. For the runtime it
  stays at `11:45 PM`.
- The Gateway log prints the restart token in clear around the daily
  restart. It is a credential for the next relaunch; it is never copied into
  a report, a ticket or a chat.
- Assumption A7 (the Spark powers on by itself when power returns, and which
  firmware setting says so): not known yet. The account holder answers it
  before the cutover evening; until then a power cut means a manual start of
  the Spark and a manual login.

## What was left behind

Nothing running: the container is stopped and stays stopped across a reboot,
`AUTO_RESTART_TIME` is back at `11:45 PM` in the operator's `.env`, the scratch
root of check 5 is removed, and the by-hand owner rule stays until the next
reboot (the unit `deploy/spark/systemd/ibgw-owner-rule.service` replaces it in
the installation). The Zenbook's Gateway is logged in again by the account
holder afterwards, and its recorder reconnects by itself.
