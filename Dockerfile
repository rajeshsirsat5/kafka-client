# ---- Base image ----
FROM registry.natwest.gitlab-dedicated.com/natwestgroup/engineeringartifacts/engineeringcomponents/executors/build/natwest-python/python-3.12.12-ubi9-9.6-1760515502

# ---- Build-time args ----
ARG APP_DIR \
    SVC_DIR \
    PIP_INDEX_URL \
    RBS_INDEX_URL \
    APP_USER="appuser"

# ---- Helpful environment defaults ----
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/apps/${APP_DIR} \
    HOME=/home/${APP_USER} \
    PIP_INDEX_URL=${PIP_INDEX_URL} \
    RBS_INDEX_URL=${RBS_INDEX_URL}

# ---- OpenTelemetry configuration ----
# OTEL_EXPORTER_OTLP_ENDPOINT: Override this in ECS Task Definition with your collector URL
# OTEL_SERVICE_NAME: Override this in ECS Task Definition with your actual service name
ENV OTEL_TRACES_EXPORTER=otlp \
    OTEL_METRICS_EXPORTER=otlp \
    OTEL_LOGS_EXPORTER=otlp \
    OTEL_EXPORTER_OTLP_PROTOCOL=grpc \
    OTEL_EXPORTER_OTLP_ENDPOINT=http://replace-with-your-collector-url:4317 \
    OTEL_SERVICE_NAME=replace-with-your-service-name \
    OTEL_PROPAGATORS=xray,tracecontext,baggage \
    OTEL_PYTHON_LOGGING_AUTO_INSTRUMENTATION_ENABLED=true \
    OTEL_PYTHON_ID_GENERATOR=xray

# Set workdir
WORKDIR /apps/${APP_DIR}

# Install uv
RUN pip install --no-cache-dir uv

# Copy requirements first (best for Docker layer caching)
COPY ./pyproject.toml ./uv.lock* ./
COPY ./app_package/terraform/resources/.env.* /apps/app_package/terraform/resources/
COPY ./app_package/terraform/resources/Service-Kafkacarbond.p12 /apps/app_package/terraform/resources/
COPY ./app_package/terraform/resources/sample_data/mapping.json /apps/app_package/terraform/resources/

# Install application dependencies via uv
RUN uv sync \
        --index-url "${PIP_INDEX_URL}" \
        --trusted-host tools.rbspeople.com \
        --no-dev

# Install OpenTelemetry packages into the uv venv so they share the same Python environment.
# Using "uv pip install" keeps everything in .venv, avoiding the split-environment
# problem that would occur if we used system pip here.
RUN uv pip install \
        --index-url "${PIP_INDEX_URL}" \
        --trusted-host tools.rbspeople.com \
        opentelemetry-distro \
        opentelemetry-exporter-otlp-proto-grpc \
        opentelemetry-propagator-aws-xray \
        opentelemetry-sdk-extension-aws \
        opentelemetry-instrumentation-fastapi \
        opentelemetry-instrumentation-httpx \
        opentelemetry-instrumentation-requests \
        opentelemetry-instrumentation-logging \
        opentelemetry-instrumentation-confluent-kafka && \
    # Auto-detect and install instrumentors for any other packages already in the venv
    .venv/bin/opentelemetry-bootstrap --action=install

# Copy application source
COPY . .

# Drop Privileges
USER ${APP_USER}

# Non-privileged port
EXPOSE 8080

# Use .venv's opentelemetry-instrument so it shares the same environment as the app
CMD [".venv/bin/opentelemetry-instrument", \
     ".venv/bin/python3", "-m", "uvicorn", "src.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8080"]
