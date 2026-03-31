# Isovalent Load Balancer — Lab Deployment Guide

This guide walks through deploying the Isovalent Load Balancer (ILB) on a Kind cluster
running on a single RHEL/CentOS host, including BGP peering, external backend connectivity,
and all the routing fixes required to make it work in this topology.

> **Note:** Isovalent Load Balancer is an enterprise-grade, officially supported product by Isovalent/Cisco.
> The management GUI described in this guide is a community best-effort tool — it is not an official
> Isovalent product and comes with no guarantees of correctness, completeness, or ongoing support.

---

## Lab Topology

```
  External Network 192.168.33.0/24
  ┌──────────────────────────────────────────────────────────────────────────────┐
  │                                                                              │
  │   ┌─────────────────┐  BGP(eBGP)   ┌──────────────────────────────────────┐ │
  │   │   BGP Router    │◄────────────►│         ILB Host (RHEL/CentOS)       │ │
  │   │  192.168.33.1   │  ASN 65220   │  192.168.33.24 (ens192)  ASN 65200   │ │
  │   │   ASN 65220     │◄── VIP /32s ─┤  FRR (local BGP relay)               │ │
  │   └─────────────────┘              │                                      │ │
  │                                    │  Kind bridge br-XXXX  172.19.0.1     │ │
  │   ┌─────────────────┐              │  ┌──────────────────────────────────┐ │ │
  │   │  External Node  │              │  │  kind-worker  172.19.0.5         │ │ │
  │   │  192.168.33.10  │              │  │  T1  (L3/L4 BPF LB)  ASN 64512  │ │ │
  │   └─────────────────┘              │  │  BGP peer → 172.19.0.1 (host)    │ │ │
  │                                    │  ├──────────────────────────────────┤ │ │
  │                                    │  │  kind-worker2 172.19.0.3         │ │ │
  │                                    │  │  T2  (L5-L7 Envoy)               │ │ │
  │                                    │  ├──────────────────────────────────┤ │ │
  │                                    │  │  kind-worker3 172.19.0.2         │ │ │
  │                                    │  │  T2  (L5-L7 Envoy)               │ │ │
  │                                    │  ├──────────────────────────────────┤ │ │
  │                                    │  │  control-plane 172.19.0.5 (cp)   │ │ │
  │                                    │  └──────────────────────────────────┘ │ │
  │                                    │                                      │ │
  │                                    │  VIP pool: 172.20.0.0/24             │ │
  │                                    │    172.20.0.1  → first service       │ │
  │                                    │    172.20.0.2  → ilb-gui             │ │
  │                                    └──────────────────────────────────────┘ │
  │                                                                              │
  └──────────────────────────────────────────────────────────────────────────────┘

  Backend Network 192.168.39.0/24
  ┌──────────────────────────────────────┐
  │  ilb-server1  192.168.39.221:8080    │
  │  ilb-server2  192.168.39.222:8080    │
  └──────────────────────────────────────┘
```

**BGP peering chain:**
```
  T1 node (ASN 64512)  ──eBGP──►  Host FRR (ASN 65200)  ──eBGP──►  Upstream FRR (ASN 65220)
  172.19.0.5                       172.19.0.1 / 192.168.33.24        192.168.33.1
  advertises VIP /32s              relays VIP /32s upstream           installs routes, reaches VIPs
```

**Address summary:**

| Component              | Address / Subnet       | Notes                                   |
|------------------------|------------------------|-----------------------------------------|
| ILB host (ens192)      | 192.168.33.24          | Physical NIC                            |
| ILB host (bridge)      | 172.19.0.1             | Kind bridge gateway, BGP peer for ILB   |
| Host FRR ASN           | 65200                  | BGP relay on the ILB host               |
| Kind subnet            | 172.19.0.0/16          | Docker bridge network                   |
| T1 node                | 172.19.0.5             | kind-worker, L3/L4                      |
| T2 nodes               | 172.19.0.3, .0.2       | kind-worker2/3, L5-L7/Envoy             |
| VIP pool               | 172.20.0.0/24          | BGP-advertised as /32 host routes       |
| Upstream BGP router    | 192.168.33.1 ASN 65220 | FRR in network infrastructure           |
| ILB local ASN          | 64512                  | Advertised from T1 node                 |
| Backend VMs            | 192.168.39.221/222     | External, port 8080                     |
| External client        | 192.168.33.10          | Test node                               |

---

## 1. Install Prerequisites

### Docker

```bash
sudo dnf config-manager --add-repo https://download.docker.com/linux/rhel/docker-ce.repo
sudo dnf install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin
sudo systemctl enable --now docker
sudo usermod -aG docker $USER
# Log out and back in for group membership to take effect
```

#### Optional: HTTP proxy for Docker daemon

If the host is behind a corporate proxy, Docker needs to know about it to pull images.
The daemon proxy config is separate from the shell environment — setting `http_proxy` in
your shell is not enough.

```bash
sudo mkdir -p /etc/systemd/system/docker.service.d
sudo tee /etc/systemd/system/docker.service.d/http-proxy.conf <<EOF
[Service]
Environment="HTTP_PROXY=http://proxy.example.com:80"
Environment="HTTPS_PROXY=http://proxy.example.com:80"
Environment="NO_PROXY=localhost,127.0.0.1,172.19.0.0/16,172.20.0.0/24,10.0.0.0/8"
EOF
sudo systemctl daemon-reload
sudo systemctl restart docker
```

> **NO_PROXY** must include the Kind bridge subnet (`172.19.0.0/16`), the VIP pool
> (`172.20.0.0/24`), and the pod CIDR (`10.0.0.0/8`) so that container-to-container and
> host-to-VIP traffic is never routed through the proxy.

Verify:
```bash
docker run --rm hello-world
```

### kubectl

```bash
curl -LO "https://dl.k8s.io/release/$(curl -sL https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"
sudo install -o root -g root -m 0755 kubectl /usr/local/bin/kubectl
kubectl version --client
```

### Helm

```bash
curl https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
helm version
```

### Kind

```bash
curl -Lo /tmp/kind https://kind.sigs.k8s.io/dl/latest/kind-linux-amd64
sudo install -o root -g root -m 0755 /tmp/kind /usr/local/bin/kind
kind version
```

### Cilium CLI (Enterprise Edition)

Download the enterprise cilium CLI from the Isovalent releases page:
`https://github.com/isovalent/cilium-cli-releases/releases`

The binary used in this lab: `cilium-cli/v0.19.2-cee.1`

The release is a `.tar.gz` archive containing the `cilium` binary.

```bash
# Replace <version> with the actual release tag, e.g. v0.19.2-cee.1
CILIUM_VERSION="v0.19.2-cee.1"

# Download the tarball (adjust URL to the actual asset from the releases page)
curl -Lo /tmp/cilium.tar.gz \
  "https://github.com/isovalent/cilium-cli-releases/releases/download/${CILIUM_VERSION}/cilium-linux-amd64.tar.gz"

# Extract the binary
tar -xzf /tmp/cilium.tar.gz -C /tmp cilium

# Install system-wide
sudo install -o root -g root -m 0755 /tmp/cilium /usr/local/bin/cilium

# Verify
cilium version
```

Note: error message `cilium image (running): unknown. Unable to obtain cilium version.` is fine at this moment, it has no cluster to connect to.

---

## 2. Create the Kind Cluster

Create `kind-config.yaml`:

```yaml
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
- role: control-plane
- role: worker
- role: worker
- role: worker
networking:
  disableDefaultCNI: true
  kubeProxyMode: none
```

Create the cluster:

```bash
KIND_EXPERIMENTAL_DOCKER_NETWORK="kind-cilium" kind create cluster --config=kind-config.yaml
```

> The `KIND_EXPERIMENTAL_DOCKER_NETWORK` variable sets the Docker network name, required
> for ILB end-to-end tests.

Verify (nodes will be `NotReady` until Cilium is installed — this is expected):

```bash
kubectl cluster-info --context kind-kind
kubectl get nodes
```

---

## 3. Install ILB via Helm

### Add the Helm repository

```bash
helm repo add isovalent https://helm.isovalent.com
helm repo update isovalent
```

### Prepare values.yaml


Save as `values.yaml`:

```yaml
debug:
  enabled: true
  verbose: "envoy datapath"
bpf:
  monitorAggregation: "none"
  masquerade: true
  ctAccounting: true
  lbAlgorithmAnnotation: true
  lbModeAnnotation: false        # disable per-service annotation override
envoy:
  enabled: true
  dnsPolicy: ClusterFirstWithHostNet
  debug:
    admin:
      enabled: true
  nodeSelector:
    service.cilium.io/node: t2
envoyConfig:
  enabled: true
loadBalancer:
  acceleration: disabled
  dsrDispatch: ipip
  mode: dsr          # DSR on the T1→T2 leg; T2 (Envoy/tcpProxy/httpProxy) does SNAT toward backends
routingMode: native
autoDirectNodeRoutes: true
ipv4NativeRoutingCIDR: 10.0.0.0/8
kubeProxyReplacement: true
k8sServiceHost: auto
enterprise:
  featureGate:
    minimumMaturity: Alpha
  bgpControlPlane:
    enabled: true
    enableServiceHealthChecking: true
  bfd:
    enabled: true
  loadbalancer:
    enabled: true
  dnsProxyHA:
    enabled: false
    offlineMode:
      enabled: false
operator:
  replicas: 1
```

### Install

```bash
helm install ilb isovalent/cilium --version 1.18.7 -f values.yaml -n kube-system
```

> **DSR vs SNAT:** `mode: dsr` applies to the T1 (BPF) → T2 (Envoy) forwarding leg, where IPIP
> encapsulation carries the original client IP. However, T2 (Envoy) always performs **SNAT** when
> forwarding to the actual backends — regardless of whether the LBService uses `tcpProxy` or
> `httpProxy`. Backends therefore see the T2 node IP as the source, not the original client IP.
> This is expected behaviour and does not require any special configuration on the backend VMs.

> **Note:** Every `helm upgrade` requires these extra flags due to a nil pointer bug in the
> enterprise template:
> ```
> --set enterprise.dnsProxyHA.enabled=false
> --set enterprise.dnsProxyHA.offlineMode.enabled=false
> ```
> Include them in `values.yaml` (as shown above) to avoid issues.

### Verify

```bash
cilium status --wait
```

Expected output shows Cilium, Operator, and Envoy DaemonSet all `OK`.

---

## 4. Label ILB Nodes

Assign T1 (L3/L4) and T2 (L5-L7/Envoy) roles to the worker nodes:

```bash
kubectl label node kind-worker  service.cilium.io/node=t1
kubectl label node kind-worker2 service.cilium.io/node=t2
kubectl label node kind-worker3 service.cilium.io/node=t2
```

Verify:
```bash
kubectl get ciliumnodes --show-labels
cilium lb status -n kube-system
```

---

## 5. Configure Host Networking for VIP Access

The VIP pool (`172.20.0.0/24`) is outside the Kind bridge subnet (`172.19.0.0/16`).
Without extra configuration neither the host nor external nodes can reach VIPs, and the T1
BPF program will not delegate L7 processing to T2 for external clients.

> **Lab note:** The networking complexity in this section (IP forwarding, iptables rules,
> MASQUERADE, FRR relay) is specific to running ILB inside a Kind cluster on a single host.
> In a production deployment on real nodes, BGP handles VIP reachability end-to-end and
> none of these workarounds are needed.

### 5a. Enable IP forwarding

The host must act as a router — forwarding packets between `ens192` (physical NIC) and the
Kind bridge.

```bash
# Enable immediately
sudo sysctl -w net.ipv4.ip_forward=1

# Persist across reboots
echo "net.ipv4.ip_forward = 1" | sudo tee /etc/sysctl.d/99-ilb.conf
sudo sysctl -p /etc/sysctl.d/99-ilb.conf
```

### 5b. Install and configure FRR on the host

Running FRR on the ILB host as a BGP relay eliminates the need for static routes — VIP
prefixes are learned dynamically from the ILB T1 node and redistributed to the upstream
router. This also mirrors how a real network would work.

**BGP topology:**
```
  T1 node              Host FRR             Upstream BGP router
  ASN 64512            ASN 65200            ASN 65220
  172.19.0.5  ──eBGP──► 172.19.0.1  ──eBGP──► 192.168.33.1
  (Kind bridge)         (bridge GW)           (infra router)
  advertises            relays VIP /32s
  VIP /32s              installs them
                        in kernel routing
```

Install FRR:
```bash
sudo dnf install -y frr
```

Enable the BGP daemon:
```bash
sudo sed -i 's/^bgpd=no/bgpd=yes/' /etc/frr/daemons
```

Write `/etc/frr/frr.conf`:
```
frr version 8.5.3
frr defaults traditional
hostname ilb-host
log syslog informational
service integrated-vtysh-config

router bgp 65200
 bgp router-id 192.168.33.24
 bgp log-neighbor-changes
 no bgp ebgp-requires-policy
 no bgp network import-check

 ! Peer group for ILB T1/T1-T2 nodes — define before bgp listen range
 neighbor ilb-nodes peer-group
 neighbor ilb-nodes remote-as 64512
 neighbor ilb-nodes ebgp-multihop 3
 neighbor ilb-nodes update-source 172.19.0.1

 ! Accept BGP sessions from any node in the Kind bridge subnet
 bgp listen range 172.19.0.0/16 peer-group ilb-nodes

 ! Upstream FRR router
 neighbor 192.168.33.1 remote-as 65220
 neighbor 192.168.33.1 ebgp-multihop 3
 neighbor 192.168.33.1 update-source 192.168.33.24

 address-family ipv4 unicast
  neighbor ilb-nodes activate
  neighbor ilb-nodes soft-reconfiguration inbound
  neighbor ilb-nodes route-map ACCEPT-ALL in
  neighbor ilb-nodes route-map ACCEPT-ALL out

  neighbor 192.168.33.1 activate
  neighbor 192.168.33.1 soft-reconfiguration inbound
  neighbor 192.168.33.1 route-map ACCEPT-ALL in
  neighbor 192.168.33.1 route-map ACCEPT-ALL out
 exit-address-family

route-map ACCEPT-ALL permit 10

line vty
```

Start and enable FRR:
```bash
sudo systemctl enable --now frr
```

Verify FRR is running and BGP sessions come up (after ILB BGP is configured in section 8):
```bash
sudo vtysh -c "show bgp summary"
sudo vtysh -c "show bgp ipv4 unicast"
# VIP /32 routes should appear with next-hop 172.19.x.x (T1 node)
ip route show | grep "proto bgp"
```

> **`ebgp-multihop 3`** is required on both the ILB side and the host FRR side. The BGP
> session between the T1 node and the host bridge IP crosses multiple hops through the
> Docker/Kind bridging layer, exceeding the default eBGP TTL of 1.

> The host FRR `bgp listen range` directive accepts dynamic BGP connections from any IP in
> `172.19.0.0/16`. This means the config does not need updating if the T1 node IP changes
> after a cluster recreation.

### 5c. iptables rules for external access

Find the Kind bridge name (it is generated and changes if the cluster is recreated):
```bash
docker network ls | grep kind-cilium
ip link show type bridge | grep br-
```

Apply both rules:
```bash
BRIDGE=br-2ef19183f3a9   # replace with your bridge name (from above)
NIC=ens192               # replace with your physical NIC name

# Allow forwarding from physical NIC into Kind bridge toward VIPs
# Docker FORWARD policy is DROP by default
sudo iptables -I FORWARD 1 -i $NIC -o $BRIDGE -d 172.20.0.0/24 -j ACCEPT

# Masquerade external source IPs as the bridge gateway IP (172.19.0.1)
# T1's BPF delegate-if-local flag only delegates to T2 when source IP is
# in the Kind bridge subnet — MASQUERADE rewrites any external src to 172.19.0.1
# so T1 treats the traffic as local and correctly delegates L7 to T2/Envoy.
sudo iptables -t nat -A POSTROUTING -o $BRIDGE -d 172.20.0.0/24 -j MASQUERADE
```

> The MASQUERADE destination is the VIP subnet — at `POSTROUTING` time the destination is
> still the VIP address (BPF DNAT runs inside the bridge, after this hook).

Verify after a test request (packet counters should be non-zero):
```bash
sudo iptables -L FORWARD -n -v | grep "172.20"
sudo iptables -t nat -L POSTROUTING -n -v | grep "172.20"
```

Persist:
```bash
sudo dnf install -y iptables-services
sudo iptables-save | sudo tee /etc/sysconfig/iptables
sudo systemctl enable iptables
```

### 5d. Route on external nodes

On each external node that needs to reach VIPs, add a route pointing the VIP pool at the
ILB host:

```bash
sudo ip route add 172.20.0.0/24 via 192.168.33.24
```

In a real environment this route would be learned automatically via BGP from the upstream
router. In this lab it must be added manually since the external test node is not a BGP
speaker.

### 5e. Proxy environment variable

If the host has `http_proxy` set (corporate proxy), curl routes VIP requests through it.
Bypass with:

```bash
no_proxy='*' curl http://172.20.0.1:8080/
```

Or add the VIP subnet to `no_proxy` permanently in `/etc/environment`:
```
no_proxy=172.20.0.0/24,localhost,127.0.0.1
```

### Routing overview

```
  External node          ILB Host                        Kind cluster
  192.168.33.10          192.168.33.24                   172.19.0.0/16
        │                      │                               │
        │  route: 172.20/24    │                               │
        │  via 192.168.33.24   │                               │
        │ ───────────────────► │                               │
        │                      │  iptables FORWARD ACCEPT      │
        │  src:192.168.33.10   │  -i ens192 -o br-XXXX         │
        │  dst:172.20.0.1 ────►│  -d 172.20.0.0/24             │
        │                      │                               │
        │                      │  iptables MASQUERADE          │
        │                      │  -o br-XXXX                   │
        │                      │  dst 172.20.0.0/24            │
        │                      │  → src rewritten to           │
        │                      │    172.19.0.1 (bridge GW)     │
        │                      │                               │
        │                      │  src:172.19.0.1 ─────────────►│ T1 sees local src
        │                      │  dst:172.20.0.1               │ → delegates L7 to T2
        │                      │                               │ → Envoy handles L7
        │                      │                               │
        │                      │                               │ T2 → backend VM
        │◄─────────────────────│◄──────────────────────────────│ 192.168.39.221:8080
                  response

  BGP control plane (dynamic — no static VIP routes needed on host):
  ┌────────────────────────────────────────────────────────────────────────┐
  │  T1 (172.19.0.5) ──eBGP──► Host FRR (172.19.0.1) ──eBGP──► FRR router │
  │  ASN 64512                  ASN 65200               ASN 65220           │
  │  advertises 172.20.0.x/32   installs /32 in kernel  receives /32 routes │
  │  next-hop: 172.19.0.5       relays upstream                             │
  └────────────────────────────────────────────────────────────────────────┘
```

---

## 6. IP Pool and VIP

### LBIPPool — choosing the VIP subnet

The `CiliumLoadBalancerIPPool` defines the CIDR from which VIP addresses are allocated.
This subnet must be:

- **Not overlapping** with the Kind bridge subnet (`172.19.0.0/16`), pod CIDR (`10.0.0.0/8`),
  or any existing network on the host
- **Routable from wherever clients will connect** — BGP advertises individual VIP addresses
  (`/32`) from this pool with the T1 node as next-hop, so the upstream router (FRR) must be
  able to reach it
- **Outside the Kind bridge subnet** — if you put VIPs inside `172.19.0.0/16`, they would
  appear directly connected to the bridge and BGP advertisements may be ignored by the
  upstream router. A dedicated subnet like `172.20.0.0/24` makes routing unambiguous

> In this lab `172.20.0.0/24` is used. It does not exist on any interface — it is purely
> a BGP-advertised range. The host needs a static route pointing it at the T1 node
> (see section 5c), and external nodes need a route pointing it at the host (section 5d).

The pool is cluster-scoped (no namespace). Each `LBVIP` requests one address from it.
If `ipv4Request` is omitted, the next available address is assigned automatically.

```yaml
apiVersion: cilium.io/v2
kind: CiliumLoadBalancerIPPool
metadata:
  name: vip-pool
spec:
  blocks:
    - cidr: 172.20.0.0/24
```

```yaml
apiVersion: isovalent.com/v1alpha1
kind: LBVIP
metadata:
  name: first
  namespace: default
spec:
  ipv4Request: 172.20.0.1    # omit to auto-assign next available IP
```

Verify the IP was assigned:
```bash
kubectl get lbvip first -o jsonpath='{.status.addresses}'
```

---

## 7. Backend Servers (nginx)

This lab uses two external backend VMs:

| VM | IP | Port |
|----|-----|------|
| ilb-server1 | `192.168.39.221` | 8080 |
| ilb-server2 | `192.168.39.222` | 8080 |

Adjust these addresses to match your environment — they are referenced in the
`LBBackendPool` manifest in section 9 and must be reachable from the T2 nodes.

Deploy nginx on your backend VMs. The config listens on port 8080 and provides:
- `/` — HTML page showing hostname, IP, request info (useful for verifying load balancing)
- `/health` — returns `200 OK` (canonical health check)
- `/healtz` — returns `200 OK` (matches the typo used in example backend pool configs)

### nginx.conf

```nginx
events {}

http {
    server {
        listen 8080;

        # Default page — identifies this server instance
        location / {
            default_type text/html;
            return 200 '<!DOCTYPE html>
<html>
<head><title>Backend: $hostname</title></head>
<body>
  <h1>Backend Server</h1>
  <p><strong>Host:</strong> $hostname</p>
  <p><strong>Address:</strong> $server_addr:$server_port</p>
  <p><strong>Request:</strong> $request</p>
  <p><strong>Client:</strong> $remote_addr</p>
</body>
</html>';
        }

        # Health check endpoint (note: path matches /healtz typo in firstpool)
        location /healtz {
            default_type text/plain;
            return 200 'OK';
        }

        # Canonical health check path
        location /health {
            default_type text/plain;
            return 200 'OK';
        }
    }
}
```

For external VMs, save as `/etc/nginx/nginx.conf` and restart nginx:
```bash
sudo systemctl restart nginx
```

For in-cluster backends, create a ConfigMap and Deployment:

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: nginx-config
  namespace: default
data:
  nginx.conf: |
    events {}

    http {
        server {
            listen 8080;

            location / {
                default_type text/html;
                return 200 '<!DOCTYPE html>
    <html>
    <head><title>Backend: $hostname</title></head>
    <body>
      <h1>Backend Server</h1>
      <p><strong>Host:</strong> $hostname</p>
      <p><strong>Address:</strong> $server_addr:$server_port</p>
      <p><strong>Request:</strong> $request</p>
      <p><strong>Client:</strong> $remote_addr</p>
    </body>
    </html>';
            }

            location /healtz {
                default_type text/plain;
                return 200 'OK';
            }

            location /health {
                default_type text/plain;
                return 200 'OK';
            }
        }
    }
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: backend
  namespace: default
spec:
  replicas: 2
  selector:
    matchLabels:
      app: backend
  template:
    metadata:
      labels:
        app: backend
    spec:
      containers:
      - name: nginx
        image: nginx:1.27
        ports:
        - containerPort: 8080
        volumeMounts:
        - name: config
          mountPath: /etc/nginx/nginx.conf
          subPath: nginx.conf
      volumes:
      - name: config
        configMap:
          name: nginx-config
---
apiVersion: v1
kind: Service
metadata:
  name: backend
  namespace: default
spec:
  selector:
    app: backend
  ports:
  - port: 8080
    targetPort: 8080
```

Apply:
```bash
kubectl apply -f backend.yaml
```

---

## 8. Configure BGP

BGP peering is configured via four CRDs applied together:

```yaml
apiVersion: isovalent.com/v1alpha1
kind: IsovalentBFDProfile
metadata:
  name: ilb-profile
spec:
  detectMultiplier: 3
  minimumTTL: 255
  receiveIntervalMilliseconds: 300
  transmitIntervalMilliseconds: 300
---
apiVersion: isovalent.com/v1
kind: IsovalentBGPAdvertisement
metadata:
  name: ilb-advertisement
  labels:
    advertise: ilb
spec:
  advertisements:
  - advertisementType: Service
    service:
      addresses:
      - LoadBalancerIP
    selector:
      matchExpressions:
        - key: loadbalancer.isovalent.com/vip-name
          operator: Exists
---
apiVersion: isovalent.com/v1
kind: IsovalentBGPPeerConfig
metadata:
  name: ilb-peer-config
spec:
  bfdProfileRef: ilb-profile
  families:
    - afi: ipv4
      safi: unicast
      advertisements:
        matchLabels:
          advertise: ilb
  ebgpMultihop: 3
  timers:
    connectRetryTimeSeconds: 1
---
apiVersion: isovalent.com/v1
kind: IsovalentBGPClusterConfig
metadata:
  name: router-bgp
spec:
  bgpInstances:
    - name: instance0
      localASN: 64512
      peers:
        - name: peer0
          peerAddress: 172.19.0.1
          peerASN: 65200
          peerConfigRef:
            name: ilb-peer-config
  nodeSelector:
    matchExpressions:
    - key: service.cilium.io/node
      operator: In
      values:
      - t1
      - t1-t2
```

Apply:
```bash
kubectl apply -f bgp.yaml
```

This creates:
- **IsovalentBFDProfile** — BFD liveness detection (300ms intervals, TTL 255, multiplier 3)
- **IsovalentBGPPeerConfig** — peer config referencing BFD profile, eBGP multihop 3
- **IsovalentBGPAdvertisement** — advertises `LoadBalancerIP` addresses for all LBVIPs
- **IsovalentBGPClusterConfig** — peers with `172.19.0.1` (host FRR, ASN 65200) from T1 nodes (local ASN 64512)

### Host FRR configuration

The full host FRR config is in section 5b. Key points:

- ILB T1 nodes connect to `172.19.0.1` (Kind bridge gateway). Host FRR accepts any T1/T1-T2
  IP via `bgp listen range 172.19.0.0/16 peer-group ilb-nodes` — no fixed node IP needed.
- Host FRR (ASN 65200) relays VIP `/32` routes to the upstream router (`192.168.33.1`).
- VIP host routes are installed in the kernel automatically (`proto bgp`).

> **`ebgp-multihop 3` is required** on the ILB side. The BGP session from T1 to the bridge
> gateway traverses the Kind/Docker bridge and exceeds eBGP's default TTL of 1.

### Upstream FRR router configuration

On the upstream FRR router (`192.168.33.1`), peer with the host FRR:

```
router bgp 65220
  router-id 192.168.33.1
  neighbor 192.168.33.24 remote-as 65200
  neighbor 192.168.33.24 ebgp-multihop 3
  !
  address-family ipv4 unicast
    neighbor 192.168.33.24 activate
  exit-address-family
!
! Route to Kind bridge subnet — needed to reach T1 next-hop in VIP route advertisements
ip route 172.19.0.0/16 192.168.33.24
```

> Peer address is `192.168.33.24` (host physical NIC) at ASN `65200` (host FRR).
> The static route to `172.19.0.0/16` is needed so the upstream router can forward traffic
> to the T1 node next-hop (`172.19.0.5`) carried in the VIP `/32` advertisements.

Verify peering:
```bash
cilium bgp peers
# Expected: kind-worker   64512   65200   172.19.0.1   established

cilium bgp routes advertised
# Expected: 172.20.0.x/32 routes listed

sudo vtysh -c "show bgp summary"
# Expected: 172.19.0.5 (T1) and 192.168.33.1 (upstream) both Established

ip route show | grep "proto bgp"
# Expected: 172.20.0.1 and 172.20.0.2 via 172.19.0.5 proto bgp
```

---

## 9. Create a Load-Balanced Service

### Backend pool — external VM backends (IP type)

Use this when backends are external VMs (not running inside the cluster).
The health check path `/healtz` matches the nginx config above.

```yaml
apiVersion: isovalent.com/v1alpha1
kind: LBBackendPool
metadata:
  name: firstpool
  namespace: default
spec:
  backendType: IP
  backends:
    - ip: 192.168.39.221
      port: 8080
      weight: 1
    - ip: 192.168.39.222
      port: 8080
      weight: 1
  healthCheck:
    http:
      host: lb
      method: GET
      path: /healtz
    healthyThreshold: 2
    unhealthyThreshold: 2
    intervalSeconds: 60
    timeoutSeconds: 10
  loadbalancing:
    algorithm:
      roundRobin: {}
```

### Backend pool — in-cluster Kubernetes service (K8sService type)

Use this when backends are running inside the cluster as a Kubernetes Service.
The `k8sServiceRef.name` must match the Kubernetes Service name (e.g. `backend` from
section 7).

```yaml
apiVersion: isovalent.com/v1alpha1
kind: LBBackendPool
metadata:
  name: firstpool
  namespace: default
spec:
  backendType: K8sService
  backends:
    - k8sServiceRef:
        name: backend
      port: 8080
      weight: 1
  healthCheck:
    http:
      path: /health
    healthyThreshold: 1
    unhealthyThreshold: 2
    intervalSeconds: 10
```

### LBService

References the VIP and backend pool by name. The `backendRef.name` must match the
`LBBackendPool` name above (`firstpool`).

```yaml
apiVersion: isovalent.com/v1alpha1
kind: LBService
metadata:
  name: first
  namespace: default
spec:
  vipRef:
    name: first
  port: 8080
  applications:
    httpProxy:
      routes:
        - backendRef:
            name: firstpool
          match: {}
```

Apply:
```bash
kubectl apply -f lbservice.yaml
```

Verify:
```bash
cilium lb status
kubectl get lbservice first -o jsonpath='{.status}'
```

---

## 10. ILB GUI

The ILB GUI is a single-page web app (`index.html` + `server.py`) for managing ILB CRDs.

### Deploy to the cluster

Build and load the image (run from the `ilb-gui/` directory):

```bash
# Copy CLI binaries into build context
mkdir -p bin
cp /usr/local/bin/kubectl /usr/local/bin/cilium bin/

# Build and load into Kind
DOCKER_API_VERSION=1.45 docker build -t ilb-gui:latest .
DOCKER_API_VERSION=1.45 kind load docker-image ilb-gui:latest --name kind

# Deploy RBAC and workload
kubectl apply -f gui-deployment/rbac.yaml
kubectl apply -f gui-deployment/deployment.yaml
kubectl apply -f gui-deployment/lbservice.yaml # change the VIP address
```

The GUI is exposed as an LBService on `172.20.0.2:8080`.

### Run locally (development)

```bash
cd ilb-gui/
./start.sh
# Open http://localhost:8080
```

This expects `kubectl` and `clilium` commands available.
 
---

## Troubleshooting


### T1 delegate-if-local

The T1 BPF program has a `delegate-if-local` flag. It only delegates L7 processing to T2
(Envoy) when the source IP appears to be in the Kind subnet (`172.19.0.0/16`). Traffic from
external nodes with IPs outside this subnet will not be delegated and times out.

Fix: MASQUERADE external traffic as `172.19.0.1` (the Kind bridge IP) before it enters the
bridge — see [Section 5c](#5c-iptables-rules-for-external-access).

### Kind bridge name

The Kind bridge name (`br-2ef19183f3a9` in this lab) is generated and may differ between
cluster recreations. Find yours with:
```bash
docker network ls | grep kind-cilium
# or
ip link show type bridge | grep br-
```

Update the iptables rules accordingly when recreating the cluster. The host FRR config uses
`bgp listen range 172.19.0.0/16` so it does **not** need updating when the T1 node IP
changes — BGP reconnects automatically.

### BGP not establishing after cluster recreation

If the cluster is recreated, the T1 node gets a new IP. Host FRR will accept the new session
automatically (via `bgp listen range`). If the session does not come up:

```bash
# Check host FRR sees the session attempt
sudo vtysh -c "show bgp summary"
sudo journalctl -u frr --since "5 minutes ago" | grep -i bgp

# Check ILB BGP config still points to 172.19.0.1
kubectl get isovalentbgpclusterconfig router-bgp -o jsonpath='{.spec.bgpInstances[0].peers[0].peerAddress}'

# Restart FRR if needed
sudo systemctl restart frr
```
