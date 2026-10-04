# Adapters

An adapter tells Ratatosk how to read data from one app, such as the list of missing
episodes in Sonarr. It is a small YAML file. It holds no code: the only thing an adapter can make
Ratatosk do is send `GET` requests to paths on that app's own API, using a key an admin
approved.

Adapters are optional. Without one, every container still gets state, logs, stats and
start/stop/restart.

## How it works for the person using Ratatosk

1. A container matches an adapter (by image name, or by a label). Ratatosk lists it, here
   Sonarr, as a row in the **Apps found** panel. Nothing has been read or stored.
2. The admin picks one:
   - **Find key automatically**: Ratatosk reads the one file the adapter names (for
     Sonarr, `/config/config.xml`) inside the container, pulls the key out, checks it against
     the app, and stores it. This only happens when the button is pressed, after a confirmation.
   - **Enter key manually**: paste the key into the form. It is checked against the app before
     it is stored.
   - **Dismiss**: hide the panel. **Restore dismissed integrations** in **Settings** (the cog in the page header) brings it back.
3. With a key stored, the app gets a panel with its data. **Remove key** (in **Customize**) goes back to step 1.

The key is stored in `/data/integrations.json` (readable only by Ratatosk's user). It is
not encrypted, because Ratatosk would have to keep the encryption key in the same volume.
Only admins can find, enter or remove keys; operators see the panels but not these buttons. It is never sent to the browser or written to the logs. In `READ_ONLY` mode, finding, saving and
removing keys are switched off.

## Adding an adapter for a service Ratatosk doesn't know

You need three facts about the app: its API address, how it wants the key sent, and which
request returns the list you want. The app's API documentation or its browser developer tools
(Network tab) will show them.

1. Pick a request that returns JSON and needs the key, to use as `test_path`. Any cheap
   endpoint works; a "status" or "ping" endpoint is typical.
2. Pick the request that returns the data for a panel, and note where the list sits in the
   JSON answer (`rows`) and which fields to show (`row_title`, `row_detail`).
3. Save the file in a folder on the host, for example `./my-adapters/myapp.yml`, and mount that
   folder into Ratatosk at `/data/adapters` by adding this line under Ratatosk's
   `volumes:` in your compose file:
   ```yaml
   - ./my-adapters:/data/adapters:ro
   ```
   (`/data/adapters` lives inside the `/data` volume, and a mount there is fine. Without a
   mount you can copy a file in with `docker exec <ratatosk> mkdir -p /data/adapters` and
   `docker cp myapp.yml <ratatosk>:/data/adapters/`.)
4. Recreate or restart Ratatosk (`docker compose up -d`, or `docker restart <ratatosk>`).
   Adapters are read at start-up.
5. The **Apps found** panel lists any container whose image matches. Open its row and add the key.

If a file has a mistake, Ratatosk skips only that file and lists the problem in a red
"Adapter problems" line above the groups and in the container logs. Other adapters keep working.

A file in `/data/adapters` whose `id` matches a built-in adapter replaces it, which is how you
change a built-in's behaviour.

## Field reference

| Field | Required | Meaning |
|-------|----------|---------|
| `id` | yes | Short name: lowercase letters, digits, `-` or `_`, up to 32 characters. Used by the `dashboard.adapter` label. |
| `name` | yes | Name shown on the panel. |
| `match.images` | yes | List of image names. A container matches when the last part of its image name (`lscr.io/linuxserver/sonarr:latest` becomes `sonarr`) is in the list. An entry with a slash names the repository instead: `pelican/panel` matches `ghcr.io/pelican/panel:latest` but not `ghcr.io/other/panel`. Use it when the last part is too common on its own. |
| `match.port` | yes | The port the app listens on **inside** its container. |
| `auth.kind` | no | `header` (default): an API key sent in a header. `login`: a username and password, entered by hand. `keys`: a set of optional named keys, entered by hand (Pelican has an admin key and a user key). `login` and `keys` need a handler, so only built-in adapters can use them (see Built-in handlers). |
| `auth.keys` | with `keys` auth | One to four `{id, label}` entries. Each is optional; at least one must be entered. The handler receives `{id: key}` for the ones that are stored. |
| `auth.header` | with `header` auth | The HTTP header the key is sent in, for example `X-Api-Key`. The key is sent exactly as stored, so apps that want `Bearer <key>` are not supported by this format yet (see Limits). |
| `test_path` | yes (without a handler) | Path requested to check a key before it is stored. Must start with `/`. |
| `key_file.path` | no | File inside the container that holds the key. Without `key_file`, the panel has no "Find key automatically" button. |
| `key_file.regex` | one of these two | Pattern with exactly one capture group around the key, for example `<ApiKey>([^<]+)</ApiKey>`. Good for XML and plain text. |
| `key_file.json_path` | one of these two | Dotted path to the key in a JSON file, for example `main.apiKey`. Use it when the file holds several `apiKey` fields. |
| `handler` | no | Name of a Python handler. **Only adapters shipped with Ratatosk can use it.** |
| `panels` | yes (without a handler) | One to ten panels (below). |

Each panel:

| Field | Required | Meaning |
|-------|----------|---------|
| `id` | yes | Unique within the file. |
| `title` | yes | Heading of the panel. |
| `path` | yes | Path requested, starting with `/`. |
| `params` | no | Query parameters, as plain text, numbers or true/false. |
| `rows` | yes | Where the list is in the JSON answer. `records` means the top-level `records` list; `data.items` means `items` inside `data`. |
| `row_title` | yes | Text for each row. `{field}` inserts a field of the row; `{series.title}` reaches inside objects. A missing field becomes empty. |
| `row_detail` | no | Smaller text on the right of each row, same `{field}` syntax. |
| `total` | no | Where the total count is in the answer, shown next to the title. Defaults to the number of rows returned. |

At most 50 rows are shown per panel. Paths can't contain `..` or a host name: requests always go
to the container itself.

## The `dashboard.adapter` label

Add a Docker label to a container to override matching:

| Label | Effect |
|-------|--------|
| `dashboard.adapter=<id>` | Use that adapter whatever the image is called (handy for custom builds). |
| `dashboard.adapter=none` | Never show a panel for this container. |

## How Ratatosk reaches the app

It tries, in order, the container's address on each Docker network it shares, then the app's
published port through `host.docker.internal`. For the second route, add this to Ratatosk's
compose service (the sample compose file has it):

```yaml
extra_hosts:
  - "host.docker.internal:host-gateway"
```

If Ratatosk can't reach the app, the panel says so and keeps trying on the next refresh.

## Worked example

A made-up app called "Pantry Tracker" listens on port 3030, wants its key in an `X-Token`
header, and `GET /api/v1/items?low=true` answers like this:

```json
{"data": {"items": [{"name": "Rice", "left": 1, "unit": "kg"}], "count": 4}}
```

<!-- worked-example -->
```yaml
id: pantry
name: Pantry Tracker

match:
  images: [pantry-tracker]
  port: 3030

auth:
  header: X-Token

test_path: /api/v1/ping

panels:
  - id: low
    title: Running low
    path: /api/v1/items
    params:
      low: true
    rows: data.items
    row_title: "{name}"
    row_detail: "{left} {unit}"
    total: data.count
```

Because this app has no config file Ratatosk knows how to read, the example leaves out
`key_file`. The panel offers **Enter key manually** only.

## Built-in handlers

Some apps can't be described by a YAML file: qBittorrent logs in with a username and password
and keeps a session cookie, and Seerr's request list has no titles, so each one has to be looked up.
For these, an adapter that ships with Ratatosk names a **handler**, a small Python function in
`handlers.py`:

```yaml
id: qbittorrent
handler: qbittorrent
auth:
  kind: login
```

A handler has two functions:

| Function | Purpose |
|----------|---------|
| `check(bases, spec, creds)` | Optional. Returns `None` if the key or login works, else a short error message that never contains the secret. Without it, Ratatosk requests `test_path`. |
| `fetch(bases, spec, creds)` | Returns a list of panels. Raises `integrations.AppError` with a safe message on failure. |

`bases` are the addresses to try; use `integrations.call` or `integrations.first_ok`, which try all
of them at once. `creds` is the key string for header auth, or `{"username", "password"}` for
`auth.kind: login`. A panel is `{"id", "title", "rows", "total"}` with an optional `"summary"` line shown
beside the title, and a row is `{"title", "detail"}` with an optional `"progress"` (0 to 100) that draws
a bar.

**Why only built-in adapters can use a handler:** a handler is code that runs inside Ratatosk,
next to the Docker socket. An adapter you put in `/data/adapters` is data only, and a file that names
a `handler` is rejected with a message in the "Adapter problems" line.

A key set (`auth.kind: keys`) works the same way with several keys: the panel asks for the ones the
person wants to use, `check` receives only the ones just entered, and later entries are merged into
what is stored. The handler decides what each key unlocks, and has to work with any combination.

A login (`auth.kind: login`) has no "Find key automatically" button, because apps store passwords
hashed. The panel asks for a username and password, the handler's `check` logs in to confirm them, and
they are stored like a key (see above). They are never sent back to the browser.

## Limits

A YAML adapter can only send `GET` requests, with the key in one header, and show a list. Apps that
need a login, a token in `Authorization: Bearer`, `POST` requests, or a second request per row need a
built-in handler (above), which means a contribution to Ratatosk itself.

## Contributing a built-in adapter

Built-ins live in `adapters/`. A good contribution has:

1. The YAML file, with a comment on each non-obvious field (see `adapters/sonarr.yml`).
2. A recorded sample answer from the app's API (remove any personal data) that the test uses.
3. A test in `tests/` that loads the file and feeds that sample through `integrations.build_panel`
   (or, for a handler, through its `fetch` with `integrations.http_get_json` replaced by a fake).
4. For a handler: add it to `HANDLERS` in `handlers.py`, with tests for a wrong key or login, for an
   unreachable app, and for secrets never appearing in a response.
