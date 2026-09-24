# App image for λ‑xTB — builds on top of the pre-built base image.
# The base image contains the conda environment; this layer only adds app code.
# Typical build time: ~20 seconds.

FROM cerit.io/krupickm/lambda-xtb-base:latest

WORKDIR /app

COPY . .

# Version baked in at build time (passed via --build-arg BUILD_VERSION=<number>)
ARG BUILD_VERSION=dev
ENV BUILD_VERSION=${BUILD_VERSION}

ENV FLASK_APP=app.py
ENV FLASK_RUN_HOST=0.0.0.0

# data/ holds only the scratch SQLite file, which is untracked and
# .dockerignore'd, so it is absent from the build context entirely — create it
# here rather than relying on the COPY above. It is owned by root either way,
# so make it writable by the non-root runtime user, which is what lets
# init_db() create the sqlite file on a standalone `docker run` with no volume
# mounted. In k8s a PVC is mounted over /app/data (see
# k8s/base/deployment.yaml), which shadows this directory entirely, so this has
# no effect there.
RUN mkdir -p data && chown -R 1000:1000 data

# Run as non-root (matches k8s securityContext runAsUser: 1000)
USER 1000

EXPOSE 5000
CMD ["flask", "run"]
