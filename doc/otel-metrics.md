# OpenTelemetry Metrics Scraping

This document explains how to forward Isovalent ILB metrics from the
`/metrics` Prometheus endpoint to Splunk (or any other backend) using
an OpenTelemetry Collector.

---

## How it works

```
ilb-gui /metrics          OTel Collector              Splunk / other backend
(Prometheus format)   ──► prometheus receiver     ──► splunk_hec exporter
                           scrape every N seconds       (or otlp, kafka, ...)
```

The `ilb-gui` server exposes a standard Prometheus text endpoint at
`http://<host>:8080/metrics`. The OTel Collector scrapes it on a
configurable interval and forwards the metrics to any configured exporter.

---

## Collector configuration

Save the following as `otel-collector-config.yaml`. Adjust the scrape
target, Splunk endpoint, and token for your environment.

```yaml
receivers:
  prometheus:
    config:
      scrape_configs:
        - job_name: isovalent_ilb
          scrape_interval: 30s
          static_configs:
            - targets:
                - localhost:8080   # address of ilb-gui server

exporters:
  splunk_hec/metrics:
    endpoint: "https://your-splunk-server:8088/services/collector"
    token: "your-hec-token"
    index: "metrics_index"
    source: "isovalent-ilb"
    sourcetype: "prometheus"
    # tls:
    #   insecure_skip_verify: true   # only for self-signed certs

  # Optional: also write to a local file for debugging
  # file:
  #   path: /tmp/ilb-metrics.json

service:
  pipelines:
    metrics:
      receivers: [prometheus]
      exporters: [splunk_hec/metrics]
```

> **scrape_interval** should match or be a multiple of `METRICS_INTERVAL`
> set on the ilb-gui server (default 30s). Scraping faster than the
> collection interval just returns the same cached data.

---

## Option A: Use the pre-built contrib distribution

The easiest path. The contrib build includes all receivers and exporters.

### Download

```bash
# Check https://github.com/open-telemetry/opentelemetry-collector-releases/releases
# for the latest version. Example with v0.103.0:
VERSION=0.103.0
curl -LO https://github.com/open-telemetry/opentelemetry-collector-releases/releases/download/v${VERSION}/otelcol-contrib_${VERSION}_linux_amd64.tar.gz
tar -xzf otelcol-contrib_${VERSION}_linux_amd64.tar.gz
chmod +x otelcol-contrib
```

### Run

```bash
./otelcol-contrib --config otel-collector-config.yaml
```

### As a systemd service

```bash
sudo mv otelcol-contrib /usr/local/bin/
sudo tee /etc/systemd/system/otelcol.service <<EOF
[Unit]
Description=OpenTelemetry Collector
After=network.target

[Service]
ExecStart=/usr/local/bin/otelcol-contrib --config /etc/otelcol/config.yaml
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo mkdir -p /etc/otelcol
sudo cp otel-collector-config.yaml /etc/otelcol/config.yaml
sudo systemctl daemon-reload
sudo systemctl enable --now otelcol
```

---

## Option B: Build a minimal custom collector

Use the [OTel Collector Builder](https://github.com/open-telemetry/opentelemetry-collector/tree/main/cmd/builder)
(`ocb`) to produce a binary with only the components you need —
smaller binary, reduced attack surface.

### Install the builder

```bash
GO_VERSION=1.21   # requires Go 1.21+
VERSION=0.103.0
curl -LO https://github.com/open-telemetry/opentelemetry-collector/releases/download/cmd%2Fbuilder%2Fv${VERSION}/ocb_${VERSION}_linux_amd64
chmod +x ocb_${VERSION}_linux_amd64
sudo mv ocb_${VERSION}_linux_amd64 /usr/local/bin/ocb
```

### Builder manifest

Save as `ocb-manifest.yaml`:

```yaml
dist:
  name: otelcol-ilb
  description: Minimal OTel Collector for Isovalent ILB metrics
  output_path: ./otelcol-ilb
  otelcol_version: "0.103.0"

extensions:
  - gomod: go.opentelemetry.io/collector/extension/ballastextension v0.103.0
  - gomod: go.opentelemetry.io/collector/extension/zpagesextension v0.103.0

receivers:
  - gomod: go.opentelemetry.io/collector/receiver/prometheusreceiver v0.103.0

processors:
  - gomod: go.opentelemetry.io/collector/processor/batchprocessor v0.103.0
  - gomod: go.opentelemetry.io/collector/processor/memorylimiterprocessor v0.103.0

exporters:
  - gomod: github.com/open-telemetry/opentelemetry-collector-contrib/exporter/splunkhecexporter v0.103.0
```

> **Note:** `splunkhecexporter` lives in the contrib repo even for
> custom builds — it is not in the core collector. The receiver
> (`prometheusreceiver`) is also contrib. Only the processors are core.
> Adjust the version numbers to match a consistent release.

### Build

```bash
ocb --config ocb-manifest.yaml
```

This produces `./otelcol-ilb/otelcol-ilb`. Run it the same way as the
contrib binary:

```bash
./otelcol-ilb/otelcol-ilb --config otel-collector-config.yaml
```

---

## Verifying the setup

### Check the collector is scraping

The OTel Collector exposes its own health and metrics on port 8888 by
default. Add this to the config to enable it:

```yaml
extensions:
  zpages:
    endpoint: 0.0.0.0:55679

service:
  extensions: [zpages]
  pipelines:
    metrics:
      receivers: [prometheus]
      exporters: [splunk_hec/metrics]
```

Then open `http://localhost:55679/debug/pipelinez` in a browser to see
pipeline health, and `http://localhost:8888/metrics` for collector
self-metrics.

### Check metrics are arriving in Splunk

```
index=metrics_index sourcetype=prometheus
| stats latest(_value) by metric_name, name, namespace
```

Or for a quick connectivity test without Splunk, add a `debug` exporter:

```yaml
exporters:
  debug:
    verbosity: detailed

service:
  pipelines:
    metrics:
      receivers: [prometheus]
      exporters: [debug]
```

This prints every scraped metric to stdout — useful for confirming the
Prometheus scrape is working before wiring up Splunk.

---

## Metrics reference

See [README.md](../README.md#observability) for the full list of metrics
exposed by the ilb-gui server.
