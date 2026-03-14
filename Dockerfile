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

# Run as non-root (matches k8s securityContext runAsUser: 1000)
USER 1000

EXPOSE 5000
CMD ["flask", "run"]
