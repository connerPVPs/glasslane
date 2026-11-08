# glasslane (proof of concept)

Ephemeral, single-box **development/test** Minecraft server on a GitHub Actions
runner, exposed over the public internet with a raw TCP relay. This repository is
a small spike to validate the control-plane idea behind Glasslane: a website that
hands each signed-in user one temporary dev box.

**This is a dev/test harness, not a production hosting service.** One job = one
box, hard 5-hour cap (`timeout-minutes: 300`), concurrency limited to 1, and
nothing is persisted when the job ends. It exists to develop and test the panel,
file browser, console stream and DNS lifecycle.

## How it works

`workflow_dispatch` → one `ubuntu-latest` job that:

1. creates a workspace directory
2. downloads a Paper jar and boots a Minecraft server inside it
3. starts a [bore](https://github.com/ekzhang/bore) raw-TCP tunnel to `bore.pub`
4. self-checks the server through the relay and prints the public address
5. holds the box for the requested number of minutes, then tears everything down

The final product will replace step 3/5 with a Worker that creates a per-user
`SRV` record on a custom domain and a Durable Object alarm that deletes it at
expiry. This PoC deliberately keeps that part manual.

## Run it

Actions → **devlane** → Run workflow. `duration_minutes` defaults to 8 (the
design limit is 300). The job log prints `GLASSLANE_ADDRESS=bore.pub:PORT`.
