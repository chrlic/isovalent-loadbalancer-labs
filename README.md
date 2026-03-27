# Isovalent Load Balancer — Lab Guide & Management GUI

A hands-on lab for deploying [Isovalent](https://isovalent.com/) ILB (Layer 7 Load Balancer) on a [Kind](https://kind.sigs.k8s.io/) cluster with BGP peering, external backend VMs, and a browser-based management UI.

> **Note:** Isovalent Load Balancer is an enterprise-grade, officially supported product by Isovalent/Cisco.
> The management GUI in this repository is a community best-effort tool — it is not an official Isovalent product
> and comes with no guarantees of correctness, completeness, or ongoing support.

---

## What's in this repo

| Path | Description |
|------|-------------|
| `lab-guide.md` | Step-by-step deployment guide — from a fresh RHEL/CentOS host to a fully working ILB with BGP |
| `index.html` | Single-file management GUI — runs via `server.py` |
| `server.py` | Python HTTP server: GUI backend, Prometheus `/metrics`, Splunk HEC forwarder |
| `Dockerfile` | Container image for the GUI |
| `gui-deployment/` | Kubernetes manifests to deploy the GUI onto the cluster itself |
| `ilb-gui.service` | systemd unit file for running the GUI as a Linux service |
| `start.sh` | Helper script to launch the GUI locally |

---

## Lab Topology

```
  External Network 192.168.33.0/24
  ┌─────────────────────────────────────────────────────────────────────┐
  │                                                                     │
  │  ┌──────────────┐  BGP   ┌─────────────────────────────────────┐   │
  │  │  FRR Router  │◄──────►│          ILB Host (RHEL)            │   │
  │  │ 192.168.33.1 │        │         192.168.33.24 (ens192)      │   │
  │  │  ASN 65220   │        │                                     │   │
  │  └──────────────┘        │  Kind bridge  172.19.0.1/16         │   │
  │                          │  ┌─────────────────────────────┐    │   │
  │  ┌──────────────┐        │  │  kind-worker  172.19.0.4    │    │   │
  │  │External Node │        │  │  T1 — L3/L4 BPF LB          │    │   │
  │  │192.168.33.10 │        │  ├─────────────────────────────┤    │   │
  │  └──────────────┘        │  │  kind-worker2 172.19.0.3    │    │   │
  │                          │  │  T2 — L5-L7 Envoy           │    │   │
  │                          │  ├─────────────────────────────┤    │   │
  │                          │  │  kind-worker3 172.19.0.2    │    │   │
  │                          │  │  T2 — L5-L7 Envoy           │    │   │
  │                          │  └─────────────────────────────┘    │   │
  │                          │  VIP pool: 172.20.0.0/24            │   │
  │                          └─────────────────────────────────────┘   │
  │                                                                     │
  └─────────────────────────────────────────────────────────────────────┘

  Backend VMs  192.168.39.0/24
  ┌──────────────────────────────────┐
  │  ilb-server1  192.168.39.221:8080 │
  │  ilb-server2  192.168.39.222:8080 │
  └──────────────────────────────────┘
```

**Traffic path:** client → FRR (BGP) → ILB host → T1 (L3/L4 BPF) → T2 (Envoy, L7) → backend VM

---

## Quick Start

### 1. Follow the lab guide

Read [`lab-guide.md`](lab-guide.md) for the full walkthrough:
- Docker + Kind cluster setup
- Helm install of Isovalent ILB
- BGP peering with FRR
- Host routing and iptables rules
- Backend nginx VMs
- CRD deployment (LBIPPool, LBBackendPool, LBVIP, LBService)
- GUI deployment on the cluster

### 2. Run the GUI locally (for development/testing)

```bash
# Requires kubectl configured and Python 3
./start.sh
# Open http://localhost:8080
```

### 3. Deploy the GUI onto the cluster

```bash
kubectl apply -f gui-deployment/rbac.yaml
kubectl apply -f gui-deployment/deployment.yaml
kubectl apply -f gui-deployment/lbservice.yaml
# GUI will be reachable at http://172.20.0.2:8080
```

---

## Management GUI

The GUI is a single-page app served from `index.html`. It talks to the Kubernetes API via a local Python proxy (`server.py`) that shells out to `kubectl` and `cilium`.

**Features:**
- Dashboard with LB health overview (services, VIPs, BGP status)
- CRUD for all Isovalent ILB CRDs: LBService, LBVIP, LBBackendPool, LBDeployment, LBIPPool
- BGP setup wizard (IsovalentBGPClusterConfig, IsovalentBGPPeerConfig, IsovalentBGPAdvertisement)
- LB Status view (real-time `cilium lb status` output)
- Backend Health tab on LBBackendPool detail — per-endpoint health per T2 node
- Cilium Nodes view
- CRD Reference page — relationship diagram and field documentation
- Namespace selector (all views update on change)

---

## CRD Overview

```
LBIPPool          — defines the VIP address pool
    │
    ▼
LBVIP             — allocates a VIP from the pool
    │
    ▼
LBService         — binds a VIP + port to one or more backend routes
    │                 references LBBackendPool(s) per route
    ▼
LBBackendPool     — defines a group of backends (K8s Services or external endpoints)

LBDeployment      — assigns T1/T2 nodes to a set of LBServices by label selector
```

---

## Key Configuration Notes

- **DSR mode** (`loadBalancer.mode: dsr`, `dsrDispatch: ipip`) — IPIP is only used on the T1→T2 leg; backend VMs receive plain TCP from T2/Envoy.
- **bpf.lbModeAnnotation: false** — prevents the controller from annotating services with `forwarding-mode: dsr`, which would conflict.
- **Host route** — VIP pool `172.20.0.0/24` is not on the Kind bridge subnet; add `ip route add 172.20.0.0/24 via <T1-IP>` on the host.
- **iptables MASQUERADE** — required so T1's BPF sees traffic as coming from a local source and delegates to T2.

See [`lab-guide.md`](lab-guide.md) for the full rationale and exact commands.

---

## Observability

`server.py` exposes observability endpoints and an optional Splunk forwarder with no extra dependencies (pure Python stdlib).

### Prometheus `/metrics`

A background thread refreshes metrics every `METRICS_INTERVAL` seconds (default 30). The endpoint exposes standard Prometheus text format and can be scraped by Prometheus, an OpenTelemetry Collector, or any compatible tool.

```
GET http://<host>:8080/metrics
```

**Metrics exposed:**

| Metric | Labels | Description |
|--------|--------|-------------|
| `ilb_service_online` | name, namespace, vip, port, type | 1 = ONLINE, 0 = degraded |
| `ilb_service_backends_ok` | same | Healthy backend count (httpProxy + tcpProxy) |
| `ilb_service_backends_total` | same | Total backend count |
| `ilb_service_bgp_peers_ok/total` | same | BGP peer health per service |
| `ilb_service_t1_nodes_ok/total` | same | T1 node health per service |
| `ilb_service_t2_nodes_ok/total` | same | T2 node health per service |
| `ilb_bgp_peer_established` | local_asn, peer_address, peer_asn, node | 1 = established |
| `ilb_bgp_peer_prefixes_received` | same | Received route count |
| `ilb_inventory_count` | resource | Object count per CRD type |
| `ilb_scrape_timestamp_seconds` | — | Unix timestamp of last collection |

### Full status export

```
GET http://<host>:8080/api/status/export
```

Returns a single JSON document with `lb_status`, `bgp_peers`, `inventory`, and `collected_at`. Useful for ad-hoc scraping or feeding into any monitoring pipeline.

### Splunk HEC forwarder

Set the following environment variables to enable a background thread that pushes the full status export to a Splunk HTTP Event Collector on a configurable interval:

| Variable | Default | Description |
|----------|---------|-------------|
| `SPLUNK_HEC_URL` | _(disabled)_ | HEC endpoint, e.g. `https://splunk:8088/services/collector` |
| `SPLUNK_HEC_TOKEN` | _(disabled)_ | HEC token |
| `SPLUNK_HEC_INDEX` | _(none)_ | Target Splunk index (optional) |
| `SPLUNK_HEC_INTERVAL` | `60` | Push interval in seconds |
| `SPLUNK_VERIFY_SSL` | `true` | Set to `false` to skip TLS verification |

The forwarder only starts when both `SPLUNK_HEC_URL` and `SPLUNK_HEC_TOKEN` are set.

### Running as a Linux service

Copy and adapt the included systemd unit:

```bash
sudo cp ilb-gui.service /etc/systemd/system/
# Edit WorkingDirectory, KUBECONFIG, and optional Splunk vars
sudo useradd -r -s /sbin/nologin ilbgui
sudo cp server.py index.html favicon.ico /opt/ilb-gui/
sudo cp /usr/local/bin/kubectl /usr/local/bin/cilium /opt/ilb-gui/  # or adjust KUBECTL/CILIUM env vars
sudo systemctl daemon-reload
sudo systemctl enable --now ilb-gui
```

---

## Requirements

- RHEL 9 / CentOS Stream 9 host
- Docker CE
- Kind v0.20+
- Helm 3
- Isovalent ILB Helm chart (`isovalent/cilium`)
- FRR BGP router reachable from the host
- Two backend VMs with nginx on port 8080

---

## License

See [LICENSE](LICENSE).
