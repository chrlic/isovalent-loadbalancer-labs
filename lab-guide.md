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
  ┌──────────────────────────────────────────────────────────────────────┐
  │                                                                      │
  │   ┌─────────────────┐          ┌──────────────────────────────────┐  │
  │   │   FRR Router    │  BGP     │        ILB Host (RHEL)           │  │
  │   │  192.168.33.1   │◄────────►│       192.168.33.24 (ens192)     │  │
  │   │   ASN 65220     │          │                                  │  │
  │   └─────────────────┘          │  Kind bridge br-XXXX             │  │
  │                                │  172.19.0.1  (172.19.0.0/16)     │  │
  │   ┌─────────────────┐          │  ┌────────────────────────────┐  │  │
  │   │  External Node  │          │  │  kind-worker  172.19.0.4   │  │  │
  │   │  192.168.33.10  │          │  │  T1  (L3/L4 BPF LB)        │  │  │
  │   └─────────────────┘          │  ├────────────────────────────┤  │  │
  │                                │  │  kind-worker2 172.19.0.3   │  │  │
  │                                │  │  T2  (L5-L7 Envoy)         │  │  │
  │                                │  ├────────────────────────────┤  │  │
  │                                │  │  kind-worker3 172.19.0.2   │  │  │
  │                                │  │  T2  (L5-L7 Envoy)         │  │  │
  │                                │  ├────────────────────────────┤  │  │
  │                                │  │  control-plane 172.19.0.5  │  │  │
  │                                │  └────────────────────────────┘  │  │
  │                                │                                  │  │
  │                                │  VIP pool: 172.20.0.0/24         │  │
  │                                │    172.20.0.1  → first service   │  │
  │                                │    172.20.0.2  → ilb-gui         │  │
  │                                └──────────────────────────────────┘  │
  │                                                                      │
  └──────────────────────────────────────────────────────────────────────┘

  Backend Network 192.168.39.0/24
  ┌──────────────────────────────────────┐
  │  ilb-server1  192.168.39.221:8080    │
  │  ilb-server2  192.168.39.222:8080    │
  └──────────────────────────────────────┘
```

**Address summary:**

| Component         | Address / Subnet       | Notes                          |
|-------------------|------------------------|--------------------------------|
| ILB host (ens192) | 192.168.33.24          | Physical NIC                   |
| ILB host (bridge) | 172.19.0.1             | Kind bridge gateway            |
| Kind subnet       | 172.19.0.0/16          | Docker bridge network          |
| T1 node           | 172.19.0.4             | kind-worker, L3/L4             |
| T2 nodes          | 172.19.0.3, .0.2       | kind-worker2/3, L5-L7/Envoy   |
| VIP pool          | 172.20.0.0/24          | BGP-advertised                 |
| BGP router        | 192.168.33.1 ASN 65220 | FRR                            |
| ILB local ASN     | 64512                  | Advertised from T1 node        |
| Backend VMs       | 192.168.39.221/222     | External, port 8080            |
| External client   | 192.168.33.10          | Test node                      |

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
  mode: dsr
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

### 5a. Enable IP forwarding

> **Lab note:** The networking complexity in this entire section (IP forwarding, host routes,
> iptables rules, MASQUERADE) is specific to running ILB inside a Kind cluster on a single
> host. In a production deployment — whether ILB runs on a real Kubernetes cluster with
> dedicated nodes, or on standalone VMs — the nodes have their own routable IPs, BGP handles
> VIP reachability end-to-end, and none of these workarounds are needed.

The host must act as a router — forwarding packets between `ens192` (physical NIC) and the
Kind bridge. Docker enables this automatically for its own networks, but it is good practice
to set it explicitly and persist it.

```bash
# Enable immediately
sudo sysctl -w net.ipv4.ip_forward=1

# Persist across reboots
echo "net.ipv4.ip_forward = 1" | sudo tee /etc/sysctl.d/99-ilb.conf
sudo sysctl -p /etc/sysctl.d/99-ilb.conf
```

Verify:
```bash
sysctl net.ipv4.ip_forward
# Expected: net.ipv4.ip_forward = 1
```

### Routing overview

```
  External node                 ILB Host                      Kind cluster
  192.168.33.10                 192.168.33.24                 172.19.0.0/16
        │                             │                              │
        │  ① route on ext. node      │                              │
        │  172.20.0.0/24              │                              │
        │  via 192.168.33.24          │                              │
        │ ──────────────────────────► │                              │
        │                             │  ② route on ILB host        │
        │                             │  172.20.0.0/24               │
        │                             │  via 172.19.0.4 ────────────►│ T1 (172.19.0.4)
        │                             │                              │
        │  src:192.168.33.10          │  ③ iptables FORWARD ACCEPT  │
        │  dst:172.20.0.1 ───────────►│  -i ens192 -o br-XXXX        │
        │                             │  -d 172.20.0.0/24            │
        │                             │  (Docker blocks by default)  │
        │                             │                              │
        │                             │  ④ iptables MASQUERADE      │
        │                             │  -o br-XXXX                  │
        │                             │  src 192.168.33.0/24         │
        │                             │  dst 172.20.0.0/24           │
        │                             │  → rewrites src to           │
        │                             │    172.19.0.1 (bridge IP)    │
        │                             │                              │
        │                             │  src:172.19.0.1 ────────────►│ T1 sees local src
        │                             │  dst:172.20.0.1              │ → delegates to T2
        │                             │                              │ → Envoy handles L7
        │                             │                              │
        │                             │                              │ T2 → backend VM
        │                             │                              │ 192.168.39.221:8080
        │◄────────────────────────────│◄─────────────────────────────│
                    response

  BGP control plane:
  ┌─────────────────────────────────────────────────────────────────────┐
  │  T1 (172.19.0.4) ──BGP──► FRR (192.168.33.1)                        │
  │  advertises 172.20.0.x/32 with next-hop 172.19.0.4                  │
  │                                                                     │
  │  FRR needs a route to 172.19.0.0/16 via 192.168.33.24 (ILB host)    │
  │  so it can forward traffic destined to T1 back into the cluster     │
  └─────────────────────────────────────────────────────────────────────┘
```

**Why each rule is needed:**

| # | Applied on | Rule | Reason |
|---|------------|------|--------|
| ① | External node | `ip route add 172.20.0.0/24 via 192.168.33.24` | VIP pool not in routing table — packets never leave the external node |
| ② | ILB host | `ip route add 172.20.0.0/24 via 172.19.0.4` | VIP pool not in Kind bridge subnet — without this the host sends VIP traffic to the default gateway (FRR), not to T1 |
| ③ | ILB host | `iptables FORWARD ACCEPT -i ens192 -o br-XXXX -d 172.20.0.0/24` | Docker FORWARD policy is DROP; Docker only ACCEPTs traffic *from* bridges, not *into* them |
| ④ | ILB host | `iptables MASQUERADE -o br-XXXX -s 192.168.33.0/24 -d 172.20.0.0/24` | T1 BPF `delegate-if-local` only delegates to T2 when source IP is in the Kind subnet. External IPs fail this check — MASQUERADE rewrites src to 172.19.0.1 so T1 treats the traffic as local |

### 5b. Find Kind node addresses

The routes in sections ② and ③ require the IP address of the T1 node inside the Kind
cluster. Kind assigns IPs from the bridge subnet dynamically — find them with:

```bash
kubectl get nodes -o yaml | grep -A5 "addresses:"
```

Look for the `InternalIP` entry for each node, e.g.:

```
addresses:
- address: 172.19.0.4
  type: InternalIP
- address: kind-worker
  type: Hostname
```

You can also get a compact view:
```bash
kubectl get nodes -o wide
```

The T1 node is whichever node has the label `service.cilium.io/node=t1`:
```bash
kubectl get nodes -l service.cilium.io/node=t1 -o wide
```

Use the `INTERNAL-IP` column value as the gateway for the host route below.

### 5c. Host route to VIP pool

The VIP pool (`172.20.0.0/24`) is a dedicated subnet that does not exist on any physical
interface — it is defined in section 6 as a `CiliumLoadBalancerIPPool` and individual VIP
addresses from it are advertised via BGP by the T1 node. Without a host route, the ILB
host itself has no idea how to reach VIPs (it would send them to the default gateway), so
traffic from the host to any VIP would never reach T1.

```bash
sudo ip route add 172.20.0.0/24 via 172.19.0.4 # replace with your T1 node IP
```

Persist with NetworkManager:
```bash
# Check connection name with: nmcli connection show
nmcli connection modify ens192 +ipv4.routes "172.20.0.0/24 172.19.0.4"
nmcli connection up ens192
```

### 5d. iptables rules for external access

Find the Kind bridge name first (it is generated and changes if the cluster is recreated):
```bash
ip link show type bridge | grep br-
docker network ls | grep kind-cilium
```

The bridge is the one with the `kind-cilium` id, it's status is UP.

Apply both rules:
```bash
BRIDGE=br-2ef19183f3a9   # replace with your bridge name (from above)
NIC=ens192               # replace with your physical NIC name

# ③ Allow forwarding from physical NIC into Kind bridge toward VIPs
sudo iptables -I FORWARD 1 -i $NIC -o $BRIDGE -d 172.20.0.0/24 -j ACCEPT

# ④ Masquerade all external source IPs as the bridge IP (172.19.0.1)
#    T1's BPF delegate-if-local flag only delegates to T2 when source IP is
#    in the Kind bridge subnet — MASQUERADE rewrites any external src to 172.19.0.1
#    so T1 treats the traffic as local and correctly delegates L7 to T2/Envoy.
#    No -s filter needed — this rule only fires for traffic going out the Kind bridge
#    toward VIPs, so it won't affect unrelated traffic regardless of source subnet.
sudo iptables -t nat -A POSTROUTING -o $BRIDGE -d 172.20.0.0/24 -j MASQUERADE
```

> The MASQUERADE rule matches the VIP subnet as destination — at `POSTROUTING` time the
> destination is still the VIP (BPF DNAT runs inside the bridge, after this hook).

Verify the rules matched after a test curl from an external node (packet counters should be non-zero):
```bash
sudo iptables -L FORWARD -n -v | grep "172.20"
sudo iptables -t nat -L POSTROUTING -n -v | grep "172.20"
```

If counters stay at zero, the traffic is not hitting these rules — check that `BRIDGE` and `NIC` names are correct and that the external node has the route from section 5e.

Persist the iptables rules:
```bash
sudo dnf install -y iptables-services
sudo iptables-save | sudo tee /etc/sysconfig/iptables
sudo systemctl enable iptables
```

### 5e. Route on external nodes

On each external node that needs to reach VIPs, add a route pointing the VIP pool at the
ILB host's physical IP:

```bash
# Replace 192.168.33.24 with the ILB host's IP on the shared subnet
sudo ip route add 172.20.0.0/24 via 192.168.33.24
```

Verify connectivity:
```bash
# Check packet proxy is not intercepting VIP traffic
no_proxy='*' curl http://172.20.0.1:8080/
```

### 5f. Proxy environment variable

If the host has `http_proxy` set (corporate proxy), curl routes VIP requests through it.
Bypass with:

```bash
no_proxy='*' curl http://172.20.0.1:8080/
```

Or add the VIP subnet to `no_proxy` permanently in `/etc/environment`:
```
no_proxy=172.20.0.0/24,localhost,127.0.0.1
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
> (see section 5c), and external nodes need a route pointing it at the host (section 5e).

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
          peerAddress: 192.168.33.1
          peerASN: 65220
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
- **IsovalentBGPClusterConfig** — peers with `192.168.33.1` (ASN 65220) from T1 nodes (local ASN 64512)

Verify peering:
```bash
cilium bgp peers
cilium bgp routes advertised
```

> BGP routes are advertised with next-hop set to the T1 node IP (`172.19.0.4`).
> The FRR router (`192.168.33.1`) must have a route back to `172.19.0.0/16` via this host
> (`192.168.33.24`) for return traffic to work.

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

Fix: MASQUERADE external traffic as `172.19.0.1` (the kind bridge IP) before it enters the
bridge — see [Section 5c](#5c-iptables-rules-for-external-access).

### Kind bridge name

The Kind bridge name (`br-2ef19183f3a9` in this lab) is generated and may differ between
cluster recreations. Find yours with:
```bash
docker network ls | grep kind-cilium
# or
ip link show type bridge | grep br-
```

Update the iptables rules and route accordingly when recreating the cluster.
