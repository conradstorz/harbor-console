# Reverse Proxy: Project Migrations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move every HTTP service on hpz440 behind Traefik and label every container, until the harbor-console page reports no findings.

**Architecture:** Each project's compose file drops its published port, joins the external `harbor` network, and declares itself with labels. Projects deployed from this Windows box redeploy with `docker --context hpz440 compose up -d`; projects checked out on the server redeploy there. Portainer and Watchtower were started with `docker run` and carry no compose file; they are recreated from a compose file in this repo so they can be labelled and preserved.

**Tech Stack:** Docker Compose, Traefik v3 labels. No Python changes.

**Prerequisite:** `2026-09-18-reverse-proxy-edge-and-page.md` complete through Task 12: Traefik is up on `100.69.239.123:443`, `harbor.hpz440.ohr3023.org` serves the page, and the findings list names every container below.

## Global Constraints

- Route hosts are `<name>.hpz440.ohr3023.org`. Every HTTP service gets `traefik.enable=true`, one router rule, one explicit `loadbalancer.server.port`, and joins `harbor`. Keep each project's existing networks; add `harbor` beside them.
- An HTTP service publishes no port after migration. TCP services keep exactly the port their `harbor.port` label names.
- Verify each migration three ways before moving on: `curl -sI https://<route>/` answers, the page row shows UP, and the page has no finding naming that container.
- Never chain commands with `&&`. Run Docker from this box with `--context hpz440`; never start local containers.
- Commit in the project's own repo. Messages end with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- If a service stops answering after migration, revert its compose change and redeploy before investigating; the page must never stay dark over a migration.

## Inventory (from `com.docker.compose.*` labels on 2026-09-18)

| Container | Compose file | Service | Deploy from | Route |
|---|---|---|---|---|
| parksmart-parksmart-1 | `ParkSmart/compose.yaml` | parksmart | this box | parksmart |
| schminternet | `Internet_Schminternet/docker-compose.yml` | schminternet | this box | schminternet |
| my_river_level-app-1 | `My_River_level/docker-compose.yml` | app | this box | river |
| gte-admin-1, gte-gte-1 | `GTE/docker-compose.yml` | admin, gte | this box, project name `gte` | gte |
| gte-db-1 | `GTE/docker-compose.yml` | db | this box | internal |
| arm-rippers-dev | `automatic-ripping-machine/docker-compose.hpz440-prod.yml` | arm-rippers-dev | this box | arm |
| shared-postgres-postgres-1 | `shared-postgres/docker-compose.yml` | postgres | this box | internal |
| ice-colder-vmc, ice-colder-mqtt, ice-colder-sim-* | `/home/gte/ice-colder/docker-compose.yml` | vmc, mosquitto, sim-* | server, user gte | ice; mqtt tcp; sims internal |
| imageharbor-imageharbor-1 | `/home/claude/imageharbor/docker-compose.yml` | imageharbor | server, needs sudo | images |
| portainer | none (`docker run`) | — | recreated from `harbor-console/deploy/hosted/compose.yaml` | portainer |
| watchtower | none (`docker run`) | — | same file | internal |

Note: the GTE containers were last deployed from a worktree at `GTE/.claude/worktrees/deploy-main` that no longer exists. Redeploying from `GTE/` with `-p gte` replaces them in place.

---

### Task 1: ParkSmart

**Files:**
- Modify: `D:\Users\Conrad\Documents\programming\ParkSmart\compose.yaml`

- [ ] **Step 1: Edit the service**

Replace the `ports:` block of `parksmart` with labels and a network. The file becomes:

```yaml
services:
  parksmart:
    build: .
    labels:
      traefik.enable: "true"
      traefik.http.routers.parksmart.rule: Host(`parksmart.hpz440.ohr3023.org`)
      traefik.http.services.parksmart.loadbalancer.server.port: "8000"
      harbor.description: "ParkSmart"
    networks:
      - harbor
      - default
    volumes:
      # (keep the existing volumes block unchanged)

volumes:
  parksmart-data:

networks:
  harbor:
    external: true
```

Keep every line not shown (volumes, environment) exactly as it is. Delete the two `ports:` lines.

- [ ] **Step 2: Redeploy**

```
docker --context hpz440 compose -f D:\Users\Conrad\Documents\programming\ParkSmart\compose.yaml up -d
```

Expected: `Container parksmart-parksmart-1  Recreated`.

- [ ] **Step 3: Verify**

```
curl -sI https://parksmart.hpz440.ohr3023.org/
docker --context hpz440 port parksmart-parksmart-1
```

Expected: an HTTP status line (any status); the second prints nothing. Then load `https://harbor.hpz440.ohr3023.org/`: row `parksmart` UP, no finding names `parksmart-parksmart-1`.

- [ ] **Step 4: Commit in ParkSmart**

```
git -C D:\Users\Conrad\Documents\programming\ParkSmart add compose.yaml
git -C D:\Users\Conrad\Documents\programming\ParkSmart commit -m "deploy: route through Traefik at parksmart.hpz440.ohr3023.org

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: Internet_Schminternet

**Files:**
- Modify: `D:\Users\Conrad\Documents\programming\Internet_Schminternet\docker-compose.yml`

- [ ] **Step 1: Edit the service**

Delete the `ports:` block (`- "8099:8080"`). Extend the existing `labels:` mapping and add networks:

```yaml
    labels:
      # Locally built image with no registry — nothing for watchtower to pull.
      com.centurylinklabs.watchtower.enable: "false"
      traefik.enable: "true"
      traefik.http.routers.schminternet.rule: Host(`schminternet.hpz440.ohr3023.org`)
      traefik.http.services.schminternet.loadbalancer.server.port: "8080"
      harbor.description: "Internet Schminternet"
    networks:
      - harbor
      - default
```

At the end of the file:

```yaml
networks:
  harbor:
    external: true
```

- [ ] **Step 2: Redeploy**

```
docker --context hpz440 compose -f D:\Users\Conrad\Documents\programming\Internet_Schminternet\docker-compose.yml up -d
```

- [ ] **Step 3: Verify**

```
curl -sI https://schminternet.hpz440.ohr3023.org/
docker --context hpz440 port schminternet
```

Expected: a status line; no published ports. Page: row `schminternet` UP, no finding for it.

- [ ] **Step 4: Commit**

```
git -C D:\Users\Conrad\Documents\programming\Internet_Schminternet add docker-compose.yml
git -C D:\Users\Conrad\Documents\programming\Internet_Schminternet commit -m "deploy: route through Traefik at schminternet.hpz440.ohr3023.org

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: My_River_level

**Files:**
- Modify: `D:\Users\Conrad\Documents\programming\My_River_level\docker-compose.yml`

- [ ] **Step 1: Edit the service**

Delete `ports:` and `- "5743:5743"`. Add under `app:`:

```yaml
    labels:
      traefik.enable: "true"
      traefik.http.routers.river.rule: Host(`river.hpz440.ohr3023.org`)
      traefik.http.services.river.loadbalancer.server.port: "5743"
      harbor.description: "River level"
```

Change the service's `networks:` list to:

```yaml
    networks:
      - shared-db
      - harbor
```

And the top-level `networks:` to:

```yaml
networks:
  shared-db:
    external: true
    name: shared-db
  harbor:
    external: true
```

- [ ] **Step 2: Redeploy**

```
docker --context hpz440 compose -f D:\Users\Conrad\Documents\programming\My_River_level\docker-compose.yml up -d
```

- [ ] **Step 3: Verify**

```
curl -sI https://river.hpz440.ohr3023.org/
docker --context hpz440 port my_river_level-app-1
```

Page: row `river` UP, no finding for `my_river_level-app-1`.

- [ ] **Step 4: Commit**

```
git -C D:\Users\Conrad\Documents\programming\My_River_level add docker-compose.yml
git -C D:\Users\Conrad\Documents\programming\My_River_level commit -m "deploy: route through Traefik at river.hpz440.ohr3023.org

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: GTE

**Files:**
- Modify: `D:\Users\Conrad\Documents\programming\GTE\docker-compose.yml`

- [ ] **Step 1: Edit three services**

`admin`: delete `ports:` and `- "8080:8080"`; add:

```yaml
    labels:
      traefik.enable: "true"
      traefik.http.routers.gte.rule: Host(`gte.hpz440.ohr3023.org`)
      traefik.http.services.gte.loadbalancer.server.port: "8080"
      harbor.description: "GTE console"
    networks:
      - harbor
      - default
```

`gte`: add

```yaml
    labels:
      harbor.kind: internal
      harbor.description: "GTE worker"
```

`db`: add

```yaml
    labels:
      harbor.kind: internal
      harbor.description: "GTE postgres"
```

Top level, add:

```yaml
networks:
  harbor:
    external: true
```

If `admin` already has a `labels:` mapping, merge into it. Check first: `grep -n "labels" D:\Users\Conrad\Documents\programming\GTE\docker-compose.yml`.

- [ ] **Step 2: Redeploy with the running project name**

```
docker --context hpz440 compose -p gte -f D:\Users\Conrad\Documents\programming\GTE\docker-compose.yml up -d --build
```

Expected: `gte-admin-1`, `gte-gte-1` recreated; `gte-db-1` recreated with labels (the data volume `gte_postgres` is named and survives).

- [ ] **Step 3: Verify**

```
curl -sI https://gte.hpz440.ohr3023.org/
```

Expected: `303` to `/login`. Log in from a browser: the Secure cookie now sets over HTTPS. Page: row `gte` UP, rows `gte-db-1` and `gte-gte-1` INTERNAL, no finding for any of the three.

- [ ] **Step 4: Commit**

```
git -C D:\Users\Conrad\Documents\programming\GTE add docker-compose.yml
git -C D:\Users\Conrad\Documents\programming\GTE commit -m "deploy: route the console through Traefik at gte.hpz440.ohr3023.org

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: automatic-ripping-machine

**Files:**
- Modify: `D:\Users\Conrad\Documents\programming\automatic-ripping-machine\docker-compose.hpz440-prod.yml`

- [ ] **Step 1: Edit the service**

Delete `ports:` and `- "100.69.239.123:49152:8080"`. Add:

```yaml
    labels:
      traefik.enable: "true"
      traefik.http.routers.arm.rule: Host(`arm.hpz440.ohr3023.org`)
      traefik.http.services.arm.loadbalancer.server.port: "8080"
      harbor.description: "Automatic Ripping Machine"
    networks:
      - harbor
      - default
```

Top level:

```yaml
networks:
  harbor:
    external: true
```

- [ ] **Step 2: Redeploy**

```
docker --context hpz440 compose -f D:\Users\Conrad\Documents\programming\automatic-ripping-machine\docker-compose.hpz440-prod.yml up -d
```

The image is `arm-dev:local`; no build step. Devices and volumes are unchanged.

- [ ] **Step 3: Verify**

```
curl -sI https://arm.hpz440.ohr3023.org/
docker --context hpz440 port arm-rippers-dev
```

Page: row `arm` UP, no finding.

- [ ] **Step 4: Commit**

```
git -C D:\Users\Conrad\Documents\programming\automatic-ripping-machine add docker-compose.hpz440-prod.yml
git -C D:\Users\Conrad\Documents\programming\automatic-ripping-machine commit -m "deploy: route through Traefik at arm.hpz440.ohr3023.org

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: shared-postgres

**Files:**
- Modify: `D:\Users\Conrad\Documents\programming\shared-postgres\docker-compose.yml`

- [ ] **Step 1: Add labels under `postgres:`**

```yaml
    labels:
      harbor.kind: internal
      harbor.description: "Shared postgres on the shared-db network"
```

- [ ] **Step 2: Redeploy**

```
docker --context hpz440 compose -f D:\Users\Conrad\Documents\programming\shared-postgres\docker-compose.yml up -d
```

`.env` beside the file supplies the passwords; the container is recreated, the `shared_pgdata` volume survives. River reconnects on its own (its healthcheck restarts it if not).

- [ ] **Step 3: Verify**

Page: row `shared-postgres-postgres-1` INTERNAL, no finding. `curl -sI https://river.hpz440.ohr3023.org/` still answers.

- [ ] **Step 4: Commit**

```
git -C D:\Users\Conrad\Documents\programming\shared-postgres add docker-compose.yml
git -C D:\Users\Conrad\Documents\programming\shared-postgres commit -m "deploy: declare the server internal to harbor-console

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: ice-colder (on the server)

**Files:**
- Modify: `/home/gte/ice-colder/docker-compose.yml` (server checkout) and the same file in `D:\Users\Conrad\Documents\programming\ice-colder` (the repo). Make the change in the local repo, commit, push, then `git pull` on the server.

- [ ] **Step 1: Edit five services**

This file uses list-form labels. `vmc`: delete `ports:` / `- "26123:26123"`; extend:

```yaml
    labels:
      - com.centurylinklabs.watchtower.enable=true
      - traefik.enable=true
      - traefik.http.routers.ice.rule=Host(`ice.hpz440.ohr3023.org`)
      - traefik.http.services.ice.loadbalancer.server.port=26123
      - harbor.description=Ice Colder VMC
    networks:
      - broker
      - harbor
```

`mosquitto`: keep `ports: - "1883:1883"`; add

```yaml
    labels:
      - harbor.kind=tcp
      - harbor.port=1883
      - harbor.description=MQTT broker
```

Each `sim-ice-maker`, `sim-mdb`, `sim-vending`: extend the existing labels list with

```yaml
      - harbor.kind=internal
```

Top-level networks:

```yaml
networks:
  broker:
    driver: bridge
  harbor:
    external: true
```

- [ ] **Step 2: Commit and push from the local repo**

```
git -C D:\Users\Conrad\Documents\programming\ice-colder add docker-compose.yml
git -C D:\Users\Conrad\Documents\programming\ice-colder commit -m "deploy: route the VMC through Traefik at ice.hpz440.ohr3023.org; label the rest

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
git -C D:\Users\Conrad\Documents\programming\ice-colder push
```

- [ ] **Step 3: Redeploy on the server**

```
ssh gte@hpz440 "cd /home/gte/ice-colder; git pull; docker compose up -d"
```

If `/home/gte/ice-colder` has local edits that block the pull, stop and report; do not force.

- [ ] **Step 4: Verify**

```
curl -sI https://ice.hpz440.ohr3023.org/
docker --context hpz440 port ice-colder-vmc
docker --context hpz440 port ice-colder-mqtt
```

Expected: a status line; nothing; `1883/tcp -> 0.0.0.0:1883`. Page: `ice` UP, `ice-colder-mqtt` LISTENING at `0.0.0.0:1883`, three sims INTERNAL, no findings for any.

---

### Task 8: ImageHarbor (on the server, needs sudo)

**Files:**
- Modify: `/home/claude/imageharbor/docker-compose.yml` (the deployed copy, owned by user `claude`, unreadable by `gte`) and `D:\Users\Conrad\Documents\programming\ImageHarbor\docker-compose.yml` (the repo, which publishes `8087:8080` rather than the tailnet-bound form the server runs).

- [ ] **Step 1: Read the deployed file**

```
ssh conrad@hpz440 "sudo cat /home/claude/imageharbor/docker-compose.yml"
```

Note every difference from the repo copy before editing. The change is the same in both:

- delete the `ports:` entry publishing 8087
- add under `imageharbor:`

```yaml
    labels:
      traefik.enable: "true"
      traefik.http.routers.images.rule: Host(`images.hpz440.ohr3023.org`)
      traefik.http.services.images.loadbalancer.server.port: "8080"
      harbor.description: "ImageHarbor"
    networks:
      - harbor
      - default
```

- top level:

```yaml
networks:
  harbor:
    external: true
```

- [ ] **Step 2: Apply on the server and redeploy**

```
ssh conrad@hpz440 "sudo -u claude -H bash -c 'cd /home/claude/imageharbor; docker compose up -d'"
```

If `claude` is not in the docker group, run `sudo docker compose up -d` from that directory instead.

- [ ] **Step 3: Verify**

```
curl -sI https://images.hpz440.ohr3023.org/
docker --context hpz440 port imageharbor-imageharbor-1
```

Page: `images` UP, no finding.

- [ ] **Step 4: Commit the repo copy**

```
git -C D:\Users\Conrad\Documents\programming\ImageHarbor add docker-compose.yml
git -C D:\Users\Conrad\Documents\programming\ImageHarbor commit -m "deploy: route through Traefik at images.hpz440.ohr3023.org

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 9: Portainer and Watchtower, recreated from a compose file

**Files:**
- Create: `deploy/hosted/compose.yaml` in harbor-console
- Modify: `deploy/install.sh` (bring it up beside Traefik)

Both containers were started by hand with `docker run` and carry no labels; labels cannot be added to a running container. Their configuration, read from `docker inspect` on 2026-09-18: Portainer `portainer/portainer-ce:latest`, `restart=always`, binds the Docker socket and volume `portainer_data:/data`, publishes `127.0.0.1:9443`. Watchtower `containrrr/watchtower`, `restart=always`, socket only, env `WATCHTOWER_LABEL_ENABLE=true WATCHTOWER_CLEANUP=true WATCHTOWER_SCHEDULE=0 0 4 * * *`.

- [ ] **Step 1: Write `deploy/hosted/compose.yaml`**

```yaml
# Host infrastructure that belongs to no project repo. Recreated from the
# `docker run` configuration found on 2026-09-18; the named volume
# `portainer_data` is reused, so Portainer keeps its state.
services:
  portainer:
    image: portainer/portainer-ce:latest
    container_name: portainer
    restart: always
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - portainer_data:/data
    networks:
      - harbor
    labels:
      traefik.enable: "true"
      traefik.http.routers.portainer.rule: Host(`portainer.hpz440.ohr3023.org`)
      traefik.http.services.portainer.loadbalancer.server.port: "9000"
      harbor.description: "Portainer"

  watchtower:
    image: containrrr/watchtower
    container_name: watchtower
    restart: always
    environment:
      WATCHTOWER_LABEL_ENABLE: "true"
      WATCHTOWER_CLEANUP: "true"
      WATCHTOWER_SCHEDULE: "0 0 4 * * *"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
    labels:
      harbor.kind: internal
      harbor.description: "Watchtower, nightly image updates"

volumes:
  portainer_data:
    external: true

networks:
  harbor:
    external: true
```

- [ ] **Step 2: Add to `deploy/install.sh`** immediately after the Traefik `docker compose up -d` line:

```bash
echo "==> Bringing up hosted infrastructure (Portainer, Watchtower)"
( cd "${INSTALL_DIR}/deploy/hosted" && docker compose up -d --remove-orphans )
```

- [ ] **Step 3: Remove the hand-run containers and deploy** (**sudo**, on the server)

```
docker --context hpz440 rm -f portainer watchtower
```

Then on the server:

```
cd /srv/harbor-console
git pull
sudo bash deploy/install.sh
```

- [ ] **Step 4: Verify**

```
curl -sI https://portainer.hpz440.ohr3023.org/
docker --context hpz440 port portainer
docker --context hpz440 volume inspect portainer_data -f "{{.Name}}"
```

Expected: a status line; no published ports; `portainer_data`. Log in to Portainer at the new route and confirm existing endpoints and users are intact. Page: `portainer` UP, `watchtower` INTERNAL, no findings.

- [ ] **Step 5: Commit in harbor-console**

```
git add deploy/hosted/compose.yaml deploy/install.sh
git commit -m "deploy: Portainer and Watchtower from compose, routed and labelled

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 10: Clean state

- [ ] **Step 1: Read the page**

`https://harbor.hpz440.ohr3023.org/` must show every row from the inventory and no findings of any kind. The page's own bind on `100.69.239.123:8100` is excluded by `own_port` in the edge plan; the old `tailscale serve` ports 443 and 8443 were released there too. Any finding that remains is a migration missed above: fix it in that project, not in `directory.py`.

- [ ] **Step 2: Remove stale artefacts on the host**

```
docker --context hpz440 ps -a --filter status=exited --format "{{.Names}}\t{{.Status}}"
```

`arm-dev` (exited 7 weeks, held 8090 when running) and `arm-rippers` (exited 2 months) are the old ARM containers; `flamboyant_ardinghelli` is an anonymous exit. Remove them only if the user confirms; they are not findings because they are not running.

- [ ] **Step 3: Record**

Update memory `hpz440-deploy-checkout.md` with: routes live at `*.hpz440.ohr3023.org`; the page is at `harbor.`; the edge and hosted infra deploy from `install.sh`.

---

## Self-review

**Spec coverage.** Every container in the section 3 table has a task; `retirement-planning` and `fastapi-docker` are not running and correctly have none. Portainer's backend is its plain 9000 port as the spec says. MQTT keeps its port under `harbor.kind=tcp`. Serve fronts were removed in the edge plan.

**Consistency.** Router label names match the spec's route table: parksmart, schminternet, river, gte, arm, ice, images, portainer, harbor (file provider). Every HTTP block sets `loadbalancer.server.port` explicitly. Label syntax matches each file's existing form (mapping vs list).

**Known risks.** ImageHarbor's deployed compose differs from the repo copy; Task 8 reads it before editing. GTE's last deploy came from a deleted worktree; Task 4 pins `-p gte` so the recreate replaces rather than duplicates. The page's own bind is excluded in the edge plan, so a clean page is a genuine clean page.
