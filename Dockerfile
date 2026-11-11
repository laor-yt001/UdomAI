FROM python:3.11-slim

# Set environment variables
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_ROOT_USER_ACTION=ignore \
    TZ=UTC

# Install system dependencies, including tzdata
RUN apt-get update -qq && \
    apt-get install -y -qq --no-install-recommends \
    curl ca-certificates gnupg tzdata wget tar && \
    mkdir -p /etc/apt/keyrings && \
    curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg && \
    echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_20.x nodistro main" > /etc/apt/sources.list.d/nodesource.list && \
    apt-get update -qq && \
    apt-get install -y -qq --no-install-recommends ffmpeg nodejs && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Create a non-root user and group
RUN groupadd -r botgroup && useradd -r -g botgroup -d /app botuser

WORKDIR /app

# Copy requirement files and install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir -U https://github.com/yt-dlp/yt-dlp/archive/master.tar.gz

# Copy source code and set ownership
COPY --chown=botuser:botgroup . .
RUN chown -R botuser:botgroup /app

# Switch to the non-root user
USER botuser

# Download wireproxy
RUN wget -qO- https://github.com/windtf/wireproxy/releases/download/v1.1.3/wireproxy_linux_amd64.tar.gz | tar xz && \
    chmod +x wireproxy

# Run the bot with start_render.sh
CMD ["bash", "start_render.sh"]

