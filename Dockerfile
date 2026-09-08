# Use official Python slim image for a smaller footprint
FROM python:3.12-slim

# Set build-time variables
ARG APP_HOME=/app
ARG USERNAME=appuser
ARG USER_UID=1000
ARG USER_GID=$USER_UID

# Set working directory
WORKDIR ${APP_HOME}

# Install system dependencies required for building Python packages and curl for healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    libpq-dev \
    python3-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user for security
RUN groupadd --gid $USER_GID $USERNAME \
    && useradd --uid $USER_UID --gid $USER_GID -m $USERNAME \
    && chown -R $USERNAME:$USERNAME $APP_HOME

# Switch to non-root user
USER $USERNAME

# Add user-specific bin directory to PATH
ENV PATH="/home/$USERNAME/.local/bin:$PATH"

# Copy requirements first for better layer caching
COPY --chown=$USERNAME:$USERNAME requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir --user -r requirements.txt \
    && pip cache purge

# Copy application code
COPY --chown=$USERNAME:$USERNAME . .

# Create instance and logs directories with proper permissions
RUN mkdir -p instance logs \
    && chown -R $USERNAME:$USERNAME instance logs

# Expose port
EXPOSE 8080

# Healthcheck
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:80/ || exit 1

# Command to run the application with Waitress
CMD ["waitress-serve", "--host=0.0.0.0", "--port=80", "app:app"]
