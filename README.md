# Isovalent Load Balancer — Lab Guide & Management GUI

A hands-on lab for deploying [Isovalent](https://isovalent.com/) ILB (Layer 7 Load Balancer) on a [Kind](https://kind.sigs.k8s.io/) cluster with BGP peering, external backend VMs, and a browser-based management UI.

---

## What's in this repo

| Path | Description |
|------|-------------|
| `lab-guide.md` | Step-by-step deployment guide — from a fresh RHEL/CentOS host to a fully working ILB with BGP |
| `index.html` | Single-file management GUI — runs via `server.py` |
| `server.py` | Lightweight Python HTTP server + `kubectl`/`cilium` proxy backend |
| `Dockerfile` | Container image for the GUI |
| `gui-deployment/` | Kubernetes manifests to deploy the GUI onto the cluster itself |
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
- Cilium Nodes view
- CRD Reference page — relationship diagram and field documentation
- Namespace selector (all views update on change)

**Screenshots:**

> Dashboard → LB Status → BGP Setup → CRD Reference

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
