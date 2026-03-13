# Minimal Dockerfile for λ‑xTB calculator web service
# Uses conda to install the exact environment from environment.yml.

FROM continuumio/miniconda3:latest

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

# Flask defaults
ENV FLASK_APP=app.py
ENV FLASK_RUN_HOST=0.0.0.0

# Expose port used by flask
EXPOSE 5000

# Run the app
CMD ["flask", "run"]
