# Ratatosk

A self-hosted dashboard for the containers on one Docker host. It shows every container
with its state, health, ports, CPU and memory, lets you start, stop and restart them, and groups
them into panels you arrange yourself. One container, the Docker socket and a data volume are all
it needs.

*Named after the squirrel in Norse myth that runs up and down the world tree carrying messages
between the realms.*

- Lists every container with state, health, image, published ports, CPU and memory
- Start, stop and restart any container, and delete a stopped one (admins only)
- Live logs per container (click a row, then **Logs**)
- Panels: host CPU, memory and storage, link tiles, one panel per app and one per group of
  containers. Arrange them in columns, drag them around and hide the ones you don't
  need (**Customize**)
- Link tiles: a "go to" tile for every running container that publishes a port
- The browser tab shows the overall status: a green, amber or red dot and the count (for example "11/12 · Ratatosk")
- Update badges and an Update button for Docker Compose containers
- Optional panels for apps it knows (see [Integrations](#integrations)), and a YAML format for your own
- A **Debug** button that shows what Ratatosk itself is doing (app requests,
  key steps, sign-ins, errors), so you don't need shell access to find out why a panel is empty
- Groups: everything starts in "Ungrouped" (or in its Docker Compose project, if you switch that on), and you
  can drag containers into groups of your own
- Users: an admin adds people as admins or operators, and everyone arranges their own page

**Contents:** [Quick start](#quick-start) · [Updating](#updating-ratatosk) · [Backup and restore](#backup-and-restore) ·
[Uninstall](#uninstall) · [Troubleshooting](#troubleshooting) · [Configuration](#configuration) ·
[Security](#security) · [Debug](#debug) · [Panels and layout](#panels-and-layout) · [Users](#users) ·
[Groups](#groups) · [Link tiles](#link-tiles) · [Updates](#updates) · [Integrations](#integrations) ·
[Development](#development) · [License](#license)

## Quick start

From nothing to a working page, in order.

### What you need

- A Linux host running Docker Engine with the Compose plugin. Check with `docker compose version`.
- `git`, to download the code.
- Internet access for the first build (it pulls the Python base image and Docker's own CLI image).
- Port 8090 free on the host (or choose another in step 3).
- A browser on the host itself, or one that can reach it once you open the port up (step 3). Keep
  Ratatosk on a private network, see [Security](#security).

### 1. Download

```
git clone https://github.com/Gressklipper173/ratatosk.git
cd ratatosk
```

### 2. Make your own compose file

```
cp docker-compose.example.yml docker-compose.yml
```

`docker-compose.yml` is ignored by git, so your changes survive a later `git pull`.

### 3. Adjust it (optional)

Open `docker-compose.yml`. The defaults work as they are. The usual changes are:

- **The port and who can reach it.** The example publishes `"127.0.0.1:8090:8090"`, so the page opens
  only from the host itself. To reach it from other devices on your network, change it to
  `"8090:8090"` (every interface) or put the host's LAN or VPN address in front, for example
  `"192.168.1.10:8090:8090"`. Docker's published ports bypass the host firewall (UFW, firewalld), so do
  that only on a private network. Change the left port number to serve on another port.
- **Read-only mode.** Set `READ_ONLY: "true"` if Ratatosk should only look (see [Configuration](#configuration)).
- **Extra mounts**, only if you want the Update button or the storage meters to see more of the host.
  The file has commented examples, and the sections [Updates](#updates) and
  [Storage meters](#storage-meters) explain them. You can add them later. For the Update button each
  Compose project's folder must be mounted at the **same path as on the host**
  (`/home/me/media:/home/me/media:ro`); the storage-meter mount is a different thing and does not
  count. See [Making the Update button work](#making-the-update-button-work).

Don't set `hostname:` or `network_mode: host` on the service. Ratatosk finds its own container by
the hostname Docker gives it, and needs that to avoid offering to stop itself.

### 4. Start it

```
docker compose up -d --build
```

The first build takes a few minutes. When it is done, `docker compose ps` shows `ratatosk` as running.

### 5. Read the one-time setup token

```
docker logs ratatosk 2>&1 | grep setup
```

It prints a line like `[setup] No users yet. Open /setup and use token: ...`. A new token is made each
time Ratatosk starts while no admin exists, so if you lose it, `docker restart ratatosk` and read it again.

### 6. Create the admin account

Open `http://<host>:8090/setup`, where `<host>` is the name or address of the machine (use `localhost` if
you are on it, which is the only way in until you open the port up in step 3). Enter the token, a username (letters, digits, `.`, `_` and `-`, up to 32 characters) and a
password of at least 8 characters, then press **Create admin**. You are signed in. From now on
`http://<host>:8090` shows the sign-in page.

Right after the account is created you see a "Searching for containers…" screen while Ratatosk reads the
container list from Docker. It goes away as soon as the list arrives, even if Ratatosk is the only container on the
host (the dashboard then just shows the host stats). If the list can't be read within 15 seconds, it asks
whether to **Proceed to the dashboard** or **Keep searching**, and shows the error. It only appears this once.

### 7. Look around

- **Host** panel: CPU, memory and storage of the machine.
- **Container groups**: your containers, in one **Ungrouped** group to begin with (drag them into groups
  of your own, or switch on **Group by Compose project** in Settings). Click a row (or its **⋯**
  button) for **Update** (when a newer image exists), **Logs**, **Restart** and **Stop**.
  Ratatosk's own row says "this instance" and can't be stopped from the page.
  A stopped container also has **Delete** (admins only). You first choose between deleting the
  container only (like `docker compose down`) and also its anonymous volumes (like `down -v`,
  for that one container), then confirm what will happen. Named volumes and mounted folders are
  never deleted. Pelican game servers (owned by Wings) and Ratatosk itself can't be deleted here.
- **Browser tab**: a coloured dot and a count show the overall status.
- **Customize** (page header): drag panels and containers where you want them, rename containers,
  hide panels, add groups. Press **Done** when finished.
- **Cog** (page header): [Settings](#panels-and-layout) for columns and how often the page refreshes,
  your own password, and (for admins) users.

### 8. Next steps

All optional:

- Add people: **Settings → Users** ([Users](#users)).
- Mark which containers matter: [Priority](#priority).
- Show data from your apps and see which are supported: [Integrations](#integrations).
- Get "go to" tiles: [Link tiles](#link-tiles).
- Make the Update button work: [Updates](#updates).

## Updating Ratatosk

```
cd ratatosk
git pull
docker compose up -d --build
```

Your accounts, layouts and keys live in the `ratatosk-data` volume and are kept. Reload the page
with a hard refresh (Ctrl+Shift+R) afterwards so the browser doesn't use old scripts. Ratatosk never
updates its own container; the Update button is for your other containers.

## Backup and restore

Everything Ratatosk stores is in one Docker volume. Compose puts the project name in front, so find
its real name first:

```
docker volume ls --filter name=ratatosk-data
```

It is usually `ratatosk_ratatosk-data` (the folder name, then the volume name). The image itself
declares `VOLUME /data`, so a bare `docker run` without `-v` keeps the data in an anonymous volume
that is easy to lose; the compose file names it. Back it up
(replace `VOLUME` with the name from above):

```
docker run --rm -v VOLUME:/data:ro -v "$PWD":/backup alpine tar czf /backup/ratatosk-data.tgz -C /data .
```

Restore into the same or a new volume:

```
docker compose down
docker run --rm -v VOLUME:/data -v "$PWD":/backup alpine sh -c "cd /data && tar xzf /backup/ratatosk-data.tgz"
docker compose up -d
```

The archive holds password hashes and any app keys you stored, and they are **not encrypted**. Keep it private.

## Uninstall

```
docker compose down
```

removes the container and keeps your data. To also delete accounts, layouts and stored keys, which cannot
be undone:

```
docker compose down -v
```

Then remove the folder if you like.

## Troubleshooting

- **The page doesn't open.** Check `docker compose ps` and `docker logs ratatosk`. If the port is taken,
  change the left number in `ports:` and run `docker compose up -d`. The example file publishes the
  port on `127.0.0.1` only, so another device needs the `ports:` line opened up (step 3 of the quick
  start), and then the host's firewall may matter.
- **I lost the setup token.** Run `docker restart ratatosk` and read the log again (step 5). A token is
  only made while no admin exists.
- **I forgot my password.** Another admin can reset it under **Settings → Users**. If you are the only
  admin, run `docker exec ratatosk rm /data/users.json` and then `docker restart ratatosk`. That brings
  back the setup page with a new token. Create the admin again; layouts and stored app keys are kept.
- **"Too many attempts."** Sign-in is limited to 5 failures in 5 minutes per address. Wait, or
  `docker restart ratatosk`. Behind a reverse proxy see `BEHIND_PROXY` in [Security](#security).
- **The container list is empty or shows a Docker error.** Ratatosk needs `/var/run/docker.sock`
  mounted. On a host with SELinux, if it still can't read the socket, add
  `label=disable` to the service's `security_opt:` list (the example file already has one).
- **The page looks old after an update.** Hard refresh (Ctrl+Shift+R).
- **The Update button is greyed out, tooltip says "compose file not visible to Ratatosk".** Mount that
  project's folder in Ratatosk's compose file with the same path on both sides, for example
  `/home/me/media:/home/me/media:ro`, then recreate Ratatosk. How to find the folder, and why
  `/home:/host/home` does not count: [Making the Update button work](#making-the-update-button-work).
  If the tooltip says "this image has no docker CLI", the image was built without it; build from this
  repository's Dockerfile.
- **An app's panel says it could not reach the app, or shows nothing.** See [Integrations](#integrations);
  the **Debug** button in the page header shows each request and why it failed.

## Configuration

Set these under `environment:` in your compose file.

| Variable    | Default | Meaning                                  |
|-------------|---------|------------------------------------------|
| `READ_ONLY` | off     | Disable start/stop/restart/update/delete, app keys, game-server config files and user changes. Layouts, dismissing apps, the Debug detail level and your own password stay available |
| `UPDATE_CHECK` | on   | Set `false` to stop asking registries about newer images |
| `BEHIND_PROXY` | off  | Set `true` when a reverse proxy or tunnel is the only way in: the login throttle and the Debug log then use the visitor's real address, and the `Secure` cookie flag follows `X-Forwarded-Proto` |
| `PROXY_IP_HEADER` | `xff` | With `BEHIND_PROXY`: where the visitor's address comes from. `xff` is the last `X-Forwarded-For` entry, which is the address your proxy appended (check that yours does). `cf` is Cloudflare's `CF-Connecting-IP`; **Cloudflare users must set `cf`**, since the last `X-Forwarded-For` entry is not reliably the visitor there. Only the chosen header is read, so your proxy must set or overwrite it and not pass the visitor's copy through. Any other value is treated as `xff` |
| `DATA_DIR`  | `/data` | Where users, keys, layouts and the session key are kept. Leave it and mount a volume there |
| `DOCKER_HOST` | the socket | Where the Docker API is, if not the mounted socket (see [Security](#security) before using a socket proxy) |
| `HOSTNAME` | set by Docker | Docker sets it to the container's short id, and Ratatosk uses it to recognise its own container (so it never offers to stop itself). Don't override it; see `hostname:` in [Quick start](#3-adjust-it-optional) |
| `DISABLE_SAMPLER` | off | Set to `1` to run without the background threads (stats sampler, update checker) and refresh Pelican data inline. Meant for the test suite, not for normal use |

Ratatosk listens on port 8090 inside the container; change the left number of `ports:` to serve on another
port. Everything it stores is in `DATA_DIR`:

| File | Holds |
|------|-------|
| `users.json` | Accounts (password hashes) and roles |
| `integrations.json` | App keys and logins you entered (mode 0600, not encrypted; Ratatosk runs as root, so the mode does not keep other root processes or anyone with Docker access out) |
| `groups/` | One file per user: layout, groups, names, priorities, storage meters |
| `secret_key` | Signs the session cookies |
| `adapters/` | Your own app adapters, if you mount some |

## Security

- Mounting `/var/run/docker.sock` gives Ratatosk (and anyone who logs in to
  it) root-equivalent control of the host. **Do not expose it to the public
  internet.** Put it behind a VPN such as Tailscale or WireGuard.
- The example compose file publishes the port on `127.0.0.1` only. Opening it to other devices
  is your choice; Docker's published ports bypass the host firewall.
- Set `READ_ONLY=true` to disable the actions that change containers, app keys, game-server config
  files and users. Saving your layout, dismissing and restoring app notices, the Debug detail level
  and changing your own password stay allowed, because they change only what is displayed or your
  own account.
- A socket proxy gives little protection here. Ratatosk needs to start, stop and delete containers,
  read logs, stats and files (archive reads), look up registries, and run `docker compose pull` and
  `up -d`. A proxy that lets those container POST calls through generally lets `containers/create`
  through too, and creating a privileged container is a way to take over the host. `DOCKER_HOST`
  still works if you want to point Ratatosk at a Docker API elsewhere, but don't count on a proxy
  to make it safe.
- Ratatosk runs as root inside its container (it needs the socket), so file modes such as 0600 on
  its data do not protect anything from root. The example compose file sets
  `no-new-privileges`, which is a small extra guard, not a sandbox.
- Behind a reverse proxy or tunnel, set `BEHIND_PROXY=true` and make sure nothing else can reach
  Ratatosk's port, because the address then comes from a header. Without it every visitor
  looks like the proxy, so five failed logins by anyone lock everyone out. Set `PROXY_IP_HEADER` to
  the header your proxy overwrites: `xff` (default) for most proxies, `cf` for Cloudflare. If the
  proxy passes the visitor's own copy of that header through, anyone can forge their address and
  get past the login throttle. The session cookie is marked `Secure` when Ratatosk
  sees the request as https: a direct https connection, or, with `BEHIND_PROXY=true`, a proxy that
  sends `X-Forwarded-Proto: https`. A proxy that ends TLS and forwards plain http *without*
  `BEHIND_PROXY` leaves the cookie without `Secure`. Ratatosk
  is meant for a private network. If you publish it, put an access layer with MFA in front of it
  (for example Cloudflare Access); its own sign-in has no second factor.
- Container logs can contain secrets. They are only shown to signed-in users,
  and stay visible in `READ_ONLY` mode.
- Logins are throttled per IP (changing your own password counts too), and first-run setup
  requires a token that is only printed to the container logs. The sign-in and setup forms carry a
  CSRF token, and signing out is a POST with one.
- A sign-in lasts 12 hours from your last request. A page left open keeps itself signed in; an
  idle browser is signed out.
- Every page and API answer carries a Content-Security-Policy (scripts and styles only from
  Ratatosk itself, no framing), `X-Frame-Options: DENY` and `X-Content-Type-Options: nosniff`, and the
  pages and API answers are marked `no-store`.
- If `users.json` is damaged (not valid JSON, or not an object), Ratatosk refuses to start instead of
  treating it as empty, which would re-open the setup page to anyone who reaches it. If it
  breaks while running, requests answer 503 and the Debug panel and container logs name the file.
  Restore it from a backup. (A missing file is the normal first-run state.)
- `GET /healthz` needs no sign-in. Once an account exists it also asks Docker, and answers 503 when
  the socket is missing or unusable, so the Docker healthcheck shows the problem.
- Roles limit who can change app keys and users, see [Users](#users).

## Debug

**Debug** in the page header opens a console with Ratatosk's own activity: each request
to an app (method, address, status, time), each key or login step (found, tested, stored,
removed), sign-ins, container actions, updates, and errors with their tracebacks. It is
always recording, and kept small on purpose: in memory only, the newest 2000 lines (about
half a megabyte), and a restart clears it. Repeated lines are counted instead of repeated.

- **Detail:** *Normal* keeps the useful lines (unexpected replies, failures, key steps).
  *Verbose* also records every successful request and every address that did not answer.
  The setting resets to Normal when Ratatosk restarts.
- Keys, passwords, request bodies and reply bodies are never recorded, and neither is the
  query string of a request. Anything stored as a credential is also masked if it ever
  turns up in a message.
- Only admins can open it or change its detail level (operators don't see the button), also in
  `READ_ONLY` mode. Lines can contain container names and addresses.
- The same lines go to the container's own output (`docker logs`). Docker keeps that without
  limit unless you tell it otherwise, so `docker-compose.example.yml` sets
  `max-size: 10m` and `max-file: 3`. Keep those lines in your own compose file.

## Panels and layout

Everything on the page is a panel: **Host** (CPU, memory and storage), **Links**, one panel for
each app Ratatosk has an adapter for, and one for each group of containers.
Panels sit in columns: three on a wide screen, two on a tablet, one on a phone.
**Settings** (the cog in the page header) → **Columns** sets the most columns this browser may
use, from 1 to 5 (default 3). A narrow window still shows fewer: about 720 px fits two, 1100
three, 1480 four and 1860 five. The setting is per browser; each column count keeps its own
arrangement.

Click **Customize** to rearrange (the button becomes **Done** in the same place; press it to
finish). Drag a panel by its grip (or press and hold on a touch screen) to any column and
position, or focus the grip and use the arrow keys. The eye
hides a panel; hidden panels wait in the **Hidden panels** shelf of the edit bar, where
**Show** puts them back. Each width keeps its own arrangement, so arranging on your phone
never changes your desktop. The first time you open a width, its arrangement is saved as it
appears, so an app or group added later goes to the bottom instead of moving your panels. The layout is saved on the server, per user (`/data/groups/`), so
each person's devices see the same panels and the same hidden ones. The arrow in a panel's header
folds it; folded panels (and **Collapse all** / **Expand all** in the page header) are remembered per
browser. Layout editing stays available in `READ_ONLY` mode, since it changes only how
things are displayed.

### How fresh the page is

**Settings** (the cog) has two separate timers, kept in this browser, so a phone and a desktop can
differ:

- **Containers:** how often the container list and host vitals are fetched. Every 2 s, 5 s
  (default), 15 s or a minute.
- **App panels:** how often the panels that show an app's own data are fetched. Every 10 s, 30 s
  (default), a minute or 3 minutes.

The server keeps panel data for as long as the page's interval, so a faster setting is not answered
from an older copy. A tab that was in the background refreshes as soon as you return to it. Faster
refreshes mean more calls to your apps (for example, a Pelican panel with a user key is asked about
each game server).

### Storage meters

The **Host** panel shows a meter for each disk you choose. In **Customize**, press **Add disk** on
the Host panel, pick one of the disks Ratatosk can see (with its size and how full it is),
give it a name such as *Home*, and press **Add**. Add as many as you like; **Rename** and
**Remove** are on each meter while editing. The list is saved with the layout, so every device
shows the same disks. Until you add one, the panel shows Ratatosk's own disk, which is the one
Docker keeps its storage on.

A container can't see the host's disks by itself, so the picker lists only what has been mounted
into it. By default that is the small disk Docker keeps Ratatosk's own data on, **not** your big
data disks. To show another disk, mount any folder that lives on it into Ratatosk, read-only is
fine (only the disk's size and free space are read, never the folder's contents). Find the disk
with `df -h` on the host: the **Mounted on** column is the folder to use. For a disk mounted at
`/home`, for example:

```yaml
volumes:
  - /home:/host/home:ro
```

Add the line under the `volumes:` of the `ratatosk` service, then recreate Ratatosk with
`docker compose up -d` (no `down` needed; your data volume is kept). **Add disk** then lists
it, with its real size. If the Compose
project folders you mounted for the Update button sit on another disk, that disk is already
listed.

## Users

The account made at `/setup` is an admin. **Settings** (the cog) → **Users** lists everyone;
only admins see it. There are two roles:

| Role | Can do |
|------|--------|
| Admin | Everything an operator can, plus add, remove and change users, reset passwords, find, enter or remove app keys, update and delete containers, and use the Debug panel |
| Operator | Look at everything except the Debug log, start, stop and restart containers, edit the config files of game servers, and arrange their own page. Cannot manage users or app keys, update or delete containers, or open Debug |

- **Passwords:** an admin sets a password when adding a user and can reset it later. There is no
  email, so a forgotten password is reset by an admin. Everyone can change their own under
  **Settings** → **Account**. Passwords are at least 8 characters and stored hashed.
- **The last admin** can't be removed or made an operator.
- **Role changes and removals apply at once**, also to a person who is signed in.
  Changing or resetting a password signs that account out everywhere else; the device that changed it
  stays signed in. A session also ends after 12 hours without a request.
- **Layout and groups are per user:** panels, groups, display names, link tiles and storage
  meters belong to the account. A user who hasn't saved anything yet starts from the shared
  `groups.json` of earlier versions, so an existing page doesn't change; their first change
  saves their own copy. Removing a user deletes their copy. The column cap and refresh
  timers stay per browser.
- `READ_ONLY=true` also switches off adding, changing and removing users. Changing your own
  password stays available.
- Existing `users.json` files keep working: accounts made before roles existed are admins.

## Groups

On a new account every container starts in "Ungrouped" (which only appears while a container is in
it). To start from each container's Docker Compose project instead, switch on **Group by Compose
project** under **Settings** (the cog) → **Groups**. That setting is saved with your account, and
containers you placed in a group yourself stay where you put them. Accounts that existed before
the setting was added keep grouping by project until you switch it off. In
**Customize**, drag a container onto a group panel to move it, drop it on the "new
group" zone to start one, or press **Add group**. Each group panel then offers
**Rename**, **Delete** (user-made groups only), **Priority** and **Generate links**.

In edit mode each container also has a **Rename** button for giving it a friendly
display name (handy for containers named by a UUID). This is display-only: the
container itself is never renamed, its real name stays in the row's grey line, and
clearing the field restores it.

### Priority

Priority says how much it matters when a container is down. It decides the colour of the
overall status at the top of the page and in the browser tab:

| Priority | When a container is down |
|----------|--------------------------|
| Critical | The overall light turns **red** |
| Wanted (default) | It turns **amber** |
| On demand | Nothing: it is meant to be stopped sometimes (game servers), so it is grey and left out of the count |

Set a default for a group with **Priority** on its panel, and change a single container with
the **Priority** choice on its row (**Group** follows the group). A container that is starting,
restarting, paused or unhealthy turns the light amber, or red if it is Critical and unhealthy.
Groups marked On-demand in earlier versions stay On-demand.

Each row shows the container's state, image, ports, CPU and memory. Click a row (or its
**⋯** button) to show **Update** (when a newer image exists), **Logs**, **Restart** and **Stop**.

## Link tiles

Link tiles are off by default. In **Customize**, press **Generate links** on a group
and each of its running containers that publishes a TCP port gets a tile in the **Links** panel. The link uses
the host name from your browser's address bar, so it works over a LAN, Tailscale or
a domain with no configuration. Ports bound only to `127.0.0.1` and UDP ports are
ignored; if several ports are published, a usual web port (80, 8080, 3000, ...) is
preferred. While editing, each tile also has a Hide/Show button, and the pencil on a
container's row renames its tile too. **Generate links** is the default for a group; a
container's **Link tile** choice on its row overrides it: **Show** gives it a tile although its
group has none, **Hide** removes it, and **Group** follows the group.

Container labels override the guess:

| Label                | Effect                                                     |
|----------------------|------------------------------------------------------------|
| `ratatosk.name`      | Name shown for the container (a rename in the UI wins)     |
| `ratatosk.url`       | Link to this `http(s)` URL instead of the published port   |
| `ratatosk.hide`      | `true` removes the tile                                    |
| `ratatosk.adapter`   | An app adapter id to use whatever the image is called, or `none` to hide the app's panel (see [Integrations](#integrations)) |

The older `dashboard.name`, `dashboard.url`, `dashboard.hide` and `dashboard.adapter` still work;
if both are set, the `ratatosk.` one wins.

## Updates

Every 30 minutes Ratatosk asks each Compose container's registry whether its
image tag now points at something newer than what is running. A container with a newer
image gets an **Update** button. It asks for confirmation (apps with a database can
migrate it on first start, so back up first), then runs `docker compose pull` and
`up -d` for that one service.

Containers not started by Docker Compose, locally built images, and Ratatosk itself
never show an update. The registry check sends the image name to its registry (Docker Hub
rate-limits anonymous requests); set `UPDATE_CHECK=false` to turn it off, which also
removes the button.

### Making the Update button work

Ratatosk runs `docker compose` from inside its own container, so it has to see each project's
Compose files at **the same path as on the host**. Without that the button stays greyed out and its
tooltip says `compose file not visible to Ratatosk`, followed by the file it looked for. You do this
once per Compose project that has containers you want to update:

1. **Find the project's folder.** The tooltip already names the file, so the folder is the part
   before the file name. You can also ask Docker, using any container from that project:

   ```
   docker inspect <container> --format '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}'
   ```

   If the project was started with `-f` or `--project-directory`, the label
   `com.docker.compose.project.config_files` lists the files themselves.
2. **Mount that folder in Ratatosk's compose file, with the same path on both sides of the colon**,
   read-only:

   ```yaml
   volumes:
     - /home/me/media:/home/me/media:ro
   ```

3. **Recreate Ratatosk** (`docker compose up -d` in its own folder) and reload the page.

Things that trip people up:

- **A different mount of the same disk does not count.** The disk-meter example
  `/home:/host/home:ro` puts the files at `/host/home/...`, not `/home/...`, so Ratatosk still cannot
  find the compose file at the path Docker recorded. Add the project's own folder as above.
- **Mount the folder, not just the file.** Compose also reads the `.env` file and any relative paths
  next to the Compose file.
- **The folder becomes readable by Ratatosk**, including that project's `.env` and anything else in
  it. Ratatosk can already control every container through the Docker socket, so this adds little,
  but keep secrets you don't want it to read in a folder that is not mounted.
- **Several Compose files in one project** (a base file plus overrides) must all be reachable at their
  recorded paths. Mounting the folder they sit in covers that if they share one.
- **On a host with SELinux**, if the file still isn't visible after mounting, see the SELinux note in
  [Troubleshooting](#troubleshooting).
- **An update badge can outlive the update.** If you updated the container by hand, the badge clears
  after the next registry check (every 30 minutes).

## Integrations

Apps Ratatosk has an adapter for are listed together in one **Apps found** panel, one collapsed row per
app. Open a row to set that app up; once it is set up it leaves the list and gets a panel of its own.
Ratatosk never reads or stores an app's API key or password on its own. You choose **Find key
automatically** (it reads one named file inside the container, after a confirmation),
**Enter key manually**, or **Dismiss** (undo with **Restore** above the panels, or with **Restore dismissed
integrations** in **Settings**). The key is checked against the app before it is stored in
`/data/integrations.json`, and it is never sent to the browser. That file is readable only by
Ratatosk's user and is **not encrypted**. `READ_ONLY=true` switches off finding, saving
and removing keys.

### Supported apps

| App | Shows | Credentials |
|-----|-------|-------------|
| Sonarr | Missing episodes | API key: find automatically or enter by hand |
| Radarr | Missing movies | API key: find automatically or enter by hand |
| Seerr (also Jellyseerr, Overseerr) | Requests awaiting approval, and requests not yet available | API key: find automatically or enter by hand |
| qBittorrent | Active downloads, with progress and speeds | Username and password, entered by hand (qBittorrent stores the password hashed, so it can't be looked up) |
| Pelican (panel) | Game servers with their real names, ports and live state; start, stop and restart through the panel | Up to two API keys (admin and user), entered by hand, each optional (see [Pelican](#pelican)) |

The panel for each app matches by image name. Set the label `ratatosk.adapter=<id>` on a container
to match an app with a custom image name, or `ratatosk.adapter=none` to hide its panel.

### Setup

1. **Make the app reachable from Ratatosk.** Docker keeps containers on different networks
   apart, and Ratatosk waits for a timeout when it can't connect. Pick one:
   - *Published port (simplest).* The app publishes its port on the host (for example
     `8989:8989`), and Ratatosk gets a route to the host. Add this to Ratatosk's
     service (it is in `docker-compose.example.yml`):
     ```yaml
     extra_hosts:
       - "host.docker.internal:host-gateway"
     ```
   - *Shared network.* Put Ratatosk on the same Docker network as the app, for example by
     adding that network under Ratatosk's `networks:` in your compose file. Ratatosk
     then reaches the app by its container address.

   Ratatosk tries every route at once and uses the first that answers. If none does, the
   panel says "could not reach the app".
2. **Recreate Ratatosk** so the change applies: `docker compose up -d`.
3. **Open Ratatosk.** The **Apps found** panel lists each matching container. Open the row of the one you
   want. The container must be running, because the key is checked against the live app.
4. **Add the key.** Press **Find key automatically** (the file it reads is named in the
   confirmation), or **Enter key manually** and paste the key from the app's settings.
5. **Done.** The app gets a panel with its data and its row leaves **Apps found**. **Remove key** (in
   **Customize**) puts it back in the list (step 3).

If the app's folder isn't where the adapter expects (for example, an app whose config is not at
`/config/config.xml`), use **Enter key manually**. An app that signs in with a username and
password instead of a key shows **Enter login**; use the credentials of its web interface.

Some apps need a setting of their own before they accept Ratatosk. For example, if qBittorrent
says the address was refused, check Tools > Options > Web UI: turn off "Enable Host header
validation", or add the name Ratatosk uses. Too many failed logins can also temporarily ban
Ratatosk's address, so after a wrong password Ratatosk stops trying that login for about five
minutes (enter the right one, and it logs in again at once).

### Pelican

Pelican's Wings names every game server's container by a UUID. With the Pelican panel set up
here, those containers show the server's real name, its port and its live state (offline,
starting, running, stopping), and Start, Stop and Restart go through the panel. That last part
matters: stopping a game server behind Wings' back looks like a crash to it, and it starts the
server again.

The **Pelican panel** on the page lists every game server with its port and state, and each row has **Start**, **Stop** and **Restart** (they appear once the user key is set).
Each row also shows the server's disk: how much it uses against what it was allocated, with a bar that turns amber from 85 % and red once over. Use needs the user key; the allocation comes with either key. A server with no disk limit shows just its size.
The container rows do the same.

The adapter takes two API keys. Pelican's API is split in two: the admin key manages the panel, and the user key acts as one user account, the same as pressing Start or Stop in Pelican's own page. Pelican stores them hashed, so you enter them by hand, and each
one is optional and switches on its own part:

| Key | Make it in the panel | Gives you |
|-----|----------------------|-----------|
| Admin (application) key | Admin > API keys, with read access to Servers | Every server on the panel, not just one account's. Optional if the user key already sees the servers you want |
| User key (Pelican calls it a *client* key) | Your account > API keys, in the Pelican panel | Names and ports of that account's servers, live state, and start/stop/restart. Enough on its own |

With only the user key, the list is that account's own servers. With only the admin key there
is no live state, and Start/Stop/Restart still go straight to Docker (the Debug panel notes it
each time). Add the other key later with **Add** on the panel. **Remove keys** (in **Customize**) forgets both. The panel is matched by the image `pelican/panel`; set the label
`ratatosk.adapter=pelican` for a custom image. **One Pelican panel per Ratatosk is supported:** the
shared cache of server names and states holds a single Pelican, so with two the rows would fight
over it and show the wrong names, and **Remove keys** forgets both.

**Editing config files.** With the user key set, a stopped server's row has a **Config** button.
It opens a dialog listing the server's settings files (`.ini`, `.cfg`, `.properties`, `.txt`;
Steam and Unity install folders are skipped). A file opens as fields grouped by section
(true/false settings are checkboxes; a packed setting such as Palworld's `OptionSettings` is split
into one field per setting) or as raw text, and any file can be switched between the two. Saving
changes only the values you edited, so comments, blank lines and line endings stay as they were.
If the file changed on the server since you opened it, the save is refused and you reload first.
The server must be **stopped**: the button is disabled while it runs, and Ratatosk checks
again on every request, because a running game rewrites its files on shutdown. `READ_ONLY` hides
the editor completely (config files can hold passwords). File contents are never written to the
Debug panel.
Comments in the file are shown too: the block at the very top appears above the fields, and a
comment directly above a setting becomes an **i** button next to it (hover for the text, tap or
click to show it under the setting).

### Other apps

Adapters are small YAML files, and you can write your own for any service with a JSON API,
with no code. See [docs/ADAPTERS.md](docs/ADAPTERS.md) for the format and a step-by-step guide.
Put your files in a folder and mount it into Ratatosk, for example
`./my-adapters:/data/adapters:ro`, then restart Ratatosk.

## Development (see also [CONTRIBUTING.md](CONTRIBUTING.md))

```
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest -q
node --test tests/js/*.mjs     # the panel layout logic, no browser needed
for f in static/*.js; do node --check "$f"; done   # syntax check of the page scripts
```

The tests use a fake Docker client, so they need no Docker daemon. The image uses Python 3.12.

## License

MIT. See [LICENSE](LICENSE).
