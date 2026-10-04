FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# docker CLI + compose plugin, used only by the Update button
COPY --from=docker:cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker:cli /usr/local/libexec/docker/cli-plugins/docker-compose /usr/local/libexec/docker/cli-plugins/docker-compose

COPY app.py activity.py editor.py integrations.py handlers.py ./
COPY adapters/ adapters/
COPY templates/ templates/
COPY static/ static/

VOLUME /data
EXPOSE 8090

# single worker: the setup token and login throttle live in process memory
CMD ["gunicorn", "-b", "0.0.0.0:8090", "-w", "1", "--threads", "8", "app:app"]
