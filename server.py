#!/usr/bin/env python3
"""
Backend API server for Isovalent LB GUI.
Proxies kubectl and cilium commands to manage LB CRDs.
"""

import json
import subprocess
import sys
import os
import threading
import time
import ssl
import urllib.request
import urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import traceback
from datetime import datetime, timezone

PORT = int(os.environ.get("PORT", 8080))
KUBECTL = os.environ.get("KUBECTL", "kubectl")
CILIUM  = os.environ.get("CILIUM",  "cilium")

# ── Observability config ──────────────────────────────────────────────────────
METRICS_INTERVAL   = int(os.environ.get("METRICS_INTERVAL", "30"))   # seconds

SPLUNK_HEC_URL     = os.environ.get("SPLUNK_HEC_URL", "")            # e.g. https://splunk:8088/services/collector
SPLUNK_HEC_TOKEN   = os.environ.get("SPLUNK_HEC_TOKEN", "")
SPLUNK_HEC_INDEX   = os.environ.get("SPLUNK_HEC_INDEX", "")          # optional
SPLUNK_HEC_INTERVAL= int(os.environ.get("SPLUNK_HEC_INTERVAL", "60"))# seconds
SPLUNK_VERIFY_SSL  = os.environ.get("SPLUNK_VERIFY_SSL", "true").lower() not in ("false", "0", "no")


def run_kubectl(args, stdin_data=None):
    """Run kubectl with given args, return (stdout, stderr, returncode)."""
    cmd = [KUBECTL] + args
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
            input=stdin_data,
        )
        return result.stdout, result.stderr, result.returncode
    except FileNotFoundError:
        return "", f"kubectl not found at: {KUBECTL}", 1
    except subprocess.TimeoutExpired:
        return "", "kubectl command timed out", 1


def run_cilium(args):
    """Run cilium CLI with given args, return (stdout, stderr, returncode)."""
    cmd = [CILIUM] + args
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.stdout, result.stderr, result.returncode
    except FileNotFoundError:
        return "", f"cilium not found at: {CILIUM}", 1
    except subprocess.TimeoutExpired:
        return "", "cilium command timed out", 1


def kubectl_get_json(resource, namespace=None, name=None):
    """Get namespaced or all-namespace resource as JSON."""
    args = ["get", resource]
    if namespace:
        args += ["-n", namespace]
    else:
        args += ["-A"]
    if name:
        args.append(name)
    args += ["-o", "json"]
    stdout, stderr, rc = run_kubectl(args)
    if rc != 0:
        return None, stderr
    try:
        return json.loads(stdout), None
    except json.JSONDecodeError as e:
        return None, f"JSON parse error: {e}"


def kubectl_get_cluster_json(resource, name=None):
    """Get a cluster-scoped (non-namespaced) resource as JSON."""
    args = ["get", resource]
    if name:
        args.append(name)
    args += ["-o", "json"]
    stdout, stderr, rc = run_kubectl(args)
    if rc != 0:
        return None, stderr
    try:
        return json.loads(stdout), None
    except json.JSONDecodeError as e:
        return None, f"JSON parse error: {e}"


def get_namespaces():
    stdout, stderr, rc = run_kubectl(["get", "namespaces", "-o", "json"])
    if rc != 0:
        return []
    try:
        data = json.loads(stdout)
        return [item["metadata"]["name"] for item in data.get("items", [])]
    except Exception:
        return []


# Resources that are cluster-scoped (no namespace)
CLUSTER_SCOPED = {"/api/lbippools"}

# Full resource map including BGP CRDs and LB IP Pool
RESOURCE_MAP = {
    "/api/lbservices":       "lbservices.isovalent.com",
    "/api/lbvips":           "lbvips.isovalent.com",
    "/api/lbbackendpools":   "lbbackendpools.isovalent.com",
    "/api/lbdeployments":    "lbdeployments.isovalent.com",
    "/api/bgpclusterconfigs":  "isovalentbgpclusterconfigs.isovalent.com",
    "/api/bgppeerconfigs":     "isovalentbgppeerconfigs.isovalent.com",
    "/api/bgpadvertisements":  "isovalentbgpadvertisements.isovalent.com",
    "/api/bfdprofiles":        "isovalentbfdprofiles.isovalent.com",
    "/api/lbippools":          "ciliumloadbalancerippools.cilium.io",
}


# ── Shared metrics cache ──────────────────────────────────────────────────────
_cache_lock   = threading.Lock()
_metrics_cache = {
    "lb_status":    None,   # parsed cilium lb status JSON
    "bgp_peers":    None,   # parsed cilium bgp peers JSON
    "inventory":    {},     # {resource: count}
    "collected_at": None,   # ISO timestamp
    "error":        None,
}


def collect_metrics():
    """Fetch all runtime + inventory data and update the shared cache."""
    result = {"collected_at": datetime.now(timezone.utc).isoformat(), "error": None}

    # cilium lb status
    try:
        stdout, stderr, rc = run_cilium(["lb", "status", "-o", "json"])
        result["lb_status"] = json.loads(stdout) if rc == 0 else None
    except Exception as e:
        result["lb_status"] = None

    # cilium bgp peers — returns dict {node: [peer, ...]}; flatten to list with node added
    try:
        stdout, stderr, rc = run_cilium(["bgp", "peers", "-o", "json"])
        if rc == 0:
            raw = json.loads(stdout)
            flat = []
            if isinstance(raw, dict):
                for node, peers in raw.items():
                    for p in (peers or []):
                        flat.append({**p, "node": node})
            elif isinstance(raw, list):
                flat = raw
            result["bgp_peers"] = flat
        else:
            result["bgp_peers"] = []
    except Exception:
        result["bgp_peers"] = []

    # inventory counts
    inventory = {}
    for resource, crd in [
        ("lbservices",      "lbservices.isovalent.com"),
        ("lbvips",          "lbvips.isovalent.com"),
        ("lbbackendpools",  "lbbackendpools.isovalent.com"),
        ("lbdeployments",   "lbdeployments.isovalent.com"),
        ("bgpclusterconfigs", "isovalentbgpclusterconfigs.isovalent.com"),
        ("bgppeerconfigs",    "isovalentbgppeerconfigs.isovalent.com"),
    ]:
        data, _ = kubectl_get_json(crd)
        inventory[resource] = len(data.get("items", [])) if data else 0
    for resource, crd in [
        ("lbippools", "ciliumloadbalancerippools.cilium.io"),
    ]:
        data, _ = kubectl_get_cluster_json(crd)
        inventory[resource] = len(data.get("items", [])) if data else 0

    result["inventory"] = inventory

    with _cache_lock:
        _metrics_cache.update(result)


def _collector_loop():
    """Background thread: refresh metrics cache on interval."""
    while True:
        try:
            collect_metrics()
        except Exception as e:
            print(f"[metrics] collection error: {e}", file=sys.stderr)
        time.sleep(METRICS_INTERVAL)


# ── Prometheus /metrics renderer ──────────────────────────────────────────────

def _prom_label(k, v):
    v = str(v).replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')
    return f'{k}="{v}"'

def _prom_labels(d):
    return '{' + ','.join(_prom_label(k, v) for k, v in d.items()) + '}'

def render_prometheus_metrics():
    with _cache_lock:
        cache = dict(_metrics_cache)

    lines = []

    def gauge(name, help_text, metric_type="gauge"):
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {metric_type}")

    def sample(name, labels, value):
        lines.append(f"{name}{_prom_labels(labels)} {value}")

    # ── Inventory gauges ──────────────────────────────────────────────────
    gauge("ilb_inventory_count", "Number of ILB CRD objects by resource type")
    for resource, count in (cache.get("inventory") or {}).items():
        sample("ilb_inventory_count", {"resource": resource}, count)

    # ── Per-service runtime metrics ───────────────────────────────────────
    lb = cache.get("lb_status") or {}
    services = lb.get("services", []) if isinstance(lb, dict) else []

    gauge("ilb_service_online", "1 if service status is ONLINE, 0 otherwise")
    gauge("ilb_service_backends_ok", "Number of healthy backends for the service")
    gauge("ilb_service_backends_total", "Total backends configured for the service")
    gauge("ilb_service_bgp_peers_ok", "Number of healthy BGP peers for the service")
    gauge("ilb_service_bgp_peers_total", "Total BGP peers for the service")
    gauge("ilb_service_t1_nodes_ok", "Number of healthy T1 nodes for the service")
    gauge("ilb_service_t1_nodes_total", "Total T1 nodes for the service")
    gauge("ilb_service_t2_nodes_ok", "Number of healthy T2 nodes for the service")
    gauge("ilb_service_t2_nodes_total", "Total T2 nodes for the service")

    for svc in services:
        if not svc:
            continue
        lbls = {
            "name":      svc.get("name", ""),
            "namespace": svc.get("namespace", ""),
            "vip":       svc.get("vip", ""),
            "port":      str(svc.get("port", "")),
            "type":      svc.get("type", ""),
        }
        sample("ilb_service_online",         lbls, 1 if svc.get("status") == "ONLINE" else 0)

        bgp   = svc.get("bgpPeerStatus") or {}
        sample("ilb_service_bgp_peers_ok",    lbls, bgp.get("ok", 0))
        sample("ilb_service_bgp_peers_total",  lbls, bgp.get("total", 0))

        t1    = svc.get("t1NodeStatus") or {}
        sample("ilb_service_t1_nodes_ok",     lbls, t1.get("ok", 0))
        sample("ilb_service_t1_nodes_total",   lbls, t1.get("total", 0))

        t2    = svc.get("t2NodeStatus") or {}
        sample("ilb_service_t2_nodes_ok",     lbls, t2.get("ok", 0))
        sample("ilb_service_t2_nodes_total",   lbls, t2.get("total", 0))

        # backend counts: httpProxy uses t2BackendHealthcheckStatus,
        # tcpProxy uses backendpoolStatus.groups[0]
        hc    = svc.get("t2BackendHealthcheckStatus") or {}
        pool  = svc.get("backendpoolStatus") or {}
        grp   = (pool.get("groups") or [{}])[0]
        be_ok    = hc.get("ok")    if hc.get("ok")    is not None else grp.get("ok", 0)
        be_total = hc.get("total") if hc.get("total") is not None else grp.get("total", 0)
        sample("ilb_service_backends_ok",     lbls, be_ok)
        sample("ilb_service_backends_total",   lbls, be_total)

    # ── BGP peer metrics ──────────────────────────────────────────────────
    bgp_peers = cache.get("bgp_peers") or []
    gauge("ilb_bgp_peer_established", "1 if BGP session is established, 0 otherwise")
    gauge("ilb_bgp_peer_prefixes_received", "Number of prefixes received from BGP peer")
    for peer in bgp_peers:
        if not peer:
            continue
        lbls = {
            "local_asn":    str(peer.get("local-asn", peer.get("localAsn", ""))),
            "peer_address": peer.get("peer-address", peer.get("peerAddress", "")),
            "peer_asn":     str(peer.get("peer-asn", peer.get("peerAsn", ""))),
            "node":         peer.get("node", ""),
        }
        session = peer.get("session-state", peer.get("sessionState", ""))
        sample("ilb_bgp_peer_established",
               lbls, 1 if session == "established" else 0)
        received = peer.get("num-received-routes", peer.get("numReceivedRoutes", 0)) or 0
        sample("ilb_bgp_peer_prefixes_received", lbls, received)

    # ── Scrape metadata ───────────────────────────────────────────────────
    gauge("ilb_scrape_timestamp_seconds", "Unix timestamp of last metrics collection")
    ts = cache.get("collected_at")
    if ts:
        try:
            from datetime import datetime, timezone
            dt = datetime.fromisoformat(ts)
            lines.append(f"ilb_scrape_timestamp_seconds{{}} {dt.timestamp():.3f}")
        except Exception:
            pass

    lines.append("")
    return "\n".join(lines)


# ── Full status export (used by /api/status/export and Splunk HEC) ─────────────

def build_status_export():
    """Return a dict with full inventory + runtime state."""
    with _cache_lock:
        cache = dict(_metrics_cache)

    export = {
        "collected_at": cache.get("collected_at"),
        "lb_status":    cache.get("lb_status"),
        "bgp_peers":    cache.get("bgp_peers"),
        "inventory":    cache.get("inventory") or {},
    }
    return export


# ── Splunk HEC forwarder ──────────────────────────────────────────────────────

def _splunk_post(payload: dict):
    """POST a single event to Splunk HEC."""
    event = {"event": payload, "time": time.time(), "sourcetype": "isovalent:ilb"}
    if SPLUNK_HEC_INDEX:
        event["index"] = SPLUNK_HEC_INDEX
    body = json.dumps(event).encode()
    req  = urllib.request.Request(
        SPLUNK_HEC_URL,
        data=body,
        headers={
            "Authorization": f"Splunk {SPLUNK_HEC_TOKEN}",
            "Content-Type":  "application/json",
        },
        method="POST",
    )
    ctx = ssl.create_default_context() if SPLUNK_VERIFY_SSL else ssl._create_unverified_context()
    with urllib.request.urlopen(req, context=ctx, timeout=10) as resp:
        return resp.status


def _splunk_loop():
    """Background thread: push status export to Splunk HEC on interval."""
    print(f"[splunk] HEC forwarder starting → {SPLUNK_HEC_URL} (interval {SPLUNK_HEC_INTERVAL}s)",
          file=sys.stderr)
    while True:
        try:
            export = build_status_export()
            status = _splunk_post(export)
            print(f"[splunk] pushed export, HEC status={status}", file=sys.stderr)
        except Exception as e:
            print(f"[splunk] push error: {e}", file=sys.stderr)
        time.sleep(SPLUNK_HEC_INTERVAL)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        print(f"[{self.address_string()}] {format % args}", file=sys.stderr)

    def send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, msg, status=500):
        self.send_json({"error": msg}, status)

    def read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length:
            return self.rfile.read(length).decode()
        return ""

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        qs = parse_qs(parsed.query)
        ns = qs.get("namespace", [None])[0]

        try:
            # Static files
            if path == "" or path == "/":
                self.serve_file("index.html", "text/html")
                return

            if path == "/favicon.ico":
                self.serve_file("favicon.ico", "image/x-icon")
                return

            # Prometheus metrics
            if path == "/metrics":
                body = render_prometheus_metrics().encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
                self.send_header("Content-Length", len(body))
                self.end_headers()
                self.wfile.write(body)
                return

            # Full status export
            if path == "/api/status/export":
                self.send_json(build_status_export())
                return

            # Namespaces list
            if path == "/api/namespaces":
                self.send_json(get_namespaces())
                return

            # Summary / dashboard
            if path == "/api/summary":
                summary = {}
                for resource in ["lbservices", "lbvips", "lbbackendpools", "lbdeployments"]:
                    data, err = kubectl_get_json(f"{resource}.isovalent.com")
                    summary[resource] = len(data.get("items", [])) if data else 0
                # Cluster-scoped resources
                for resource, crd in [("lbippools", "ciliumloadbalancerippools.cilium.io")]:
                    data, err = kubectl_get_cluster_json(crd)
                    summary[resource] = len(data.get("items", [])) if data else 0
                # BGP CRDs
                for resource, crd in [
                    ("bgpclusterconfigs", "isovalentbgpclusterconfigs.isovalent.com"),
                    ("bgppeerconfigs", "isovalentbgppeerconfigs.isovalent.com"),
                ]:
                    data, err = kubectl_get_json(crd)
                    summary[resource] = len(data.get("items", [])) if data else 0
                self.send_json(summary)
                return

            # CiliumNodes
            if path == "/api/ciliumnodes":
                data, err = kubectl_get_cluster_json("ciliumnodes")
                if err:
                    self.send_error_json(err)
                else:
                    self.send_json(data.get("items", []))
                return

            # Secrets list
            if path == "/api/secrets":
                if not ns:
                    self.send_error_json("namespace required", 400)
                    return
                data, err = kubectl_get_json("secrets", namespace=ns)
                if err:
                    self.send_error_json(err)
                    return
                # Strip secret data values — only return metadata + type
                items = []
                for item in data.get("items", []):
                    items.append({
                        "metadata": item.get("metadata", {}),
                        "type": item.get("type", ""),
                    })
                self.send_json(items)
                return

            # Available CRDs (for validation/warning)
            if path == "/api/crds":
                data, err = kubectl_get_cluster_json("crds")
                if err:
                    self.send_error_json(err)
                    return
                names = [item["metadata"]["name"] for item in data.get("items", [])]
                self.send_json(names)
                return

            # ── Cilium CLI endpoints ──────────────────────────────────────

            if path == "/api/cilium/lb/status":
                stdout, stderr, rc = run_cilium(["lb", "status", "-o", "json"])
                if rc != 0:
                    self.send_error_json(stderr)
                    return
                try:
                    self.send_json(json.loads(stdout))
                except json.JSONDecodeError:
                    # Return raw lines if not valid JSON
                    self.send_json({"raw": stdout})
                return

            if path.startswith("/api/cilium/lb/service/"):
                svc_name = path[len("/api/cilium/lb/service/"):]
                if not svc_name:
                    self.send_error_json("service name required", 400)
                    return
                svc_ns = qs.get("namespace", [None])[0]
                cmd = ["lb", "service", svc_name, "-o", "json"]
                if svc_ns:
                    cmd += ["-m", svc_ns]
                stdout, stderr, rc = run_cilium(cmd)
                if rc != 0:
                    self.send_error_json(stderr)
                    return
                try:
                    self.send_json(json.loads(stdout))
                except json.JSONDecodeError:
                    self.send_json({"raw": stdout})
                return

            if path == "/api/cilium/bgp/peers":
                stdout, stderr, rc = run_cilium(["bgp", "peers", "-o", "json"])
                if rc != 0:
                    self.send_error_json(stderr)
                    return
                try:
                    self.send_json(json.loads(stdout))
                except json.JSONDecodeError:
                    self.send_json({"raw": stdout})
                return

            if path == "/api/cilium/bgp/routes":
                stdout, stderr, rc = run_cilium(["bgp", "routes", "-o", "json"])
                if rc != 0:
                    self.send_error_json(stderr)
                    return
                try:
                    self.send_json(json.loads(stdout))
                except json.JSONDecodeError:
                    self.send_json({"raw": stdout})
                return

            if path == "/api/cilium/lb/accesslog":
                args = ["lb", "accesslog"]
                vip = qs.get("vip", [None])[0]
                if vip:
                    args += ["--vip-and-port", vip]
                stdout, stderr, rc = run_cilium(args)
                if rc != 0:
                    self.send_error_json(stderr)
                    return
                lines = [l for l in stdout.splitlines() if l.strip()]
                # Cap at last 500 lines to avoid oversized responses
                self.send_json({"lines": lines[-500:]})
                return

            # ── CRD resource list/get endpoints ──────────────────────────

            for api_path, crd_name in RESOURCE_MAP.items():
                cluster_scoped = api_path in CLUSTER_SCOPED

                if path == api_path:
                    if cluster_scoped:
                        data, err = kubectl_get_cluster_json(crd_name)
                    else:
                        data, err = kubectl_get_json(crd_name, namespace=ns)
                    if err:
                        self.send_error_json(err)
                    else:
                        self.send_json(data.get("items", []))
                    return

                if path.startswith(api_path + "/"):
                    remainder = path[len(api_path) + 1:]
                    parts = remainder.split("/", 1)
                    if cluster_scoped:
                        resource_name = parts[0]
                        data, err = kubectl_get_cluster_json(crd_name, name=resource_name)
                    else:
                        if len(parts) == 2:
                            resource_ns, resource_name = parts
                        else:
                            resource_name = parts[0]
                            resource_ns = ns
                        data, err = kubectl_get_json(crd_name, namespace=resource_ns, name=resource_name)
                    if err:
                        self.send_error_json(err)
                    else:
                        self.send_json(data)
                    return

            self.send_error_json("Not found", 404)

        except Exception as e:
            traceback.print_exc()
            self.send_error_json(str(e))

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        try:
            # Namespace create
            if path == "/api/namespaces":
                body = self.read_body()
                if not body:
                    self.send_error_json("Empty body", 400)
                    return
                data = json.loads(body)
                name = data.get("name", "").strip()
                if not name:
                    self.send_error_json("name required", 400)
                    return
                stdout, stderr, rc = run_kubectl(["create", "namespace", name])
                if rc != 0:
                    self.send_error_json(stderr)
                else:
                    self.send_json({"message": f"Namespace '{name}' created"})
                return

            # Secret create (basic-auth or tls via apply -f -)
            if path == "/api/secrets":
                body = self.read_body()
                if not body:
                    self.send_error_json("Empty body", 400)
                    return
                data = json.loads(body)
                secret_type = data.get("type")
                ns = data.get("namespace", "default")
                name = data.get("name", "").strip()

                if not name:
                    self.send_error_json("name required", 400)
                    return

                if secret_type == "basic-auth":
                    # Build generic secret with username=password literals
                    users = data.get("users", [])
                    if not users:
                        self.send_error_json("users required for basic-auth secret", 400)
                        return
                    args = ["create", "secret", "generic", name, "-n", ns]
                    for u in users:
                        username = u.get("username", "").strip()
                        password = u.get("password", "")
                        if username:
                            args += [f"--from-literal={username}={password}"]
                    stdout, stderr, rc = run_kubectl(args)
                    if rc != 0:
                        self.send_error_json(stderr)
                    else:
                        self.send_json({"message": f"Secret '{name}' created"})

                elif secret_type == "tls":
                    import base64
                    cert_pem = data.get("cert", "")
                    key_pem = data.get("key", "")
                    if not cert_pem or not key_pem:
                        self.send_error_json("cert and key required for TLS secret", 400)
                        return
                    # Build a Secret manifest and apply via stdin
                    manifest = {
                        "apiVersion": "v1",
                        "kind": "Secret",
                        "metadata": {"name": name, "namespace": ns},
                        "type": "kubernetes.io/tls",
                        "data": {
                            "tls.crt": base64.b64encode(cert_pem.encode()).decode(),
                            "tls.key": base64.b64encode(key_pem.encode()).decode(),
                        },
                    }
                    stdout, stderr, rc = run_kubectl(
                        ["apply", "-f", "-"],
                        stdin_data=json.dumps(manifest),
                    )
                    if rc != 0:
                        self.send_error_json(stderr)
                    else:
                        self.send_json({"message": f"TLS secret '{name}' created"})

                else:
                    self.send_error_json(f"Unknown secret type: {secret_type}", 400)
                return

        except Exception as e:
            traceback.print_exc()
            self.send_error_json(str(e))
            return

        # Fall through to generic apply handler
        self.handle_write("apply")

    def do_PUT(self):
        self.handle_write("apply")

    def do_DELETE(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        try:
            # Secret delete
            if path.startswith("/api/secrets/"):
                parts = path[len("/api/secrets/"):].split("/", 1)
                if len(parts) == 2:
                    resource_ns, resource_name = parts
                else:
                    resource_ns = "default"
                    resource_name = parts[0]
                stdout, stderr, rc = run_kubectl([
                    "delete", "secret", resource_name, "-n", resource_ns, "--ignore-not-found"
                ])
                if rc != 0:
                    self.send_error_json(stderr)
                else:
                    self.send_json({"message": f"Deleted secret {resource_name}"})
                return

            # CRD resource delete
            for api_path, crd_name in RESOURCE_MAP.items():
                cluster_scoped = api_path in CLUSTER_SCOPED
                if path.startswith(api_path + "/"):
                    remainder = path[len(api_path) + 1:]
                    parts = remainder.split("/", 1)
                    if cluster_scoped:
                        resource_name = parts[0]
                        stdout, stderr, rc = run_kubectl([
                            "delete", crd_name, resource_name, "--ignore-not-found"
                        ])
                    else:
                        if len(parts) == 2:
                            resource_ns, resource_name = parts
                        else:
                            resource_ns = "default"
                            resource_name = parts[0]
                        stdout, stderr, rc = run_kubectl([
                            "delete", crd_name, resource_name, "-n", resource_ns, "--ignore-not-found"
                        ])
                    if rc != 0:
                        self.send_error_json(stderr)
                    else:
                        self.send_json({"message": f"Deleted {resource_name}"})
                    return

            self.send_error_json("Not found", 404)

        except Exception as e:
            traceback.print_exc()
            self.send_error_json(str(e))

    def handle_write(self, action):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        try:
            body = self.read_body()
            if not body:
                self.send_error_json("Empty body", 400)
                return

            try:
                manifest = json.loads(body)
            except json.JSONDecodeError as e:
                self.send_error_json(f"Invalid JSON: {e}", 400)
                return

            stdout, stderr, rc = run_kubectl(
                ["apply", "-f", "-"],
                stdin_data=json.dumps(manifest)
            )
            if rc != 0:
                self.send_error_json(stderr)
            else:
                self.send_json({"message": stdout.strip()})
        except Exception as e:
            traceback.print_exc()
            self.send_error_json(str(e))

    def serve_file(self, filename, content_type):
        script_dir = os.path.dirname(os.path.abspath(__file__))
        filepath = os.path.join(script_dir, filename)
        try:
            with open(filepath, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", len(data))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        except FileNotFoundError:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"File not found")


if __name__ == "__main__":
    print(f"Starting Isovalent LB GUI on http://0.0.0.0:{PORT}", file=sys.stderr)
    print(f"Using kubectl:          {KUBECTL}", file=sys.stderr)
    print(f"Using cilium:           {CILIUM}", file=sys.stderr)
    print(f"Metrics interval:       {METRICS_INTERVAL}s  →  /metrics", file=sys.stderr)

    # Initial metrics collection (best-effort, don't block startup)
    threading.Thread(target=collect_metrics, daemon=True).start()

    # Background metrics refresh loop
    t = threading.Thread(target=_collector_loop, daemon=True)
    t.start()

    # Splunk HEC forwarder (only if configured)
    if SPLUNK_HEC_URL and SPLUNK_HEC_TOKEN:
        print(f"Splunk HEC forwarder:   {SPLUNK_HEC_URL} (interval {SPLUNK_HEC_INTERVAL}s)", file=sys.stderr)
        ts = threading.Thread(target=_splunk_loop, daemon=True)
        ts.start()
    else:
        print("Splunk HEC forwarder:   disabled (set SPLUNK_HEC_URL + SPLUNK_HEC_TOKEN to enable)", file=sys.stderr)

    server = HTTPServer(("0.0.0.0", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.", file=sys.stderr)
