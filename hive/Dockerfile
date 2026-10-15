FROM python:3.11.16-slim@sha256:e41613d42d4891e4930f79523f93f81bbc7632584ec65e36ab055f41a800b41e

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --require-hashes -r /app/requirements.txt

COPY common /app/common
COPY hive /app/hive
COPY scripts /app/scripts
COPY domain-kits/compiler /app/domain-kits/compiler
COPY domain-kits/sheets /app/domain-kits/sheets

# Compiled kits are build output (gitignored), so the image compiles every
# sheet itself; the entrypoint then registers whatever this produced.
RUN mkdir -p /app/domain-kits/generated \
    && for sheet in /app/domain-kits/sheets/*.yaml /app/domain-kits/sheets/*.yml; do \
         [ -f "$sheet" ] || continue; \
         PYTHONPATH=/app python /app/domain-kits/compiler/engine.py --sheet "$sheet" \
           --out-dir /app/domain-kits/generated --tests-out-dir /tmp/kit-tests || exit 1; \
       done \
    && rm -rf /tmp/kit-tests
COPY VERSION /app/VERSION

RUN chmod +x /app/scripts/hive_docker_entrypoint.sh \
    && useradd --create-home --uid 1000 agent \
    && mkdir -p /data \
    && chown -R agent:agent /data

ENV DATA_DIR=/data
ENV PYTHONPATH=/app
ENV PORT=10000
EXPOSE 10000

USER agent

CMD ["/app/scripts/hive_docker_entrypoint.sh"]
