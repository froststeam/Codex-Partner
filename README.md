# Codex Partner

Codex Partner is a self-hosted control panel for long-running Codex work. It keeps one native Codex thread alive and lets terminals, browsers, and multiple devices interact with the same task.

![Codex Partner task workspace](https://raw.githubusercontent.com/froststeam/Codex-Partner/main/docs/codex-partner.png)

## Why

- **Long tasks stop too early.** Persistent Goals can resume after a failed turn, disconnect, or service restart until the objective is complete.
- **Multiple clients split the session.** Every connected browser is a view of the same server-owned Codex thread, not a competing `resume` process.
- **Messages collide with active work.** New messages wait in a durable queue and enter the conversation only when their turn actually starts.

## Highlights

- Live task state, tool activity, context usage, and paginated history
- Goal Resume with pause, retry, and restart recovery
- Ordered message queue with manual dispatch
- Workspace browser, editor, uploads, downloads, and image previews
- Browser terminal that preserves its session when hidden
- Model and API provider management with health checks and failover
- Codex memories, Skills, archive, recycle bin, and SSH workspace support
- Default YOLO mode for newly created sessions

## Install

Requires Python 3.10+ and an authenticated [Codex CLI](https://github.com/openai/codex).

### PyPI

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install codex-partner
codex-partner
```

### From source

```bash
git clone https://github.com/froststeam/Codex-Partner.git
cd Codex-Partner
pip install .
cp .env.example .env
codex-partner
```

Open <http://127.0.0.1:8787>.

For access from other devices, set `CODEX_DASHBOARD_HOST=0.0.0.0`. Codex Partner signs browsers in with a username and password accepted by this server's SSH service; the password is verified by OpenSSH and is never stored. Put HTTPS in front of the service before entering an SSH password over an untrusted network.

## Docker

```bash
docker build -t codex-partner:0.0.2 .
docker run -d --name codex-partner \
  -p 8787:8787 \
  --add-host=host.docker.internal:host-gateway \
  -e CODEX_DASHBOARD_AUTH=ssh \
  -e CODEX_DASHBOARD_AUTH_SSH_HOST=host.docker.internal \
  -v codex-partner-data:/var/lib/codex-partner \
  -v codex-home:/home/codex/.codex \
  -v codex-workspace:/workspace \
  codex-partner:0.0.2
docker exec -it codex-partner codex login
```

## systemd

After completing the source installation above, stop the foreground process and install the user service:

```bash
mkdir -p ~/.config/systemd/user
cp codex-partner.user.service.example ~/.config/systemd/user/codex-partner.service
systemctl --user daemon-reload
systemctl --user enable --now codex-partner.service
systemctl --user status codex-partner.service
```

This service uses the current user's existing Codex login, configuration, memories, and Skills. The example assumes the repository is at `~/Codex-Partner`; edit the two paths in the unit if it is elsewhere.

View logs with `journalctl --user -u codex-partner.service -f`. To keep it running after logout, run `sudo loginctl enable-linger "$USER"` once.

## Use

Create a session, choose a workspace and model, then send a message. Messages sent during an active turn queue automatically. Set a Goal to let Codex Partner keep resuming unfinished work. Reopen the same session from any connected device to continue the same thread.

Type `/help` in the message input for commands. Press `` ` `` to toggle the terminal, `Shift+N` / `Shift+P` to switch sessions, and `Ctrl/Cmd+K` to focus or unfocus the composer.
