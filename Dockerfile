FROM python:3.12-slim

WORKDIR /app

# Copy application files
COPY index.html .
COPY server.py .
COPY favicon.ico .

# Copy CLI binaries from build context
COPY bin/kubectl /usr/local/bin/kubectl
COPY bin/cilium  /usr/local/bin/cilium
RUN chmod +x /usr/local/bin/kubectl /usr/local/bin/cilium

EXPOSE 8080

CMD ["python3", "server.py"]
