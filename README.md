# matcha-anki-server

A small server that lets a [Matcha Reader](https://github.com/eszter007/matcha-reader) e-reader sync flashcards with Anki, without Anki desktop running. It keeps an Anki collection on disk, syncs it with AnkiWeb (or your own Anki sync server), and answers the reader with the same API as [AnkiConnect](https://git.sr.ht/~foosoft/anki-connect)'s v1, which the reader also speaks.

It also prepares cards for e-ink: large images are shrunk to grayscale, and buttons, scripts and hidden menus are left out of each card.

## What you need

- A computer or server that is always on: a NAS, a Raspberry Pi, a home server, or a small cloud VM.
- Docker.
- An AnkiWeb account, if you want your Anki collection on the server. Without one the server keeps its own collection, filled only by the reader.

## Run it with Docker

1. Clone this repository.
2. Make a file named `.env` next to `docker-compose.yml`:

   ```
   MATCHA_TOKEN=a-long-random-key
   ANKIWEB_USERNAME=you@example.com
   ANKIWEB_PASSWORD=your-ankiweb-password
   ```

   `MATCHA_TOKEN` is the key the reader sends. Use at least 16 random characters, for example from `openssl rand -hex 24`. Leave out the two AnkiWeb lines to run without AnkiWeb.
3. Start it:

   ```
   docker compose up -d
   ```

The server listens on port 8080. The collection is kept in `./data`. `.env` and `data/` are never committed (see `.gitignore`).

## Set up the reader

In the reader's settings (**Settings → Connectivity → Anki Sync**, or the web settings page in **File Transfer**):

- **Anki Address:** `http://<server address>:8080`, or your `https://` address (see below).
- **Anki Key:** your `MATCHA_TOKEN`.
- **Anki Decks:** the decks to sync, separated by commas.
- **Sync AnkiWeb Too:** **On**, so each sync also syncs the server with AnkiWeb.

Then choose **Flashcards → Sync with Anki** on the reader.

## Reaching it from outside your home

On your home network, plain `http://` is fine. Across the internet, put the server behind HTTPS so the key is not sent in the clear, for example with [Caddy](https://caddyserver.com/) as a reverse proxy. Note that HTTPS needs more memory on the reader; on the X4 it may not fit during a sync.

## Google Cloud

`deploy/gcp/startup.sh` runs the server on a Compute Engine VM, with Caddy in front for HTTPS at `<external-ip>.sslip.io`. An e2-micro VM in `us-central1`, `us-west1` or `us-east1` is in Google's free tier; Google bills a few dollars a month for its public IPv4 address.

With the [gcloud CLI](https://cloud.google.com/sdk) signed in to your project (`PROJECT` below):

```bash
gcloud services enable compute.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com --project PROJECT
gcloud artifacts repositories create matcha --repository-format=docker --location=us-central1 --project PROJECT
gcloud builds submit --tag us-central1-docker.pkg.dev/PROJECT/matcha/matcha-anki-server:latest --project PROJECT .
```

The key and the AnkiWeb login go into Secret Manager. Each command reads the value from what you type; finish with Ctrl-D:

```bash
openssl rand -hex 24 | tr -d '\n' | gcloud secrets create matcha-token --data-file=- --project PROJECT
gcloud secrets create ankiweb-username --data-file=- --project PROJECT
gcloud secrets create ankiweb-password --data-file=- --project PROJECT
```

Let the VM's service account read them and the image:

```bash
SA=$(gcloud projects describe PROJECT --format="value(projectNumber)")-compute@developer.gserviceaccount.com
gcloud projects add-iam-policy-binding PROJECT --member=serviceAccount:$SA --role=roles/secretmanager.secretAccessor
gcloud projects add-iam-policy-binding PROJECT --member=serviceAccount:$SA --role=roles/artifactregistry.reader
```

Open ports 80 and 443 and create the VM:

```bash
gcloud compute firewall-rules create matcha-web --allow=tcp:80,tcp:443 --target-tags=matcha-web --project PROJECT
gcloud compute instances create matcha-anki --zone us-central1-a --machine-type e2-micro \
  --image-family debian-12 --image-project debian-cloud --boot-disk-size 30GB --boot-disk-type pd-standard \
  --scopes cloud-platform --tags matcha-web \
  --metadata matcha-image=us-central1-docker.pkg.dev/PROJECT/matcha/matcha-anki-server:latest \
  --metadata-from-file startup-script=deploy/gcp/startup.sh --project PROJECT
```

After two or three minutes the server answers at `https://<external-ip with dashes>.sslip.io`, for example `https://203-0-113-5.sslip.io/health`. Show the key for the reader's settings with:

```bash
gcloud secrets versions access latest --secret=matcha-token --project PROJECT
```

To update the server, build the image again and restart the VM (`gcloud compute instances reset matcha-anki --zone us-central1-a --project PROJECT`). The collection stays on the VM's disk.

## Configuration

| Variable | Meaning |
| --- | --- |
| `MATCHA_TOKEN` | Required. The key the reader sends as `Authorization: Bearer <key>`; at least 16 characters. |
| `ANKIWEB_USERNAME`, `ANKIWEB_PASSWORD` | Your AnkiWeb login. Without them, AnkiWeb sync is refused. |
| `SYNC_ENDPOINT` | Optional: your own Anki sync server instead of AnkiWeb. |
| `DATA_DIR` | Where the collection is kept. Default `./data`; `/data` in the Docker image. |
| `PORT` | Default 8080. |

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

## Licence

This server's own code is licensed under [CC BY-NC-SA 4.0](LICENSE): you may use, change and share it for non-commercial purposes, with credit, and changed versions must be shared under the same licence. For commercial use, ask first.

It uses the [Anki](https://github.com/ankitects/anki) library, which keeps its own licence (AGPL-3.0).
