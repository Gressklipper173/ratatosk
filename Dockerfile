# Base images are pinned by digest so two builds of one commit match. To update: look up the
# new digests, rebuild, and check the Update button still works (the compose flags it relies on
# can change between docker CLI releases).
# docker CLI + compose plugin, used only by the Update button
FROM docker:29.8.2-cli@sha256:35cfcf893c20f2931f4909151b5407aaaeab711277de7ce8b8f521f095d54c32 AS dockercli

FROM python:3.14.7-slim@sha256:51dafde81dbdb6ebde285137a295cf18a47ca95234fe388a343719cb97305b3d

WORKDIR /app

# requirements.txt is generated with hashes from requirements.in (see the note in that file)
COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=dockercli /usr/local/libexec/docker/cli-plugins/docker-compose /usr/local/libexec/docker/cli-plugins/docker-compose

COPY VERSION ./
COPY app.py activity.py editor.py integrations.py handlers.py schedules.py ./
COPY adapters/ adapters/
COPY templates/ templates/
COPY static/ static/

LABEL org.opencontainers.image.title="Ratatosk" \
      org.opencontainers.image.description="A self-hosted dashboard for the containers on one Docker host" \
      org.opencontainers.image.source="https://github.com/Gressklipper173/ratatosk" \
      org.opencontainers.image.licenses="MIT"

# /data is a declared volume: a bare `docker run` without -v gets an anonymous one, so name it
# (the compose file does) if you want to find your data again
VOLUME /data
EXPOSE 8090

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8090/healthz', timeout=4).status == 200 else 1)"

# single worker: the setup token and login throttle live in process memory
CMD ["gunicorn", "-b", "0.0.0.0:8090", "-w", "1", "--threads", "8", "app:app"]
