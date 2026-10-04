# Ratatosk

A self-hosted dashboard for the containers on one Docker host. It shows every container
with its state, health, ports, CPU and memory, lets you start, stop and restart them, and groups
them into panels you arrange yourself. One container, the Docker socket and a data volume are all
it needs.

*Named after the squirrel in Norse myth that runs up and down the world tree carrying messages
between the realms.*

- Lists every container with state, health, image, published ports, CPU and memory
- Start, stop and restart any container
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
- A browser that can reach the host. Keep Ratatosk on a private network, see [Security](#security).

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

- **The port.** `"8090:8090"`: change the left number to serve on another port.
- **Read-only mode.** Set `READ_ONLY: "true"` if Ratatosk should only look (see [Configuration](#configuration)).
- **Extra mounts**, only if you want the Update button or the storage meters to see more of the host.
  The file has commented examples, and the sections [Updates](#updates) and
  [Storage meters](#storage-meters) explain them. You can add them later.

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
you are on it). Enter the token, a username (letters, digits, `.`, `_` and `-`, up to 32 characters) and a
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

It is usually `ratatosk_ratatosk-data` (the folder name, then the volume name). Back it up
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
  change the left number in `ports:` and run `docker compose up -d`. Check the host's firewall.
- **I lost the setup token.** Run `docker restart ratatosk` and read the log again (step 5). A token is
  only made while no admin exists.
- **I forgot my password.** Another admin can reset it under **Settings → Users**. If you are the only
  admin, run `docker exec ratatosk rm /data/users.json` and then `docker restart ratatosk`. That brings
  back the setup page with a new token. Create the admin again; layouts and stored app keys are kept.
- **"Too many attempts."** Sign-in is limited to 5 failures in 5 minutes per address. Wait, or
  `docker restart ratatosk`. Behind a reverse proxy see `BEHIND_PROXY` in [Security](#security).
- **The container list is empty or shows a Docker error.** Ratatosk needs `/var/run/docker.sock`
  mounted. On a host with SELinux, if it still can't read the socket, add
  `security_opt: ["label=disable"]` to the service.
- **The page looks old after an update.** Hard refresh (Ctrl+Shift+R).
- **The Update button is greyed out.** Mount the project's folder at the same path (see [Updates](#updates)).
- **An app's panel says it could not reach the app, or shows nothing.** See [Integrations](#integrations);
  the **Debug** button in the page header shows each request and why it failed.

## Configuration

Set these under `environment:` in your compose file.

| Variable    | Default | Meaning                                  |
|-------------|---------|------------------------------------------|
| `READ_ONLY` | off     | Disable start/stop/restart/update, app keys and user changes |
| `UPDATE_CHECK` | on   | Set `false` to stop asking registries about newer images |
| `BEHIND_PROXY` | off  | Set `true` when a reverse proxy or tunnel is the only way in: the login throttle and the Debug log then use the visitor's real address |
| `DATA_DIR`  | `/data` | Where users, keys, layouts and the session key are kept. Leave it and mount a volume there |
| `DOCKER_HOST` | the socket | Point at a filtered socket proxy instead of mounting the socket directly |

Ratatosk listens on port 8090 inside the container; change the left number of `ports:` to serve on another
port. Everything it stores is in `DATA_DIR`:

| File | Holds |
|------|-------|
| `users.json` | Accounts (password hashes) and roles |
| `integrations.json` | App keys and logins you entered (readable only by Ratatosk's user, not encrypted) |
| `groups/` | One file per user: layout, groups, names, priorities, storage meters |
| `secret_key` | Signs the session cookies |
| `adapters/` | Your own app adapters, if you mount some |

## Security

- Mounting `/var/run/docker.sock` gives Ratatosk (and anyone who logs in to
  it) root-equivalent control of the host. **Do not expose it to the public
  internet.** Put it behind a VPN such as Tailscale or WireGuard.
- Set `READ_ONLY=true` to disable all state-changing actions.
- To reduce the socket's reach, point `DOCKER_HOST` at a filtered socket proxy
  instead of mounting the socket directly.
- Behind a reverse proxy or tunnel, set `BEHIND_PROXY=true` and make sure nothing else can reach
  Ratatosk's port, because the address then comes from a header. Without it every visitor
  looks like the proxy, so five failed logins by anyone lock everyone out. The session cookie is
  marked `Secure` whenever the page was served over https, whatever the setting. Ratatosk
  is meant for a private network. If you publish it, put an access layer with MFA in front of it
  (for example Cloudflare Access); its own sign-in has no second factor.
- Container logs can contain secrets. They are only shown to signed-in users,
  and stay visible in `READ_ONLY` mode.
- Logins are throttled per IP (changing your own password counts too), and first-run setup
  requires a token that is only printed to the container logs.
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
- Every signed-in user can open it, also in `READ_ONLY` mode. Lines can contain container
  names and addresses.
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
never changes your desktop. The layout is saved on the server, per user (`/data/groups/`), so
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

A container can't see the host's disks by itself, so the picker lists what has been mounted into
it. To show a disk that isn't listed, mount any folder from it into Ratatosk, read-only is
fine (only the disk's size and free space are read, never the folder's contents):

```yaml
volumes:
  - /srv/data:/host/data:ro
```

Recreate Ratatosk (`docker compose up -d`), then **Add disk** lists it. If the Compose
project folders you mounted for the Update button sit on another disk, that disk is already
listed.

## Users

The account made at `/setup` is an admin. **Settings** (the cog) → **Users** lists everyone;
only admins see it. There are two roles:

| Role | Can do |
|------|--------|
| Admin | Everything an operator can, plus add, remove and change users, reset passwords, and find, enter or remove app keys |
| Operator | Look at everything, start, stop, restart and update containers, edit the config files of game servers, and arrange their own page. Cannot manage users or app keys |

- **Passwords:** an admin sets a password when adding a user and can reset it later. There is no
  email, so a forgotten password is reset by an admin. Everyone can change their own under
  **Settings** → **Account**. Passwords are at least 8 characters and stored hashed.
- **The last admin** can't be removed or made an operator.
- **Role changes and removals apply at once**, also to a person who is signed in.
  A password change does not sign out sessions that are already open.
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
| `dashboard.name`     | Name shown for the container (a rename in the UI wins)     |
| `dashboard.url`      | Link to this `http(s)` URL instead of the published port   |
| `dashboard.hide`     | `true` removes the tile                                    |

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

The button needs the project's Compose files at the same path inside Ratatosk
container as on the host. Mount each project's folder that way, for example
`/home/me/media:/home/me/media:ro`. Without it the button is greyed out and its tooltip
says which file is missing.

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

The panel for each app matches by image name. Set the label `dashboard.adapter=<id>` on a container
to match an app with a custom image name, or `dashboard.adapter=none` to hide its panel.

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
Ratatosk's address.

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
| Admin (application) key | Admin > API keys, with read access to Servers | Every server, with its name and port |
| User key (Pelican calls it a *client* key) | Your account > API keys, in the Pelican panel | Live state, and start/stop/restart. It only sees servers that account can access |

With only the user key, the list is that account's own servers. With only the admin key there
is no live state, and Start/Stop/Restart still go straight to Docker (the Debug panel notes it
each time). Add the other key later with **Add** on the panel. **Remove keys** (in **Customize**) forgets both. The panel is matched by the image `pelican/panel`; set the label
`dashboard.adapter=pelican` for a custom image. One Pelican panel per Ratatosk is supported.

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

## Development

```
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest -q
node --test tests/js/*.mjs     # the panel layout logic, no browser needed
node --check static/app.js     # syntax check of the page script
```

The tests use a fake Docker client, so they need no Docker daemon. The image uses Python 3.12.

## License

MIT. See [LICENSE](LICENSE).
