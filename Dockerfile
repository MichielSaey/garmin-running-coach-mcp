FROM ghcr.io/astral-sh/uv:python3.14-trixie
WORKDIR /app
COPY requirements.txt /app/requirements.txt
COPY build.sh /app/build.sh
COPY post-build.sh /app/post-build.sh
RUN if [ -f /app/build.sh ]; then chmod +x /app/build.sh && /app/build.sh; fi

# Install Python requirements early so layers are cached when requirements don't change
RUN pip install --no-cache-dir -r /app/requirements.txt
# Copy all Python modules in one step so new files cannot be forgotten individually
# and break container health on startup.
COPY src/*.py /app/
ENV MCP_HOST=0.0.0.0
ENV MCP_PORT=8000
ENV MCP_TRANSPORT=streamable-http
CMD ["/bin/sh", "-c", "if [ -f /app/post-build.sh ]; then chmod +x /app/post-build.sh && /app/post-build.sh; fi"]
