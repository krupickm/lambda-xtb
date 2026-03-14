# Minimal Dockerfile for λ‑xTB calculator web service
# Uses conda to install the exact environment from environment.yml.

FROM continuumio/miniconda3:latest

# Apply available OS security patches and remove unused system Python 3.13.
# continuumio/miniconda3:latest is based on Debian 13 (Trixie), which ships
# Python 3.13 as its default system Python — but we use conda Python 3.11
# exclusively. Purging these packages eliminates several High-severity CVEs
# (CVE-2025-13836, CVE-2025-15366, CVE-2025-15367, CVE-2025-8194, CVE-2026-1299).
# The remaining CVEs (glibc, libexpat, libtasn1) have no upstream fix yet.
RUN apt-get update && \
    apt-get upgrade -y && \
    apt-get purge -y --auto-remove \
        python3.13 python3.13-minimal \
        libpython3.13-minimal libpython3.13-stdlib && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Work inside /app
WORKDIR /app

# Copy environment spec first (layer caching)
COPY environment.yml ./

# Create conda environment from env file
RUN conda env create -f environment.yml && \
    conda clean -afy

# Ensure the conda env is on PATH (so python/flask work without explicit activation)
ENV PATH=/opt/conda/envs/xtb-lambda/bin:${PATH}
ENV CONDA_DEFAULT_ENV=xtb-lambda

# Copy source
COPY . .

# Version baked in at build time (passed via --build-arg BUILD_VERSION=<number>)
ARG BUILD_VERSION=dev
ENV BUILD_VERSION=${BUILD_VERSION}

# easyxtb writes calculation temp files under $XDG_DATA_HOME/easyxtb;
# /tmp is a writable tmpfs in k8s and exists from container start
ENV XDG_DATA_HOME=/tmp

# Flask defaults
ENV FLASK_APP=app.py
ENV FLASK_RUN_HOST=0.0.0.0

# Run as non-root (matches k8s securityContext runAsUser: 1000)
USER 1000

# Expose port used by flask
EXPOSE 5000

# Run the app
CMD ["flask", "run"]
