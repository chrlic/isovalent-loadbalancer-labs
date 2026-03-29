#!/usr/bin/env python3
"""
Mock API server for offline simulation of the Isovalent LB GUI.
Serves the same endpoints as server.py but returns static fixture data.
Run with: python3 mock_server.py
"""

import json
import os
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

PORT = int(os.environ.get("PORT", 8080))

# ── Fixture data ───────────────────────────────────────────────────────────────

NAMESPACES = ["default", "prod", "staging", "infra", "kube-system"]

SUMMARY = {
    "lbservices": 9,
    "lbvips": 7,
    "lbbackendpools": 9,
    "lbdeployments": 3,
    "lbippools": 1,
    "bgpclusterconfigs": 1,
    "bgppeerconfigs": 1,
}

LBIPPOOLS = [
    {
        "apiVersion": "cilium.io/v2alpha1",
        "kind": "CiliumLoadBalancerIPPool",
        "metadata": {
            "name": "ilb-pool",
            "creationTimestamp": "2024-01-15T10:00:00Z",
        },
        "spec": {
            "blocks": [
                {"cidr": "10.100.0.0/24"},
                {"cidr": "10.101.0.0/24"},
            ]
        },
        "status": {
            "conditions": [
                {"type": "Ready", "status": "True", "reason": "Available"}
            ]
        },
    }
]

LBVIPS = [
    # Scenario A: web-vip shared by 2 services (web-service HTTP port 80, admin-service HTTPS port 443)
    {
        "apiVersion": "isovalent.com/v1",
        "kind": "IsovalentLBVIP",
        "metadata": {"name": "web-vip", "namespace": "prod", "creationTimestamp": "2024-01-15T10:05:00Z"},
        "spec": {"addressFamily": "ipv4", "ipv4Request": "10.100.0.10"},
        "status": {"ipv4": "10.100.0.10", "conditions": [{"type": "Allocated", "status": "True"}]},
    },
    # Scenario B: api-vip used by api-service with 3 routes to 3 different backend pools
    {
        "apiVersion": "isovalent.com/v1",
        "kind": "IsovalentLBVIP",
        "metadata": {"name": "api-vip", "namespace": "prod", "creationTimestamp": "2024-01-15T10:06:00Z"},
        "spec": {"addressFamily": "ipv4", "ipv4Request": "10.100.0.11"},
        "status": {"ipv4": "10.100.0.11", "conditions": [{"type": "Allocated", "status": "True"}]},
    },
    # Scenario C: grpc-vip used by grpc-service which shares api-v2-backends with api-service (cross-VIP pool sharing)
    {
        "apiVersion": "isovalent.com/v1",
        "kind": "IsovalentLBVIP",
        "metadata": {"name": "grpc-vip", "namespace": "prod", "creationTimestamp": "2024-01-17T08:00:00Z"},
        "spec": {"addressFamily": "ipv4", "ipv4Request": "10.100.0.30"},
        "status": {"ipv4": "10.100.0.30", "conditions": [{"type": "Allocated", "status": "True"}]},
    },
    # Scenario D: metrics-vip with degraded TCP service
    {
        "apiVersion": "isovalent.com/v1",
        "kind": "IsovalentLBVIP",
        "metadata": {"name": "metrics-vip", "namespace": "staging", "creationTimestamp": "2024-01-16T09:00:00Z"},
        "spec": {"addressFamily": "ipv4", "ipv4Request": "10.100.0.20"},
        "status": {"ipv4": "10.100.0.20", "conditions": [{"type": "Allocated", "status": "True"}]},
    },
    # Scenario E: internal-vip used by two services in staging that share the same backend pool
    {
        "apiVersion": "isovalent.com/v1",
        "kind": "IsovalentLBVIP",
        "metadata": {"name": "internal-vip", "namespace": "staging", "creationTimestamp": "2024-01-18T08:00:00Z"},
        "spec": {"addressFamily": "ipv4", "ipv4Request": "10.100.0.40"},
        "status": {"ipv4": "10.100.0.40", "conditions": [{"type": "Allocated", "status": "True"}]},
    },

    # --- infra namespace ---

    # Scenario F: cache-vip for cache-service (TCP, single route)
    {
        "apiVersion": "isovalent.com/v1",
        "kind": "IsovalentLBVIP",
        "metadata": {"name": "cache-vip", "namespace": "infra", "creationTimestamp": "2024-02-01T08:00:00Z"},
        "spec": {"addressFamily": "ipv4", "ipv4Request": "10.100.1.10"},
        "status": {"ipv4": "10.100.1.10", "conditions": [{"type": "Allocated", "status": "True"}]},
    },
    # Scenario G: proxy-vip for proxy-service (HTTP, 2 routes, one backend degraded)
    {
        "apiVersion": "isovalent.com/v1",
        "kind": "IsovalentLBVIP",
        "metadata": {"name": "proxy-vip", "namespace": "infra", "creationTimestamp": "2024-02-01T08:05:00Z"},
        "spec": {"addressFamily": "ipv4", "ipv4Request": "10.100.1.11"},
        "status": {"ipv4": "10.100.1.11", "conditions": [{"type": "Allocated", "status": "True"}]},
    },
]

LBBACKENDPOOLS = [
    # --- prod namespace ---

    # Scenario A: web-service routes to web-backends (main) and static-backends (shared)
    {
        "apiVersion": "isovalent.com/v1",
        "kind": "IsovalentLBBackendPool",
        "metadata": {"name": "web-backends", "namespace": "prod", "creationTimestamp": "2024-01-15T10:10:00Z"},
        "spec": {"backends": [
            {"ip": "192.168.1.10", "port": 8080, "weight": 1},
            {"ip": "192.168.1.11", "port": 8080, "weight": 1},
            {"ip": "192.168.1.12", "port": 8080, "weight": 1},
        ]},
        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
    },
    # Scenario A: admin-service (shares web-vip) routes to admin-backends
    {
        "apiVersion": "isovalent.com/v1",
        "kind": "IsovalentLBBackendPool",
        "metadata": {"name": "admin-backends", "namespace": "prod", "creationTimestamp": "2024-01-15T10:12:00Z"},
        "spec": {"backends": [
            {"ip": "192.168.1.20", "port": 8443, "weight": 1},
            {"ip": "192.168.1.21", "port": 8443, "weight": 1},
        ]},
        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
    },
    # Scenario B: api-service has 3 routes: /v1, /v2, /static — each to its own pool
    {
        "apiVersion": "isovalent.com/v1",
        "kind": "IsovalentLBBackendPool",
        "metadata": {"name": "api-v1-backends", "namespace": "prod", "creationTimestamp": "2024-01-15T10:13:00Z"},
        "spec": {"backends": [
            {"ip": "192.168.2.10", "port": 9090, "weight": 2},
            {"ip": "192.168.2.11", "port": 9090, "weight": 1},
        ]},
        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
    },
    # Scenario B+C: api-v2-backends shared by api-service (/v2 route) AND grpc-service (different VIP)
    {
        "apiVersion": "isovalent.com/v1",
        "kind": "IsovalentLBBackendPool",
        "metadata": {"name": "api-v2-backends", "namespace": "prod", "creationTimestamp": "2024-01-15T10:14:00Z"},
        "spec": {"backends": [
            {"ip": "192.168.2.20", "port": 9091, "weight": 1},
            {"ip": "192.168.2.21", "port": 9091, "weight": 1},
            {"ip": "192.168.2.22", "port": 9091, "weight": 1},
        ]},
        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
    },
    # Scenario A+B: static-backends shared by web-service (/static route, web-vip) AND api-service (/static route, api-vip)
    {
        "apiVersion": "isovalent.com/v1",
        "kind": "IsovalentLBBackendPool",
        "metadata": {"name": "static-backends", "namespace": "prod", "creationTimestamp": "2024-01-15T10:15:00Z"},
        "spec": {"backends": [
            {"ip": "192.168.4.10", "port": 8081, "weight": 1},
            {"ip": "192.168.4.11", "port": 8081, "weight": 1},
        ]},
        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
    },

    # --- staging namespace ---

    # Scenario D: metrics-service (degraded)
    {
        "apiVersion": "isovalent.com/v1",
        "kind": "IsovalentLBBackendPool",
        "metadata": {"name": "metrics-backends", "namespace": "staging", "creationTimestamp": "2024-01-16T09:05:00Z"},
        "spec": {"backends": [
            {"ip": "192.168.3.10", "port": 9091, "weight": 1},
        ]},
        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
    },
    # Scenario E: shared-backends used by both worker-service and monitor-service (same internal-vip)
    {
        "apiVersion": "isovalent.com/v1",
        "kind": "IsovalentLBBackendPool",
        "metadata": {"name": "shared-backends", "namespace": "staging", "creationTimestamp": "2024-01-18T08:05:00Z"},
        "spec": {"backends": [
            {"ip": "192.168.5.10", "port": 7070, "weight": 1},
            {"ip": "192.168.5.11", "port": 7070, "weight": 1},
        ]},
        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
    },

    # --- infra namespace ---

    # Scenario F: redis-backends for cache-service (all healthy)
    {
        "apiVersion": "isovalent.com/v1",
        "kind": "IsovalentLBBackendPool",
        "metadata": {"name": "redis-backends", "namespace": "infra", "creationTimestamp": "2024-02-01T08:10:00Z"},
        "spec": {"backends": [
            {"ip": "192.168.6.10", "port": 6379, "weight": 1},
            {"ip": "192.168.6.11", "port": 6379, "weight": 1},
            {"ip": "192.168.6.12", "port": 6379, "weight": 1},
        ]},
        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
    },
    # Scenario G: proxy-backends for proxy-service (one backend degraded)
    {
        "apiVersion": "isovalent.com/v1",
        "kind": "IsovalentLBBackendPool",
        "metadata": {"name": "proxy-backends", "namespace": "infra", "creationTimestamp": "2024-02-01T08:11:00Z"},
        "spec": {"backends": [
            {"ip": "192.168.6.20", "port": 3128, "weight": 1},
            {"ip": "192.168.6.21", "port": 3128, "weight": 1},
        ]},
        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
    },
]

LBSERVICES = [
    # ── Scenario A: two services share web-vip (different ports) ──────────────
    # web-service: HTTP :80 on web-vip, 3 routes to 2 different backend pools
    #   /static → static-backends  (static-backends also used by api-service = cross-VIP sharing)
    {
        "apiVersion": "isovalent.com/v1",
        "kind": "IsovalentLBService",
        "metadata": {"name": "web-service", "namespace": "prod", "creationTimestamp": "2024-01-15T10:20:00Z"},
        "spec": {
            "vipRef": "web-vip",
            "applications": {
                "httpProxy": {
                    "port": 80,
                    "routes": [
                        {
                            "backendRef": "web-backends",
                            "matchHostNames": ["www.example.com", "example.com"],
                            "matchPathType": "Prefix",
                            "matchPathValue": "/",
                        },
                        {
                            "backendRef": "web-backends",
                            "matchHostNames": ["www.example.com"],
                            "matchPathType": "Exact",
                            "matchPathValue": "/health",
                        },
                        {
                            # shared with api-service (cross-VIP pool sharing)
                            "backendRef": "static-backends",
                            "matchHostNames": ["www.example.com", "example.com"],
                            "matchPathType": "Prefix",
                            "matchPathValue": "/static",
                        },
                    ],
                }
            },
        },
        "status": {"conditions": [{"type": "Ready", "status": "True", "reason": "ServiceReady"}]},
    },
    # admin-service: HTTPS :443 on web-vip (same VIP, different port/service)
    {
        "apiVersion": "isovalent.com/v1",
        "kind": "IsovalentLBService",
        "metadata": {"name": "admin-service", "namespace": "prod", "creationTimestamp": "2024-01-15T10:22:00Z"},
        "spec": {
            "vipRef": "web-vip",
            "applications": {
                "httpsProxy": {
                    "port": 443,
                    "tlsSecretRef": "web-basic-auth",
                    "routes": [
                        {
                            "backendRef": "admin-backends",
                            "matchHostNames": ["admin.example.com"],
                            "matchPathType": "Prefix",
                            "matchPathValue": "/admin",
                        },
                        {
                            "backendRef": "admin-backends",
                            "matchHostNames": ["admin.example.com"],
                            "matchPathType": "Prefix",
                            "matchPathValue": "/metrics",
                        },
                    ],
                }
            },
        },
        "status": {"conditions": [{"type": "Ready", "status": "True", "reason": "ServiceReady"}]},
    },

    # ── Scenario B: api-service with 3 routes each to a distinct backend pool ─
    # /static route → static-backends shared with web-service above (cross-VIP)
    {
        "apiVersion": "isovalent.com/v1",
        "kind": "IsovalentLBService",
        "metadata": {"name": "api-service", "namespace": "prod", "creationTimestamp": "2024-01-15T10:21:00Z"},
        "spec": {
            "vipRef": "api-vip",
            "applications": {
                "httpsProxy": {
                    "port": 443,
                    "tlsSecretRef": "api-tls",
                    "routes": [
                        {
                            "backendRef": "api-v1-backends",
                            "matchHostNames": ["api.example.com"],
                            "matchPathType": "Prefix",
                            "matchPathValue": "/v1",
                        },
                        {
                            # api-v2-backends also used by grpc-service (cross-VIP sharing)
                            "backendRef": "api-v2-backends",
                            "matchHostNames": ["api.example.com"],
                            "matchPathType": "Prefix",
                            "matchPathValue": "/v2",
                        },
                        {
                            # static-backends also used by web-service (cross-VIP sharing)
                            "backendRef": "static-backends",
                            "matchHostNames": ["api.example.com"],
                            "matchPathType": "Prefix",
                            "matchPathValue": "/static",
                        },
                    ],
                }
            },
        },
        "status": {"conditions": [{"type": "Ready", "status": "True", "reason": "ServiceReady"}]},
    },

    # ── Scenario C: grpc-service uses grpc-vip, shares api-v2-backends with api-service ─
    {
        "apiVersion": "isovalent.com/v1",
        "kind": "IsovalentLBService",
        "metadata": {"name": "grpc-service", "namespace": "prod", "creationTimestamp": "2024-01-17T08:10:00Z"},
        "spec": {
            "vipRef": "grpc-vip",
            "applications": {
                "tlsPassthrough": {
                    "port": 443,
                    "routes": [
                        {
                            # shared with api-service which is on api-vip (cross-VIP pool sharing)
                            "backendRef": "api-v2-backends",
                            "matchHostNames": ["grpc.example.com"],
                            "matchPathType": "Prefix",
                            "matchPathValue": "/",
                        },
                    ],
                }
            },
        },
        "status": {"conditions": [{"type": "Ready", "status": "True", "reason": "ServiceReady"}]},
    },

    # ── Scenario D: metrics-service degraded ──────────────────────────────────
    {
        "apiVersion": "isovalent.com/v1",
        "kind": "IsovalentLBService",
        "metadata": {"name": "metrics-service", "namespace": "staging", "creationTimestamp": "2024-01-16T09:10:00Z"},
        "spec": {
            "vipRef": "metrics-vip",
            "applications": {
                "tcpProxy": {
                    "port": 9091,
                    "backendRef": "metrics-backends",
                }
            },
        },
        "status": {"conditions": [{"type": "Ready", "status": "False", "reason": "BackendUnavailable"}]},
    },

    # ── Scenario E: two services share internal-vip AND shared-backends ───────
    {
        "apiVersion": "isovalent.com/v1",
        "kind": "IsovalentLBService",
        "metadata": {"name": "worker-service", "namespace": "staging", "creationTimestamp": "2024-01-18T08:10:00Z"},
        "spec": {
            "vipRef": "internal-vip",
            "applications": {
                "httpProxy": {
                    "port": 80,
                    "routes": [
                        {
                            "backendRef": "shared-backends",
                            "matchHostNames": ["internal.example.com"],
                            "matchPathType": "Prefix",
                            "matchPathValue": "/worker",
                        },
                    ],
                }
            },
        },
        "status": {"conditions": [{"type": "Ready", "status": "True", "reason": "ServiceReady"}]},
    },
    {
        "apiVersion": "isovalent.com/v1",
        "kind": "IsovalentLBService",
        "metadata": {"name": "monitor-service", "namespace": "staging", "creationTimestamp": "2024-01-18T08:20:00Z"},
        "spec": {
            "vipRef": "internal-vip",
            "applications": {
                "httpProxy": {
                    "port": 8080,
                    "routes": [
                        {
                            "backendRef": "shared-backends",
                            "matchHostNames": ["internal.example.com"],
                            "matchPathType": "Prefix",
                            "matchPathValue": "/monitor",
                        },
                    ],
                }
            },
        },
        "status": {"conditions": [{"type": "Ready", "status": "True", "reason": "ServiceReady"}]},
    },

    # ── Scenario F: cache-service on cache-vip (infra), TCP :6379 ────────────
    {
        "apiVersion": "isovalent.com/v1",
        "kind": "IsovalentLBService",
        "metadata": {"name": "cache-service", "namespace": "infra", "creationTimestamp": "2024-02-01T08:20:00Z"},
        "spec": {
            "vipRef": "cache-vip",
            "applications": {
                "tcpProxy": {
                    "port": 6379,
                    "backendRef": "redis-backends",
                }
            },
        },
        "status": {"conditions": [{"type": "Ready", "status": "True", "reason": "ServiceReady"}]},
    },

    # ── Scenario G: proxy-service on proxy-vip (infra), HTTP with 2 routes, one backend degraded ─
    {
        "apiVersion": "isovalent.com/v1",
        "kind": "IsovalentLBService",
        "metadata": {"name": "proxy-service", "namespace": "infra", "creationTimestamp": "2024-02-01T08:21:00Z"},
        "spec": {
            "vipRef": "proxy-vip",
            "applications": {
                "httpProxy": {
                    "port": 80,
                    "routes": [
                        {
                            "backendRef": "proxy-backends",
                            "matchHostNames": ["proxy.infra.local"],
                            "matchPathType": "Prefix",
                            "matchPathValue": "/",
                        },
                        {
                            "backendRef": "proxy-backends",
                            "matchHostNames": ["proxy.infra.local"],
                            "matchPathType": "Exact",
                            "matchPathValue": "/healthz",
                        },
                    ],
                }
            },
        },
        "status": {"conditions": [{"type": "Ready", "status": "True", "reason": "ServiceReady"}]},
    },
]

LBDEPLOYMENTS = [
    {
        "apiVersion": "isovalent.com/v1",
        "kind": "IsovalentLBDeployment",
        "metadata": {
            "name": "ilb-prod",
            "namespace": "prod",
            "creationTimestamp": "2024-01-15T09:00:00Z",
        },
        "spec": {
            "replicas": 3,
            "nodeSelector": {"matchLabels": {"service.cilium.io/node": "t1"}},
        },
        "status": {
            "readyReplicas": 3,
            "conditions": [{"type": "Available", "status": "True"}],
        },
    },
    {
        "apiVersion": "isovalent.com/v1",
        "kind": "IsovalentLBDeployment",
        "metadata": {
            "name": "ilb-staging",
            "namespace": "staging",
            "creationTimestamp": "2024-01-16T08:00:00Z",
        },
        "spec": {
            "replicas": 1,
            "nodeSelector": {"matchLabels": {"service.cilium.io/node": "t1"}},
        },
        "status": {
            "readyReplicas": 1,
            "conditions": [{"type": "Available", "status": "True"}],
        },
    },
    {
        "apiVersion": "isovalent.com/v1",
        "kind": "IsovalentLBDeployment",
        "metadata": {
            "name": "ilb-infra",
            "namespace": "infra",
            "creationTimestamp": "2024-02-01T07:00:00Z",
        },
        "spec": {
            "replicas": 2,
            "nodeSelector": {"matchLabels": {"service.cilium.io/node": "t1"}},
        },
        "status": {
            "readyReplicas": 2,
            "conditions": [{"type": "Available", "status": "True"}],
        },
    },
]

BGPCLUSTERCONFIGS = [
    {
        "apiVersion": "isovalent.com/v1",
        "kind": "IsovalentBGPClusterConfig",
        "metadata": {
            "name": "router-bgp",
            "creationTimestamp": "2024-01-15T09:30:00Z",
        },
        "spec": {
            "bgpInstances": [
                {
                    "name": "instance0",
                    "localASN": 64512,
                    "peers": [
                        {
                            "name": "peer0",
                            "peerAddress": "10.0.0.1",
                            "peerASN": 65001,
                            "peerConfigRef": {"name": "ilb-peer-config"},
                        },
                        {
                            "name": "peer1",
                            "peerAddress": "10.0.0.2",
                            "peerASN": 65001,
                            "peerConfigRef": {"name": "ilb-peer-config"},
                        },
                    ],
                }
            ],
            "nodeSelector": {
                "matchExpressions": [
                    {
                        "key": "service.cilium.io/node",
                        "operator": "In",
                        "values": ["t1", "t1-t2"],
                    }
                ]
            },
        },
    }
]

BGPPEERCONFIGS = [
    {
        "apiVersion": "isovalent.com/v1",
        "kind": "IsovalentBGPPeerConfig",
        "metadata": {
            "name": "ilb-peer-config",
            "creationTimestamp": "2024-01-15T09:31:00Z",
        },
        "spec": {
            "bfdProfileRef": "ilb-profile",
            "ebgpMultihop": 3,
            "timers": {"connectRetryTimeSeconds": 1},
            "families": [
                {
                    "afi": "ipv4",
                    "safi": "unicast",
                    "advertisements": {"matchLabels": {"advertise": "ilb"}},
                }
            ],
        },
    }
]

BGPADVERTISEMENTS = [
    {
        "apiVersion": "isovalent.com/v1",
        "kind": "IsovalentBGPAdvertisement",
        "metadata": {
            "name": "ilb-advertisement",
            "labels": {"advertise": "ilb"},
            "creationTimestamp": "2024-01-15T09:32:00Z",
        },
        "spec": {
            "advertisements": [
                {
                    "advertisementType": "Service",
                    "service": {"addresses": ["LoadBalancerIP"]},
                    "selector": {
                        "matchExpressions": [
                            {
                                "key": "loadbalancer.isovalent.com/vip-name",
                                "operator": "Exists",
                            }
                        ]
                    },
                }
            ]
        },
    }
]

BFDPROFILES = [
    {
        "apiVersion": "isovalent.com/v1alpha1",
        "kind": "IsovalentBFDProfile",
        "metadata": {
            "name": "ilb-profile",
            "creationTimestamp": "2024-01-15T09:33:00Z",
        },
        "spec": {
            "detectMultiplier": 3,
            "minimumTTL": 255,
            "receiveIntervalMilliseconds": 300,
            "transmitIntervalMilliseconds": 300,
        },
    }
]

CILIUM_LB_STATUS = {
    "services": [
        # ── Scenario A: two services on web-vip ───────────────────────────────
        # web-service uses web-backends (3) + static-backends (2) = 5 endpoints
        {
            "name": "web-service",
            "namespace": "prod",
            "vip": "10.100.0.10",
            "port": 80,
            "type": "httpProxy",
            "status": "ONLINE",
            "bgpPeerStatus": {"status": "OK", "ok": 2, "total": 2},
            "bgpRouteStatus": {"status": "OK"},
            "t1NodeStatus": {"status": "OK", "ok": 3, "total": 3},
            "t2NodeStatus": {"status": "OK", "ok": 5, "total": 5},
            "t2BackendHealthcheckStatus": {
                "status": "OK", "ok": 5, "total": 5,
                "endpoints": [
                    # web-backends (3)
                    {"endpoint": "192.168.1.10:8080", "status": "OK"},
                    {"endpoint": "192.168.1.11:8080", "status": "OK"},
                    {"endpoint": "192.168.1.12:8080", "status": "OK"},
                    # static-backends (2)
                    {"endpoint": "192.168.4.10:8081", "status": "OK"},
                    {"endpoint": "192.168.4.11:8081", "status": "OK"},
                ],
            },
            "backendpoolStatus": {"status": "OK", "groups": [
                {"name": "web-backends",    "ok": 3, "total": 3, "status": "OK"},
                {"name": "static-backends", "ok": 2, "total": 2, "status": "OK"},
            ]},
        },
        # admin-service uses admin-backends (2)
        {
            "name": "admin-service",
            "namespace": "prod",
            "vip": "10.100.0.10",
            "port": 443,
            "type": "httpsProxy",
            "status": "ONLINE",
            "bgpPeerStatus": {"status": "OK", "ok": 2, "total": 2},
            "bgpRouteStatus": {"status": "OK"},
            "t1NodeStatus": {"status": "OK", "ok": 3, "total": 3},
            "t2NodeStatus": {"status": "OK", "ok": 5, "total": 5},
            "t2BackendHealthcheckStatus": {
                "status": "OK", "ok": 2, "total": 2,
                "endpoints": [
                    # admin-backends (2)
                    {"endpoint": "192.168.1.20:8443", "status": "OK"},
                    {"endpoint": "192.168.1.21:8443", "status": "OK"},
                ],
            },
            "backendpoolStatus": {"status": "OK", "groups": [
                {"name": "admin-backends", "ok": 2, "total": 2, "status": "OK"},
            ]},
        },

        # ── Scenario B: api-service with 3 routes on api-vip ─────────────────
        # api-service uses api-v1-backends (2, one ERR) + api-v2-backends (3) + static-backends (2) = 7
        {
            "name": "api-service",
            "namespace": "prod",
            "vip": "10.100.0.11",
            "port": 443,
            "type": "httpsProxy",
            "status": "DEGRADED",
            "bgpPeerStatus": {"status": "OK", "ok": 2, "total": 2},
            "bgpRouteStatus": {"status": "OK"},
            "t1NodeStatus": {"status": "OK", "ok": 3, "total": 3},
            "t2NodeStatus": {"status": "OK", "ok": 5, "total": 5},
            "t2BackendHealthcheckStatus": {
                "status": "DEG", "ok": 6, "total": 7,
                "endpoints": [
                    # api-v1-backends (2, one ERR)
                    {"endpoint": "192.168.2.10:9090", "status": "OK"},
                    {"endpoint": "192.168.2.11:9090", "status": "ERR"},
                    # api-v2-backends (3, all OK)
                    {"endpoint": "192.168.2.20:9091", "status": "OK"},
                    {"endpoint": "192.168.2.21:9091", "status": "OK"},
                    {"endpoint": "192.168.2.22:9091", "status": "OK"},
                    # static-backends (2, all OK)
                    {"endpoint": "192.168.4.10:8081", "status": "OK"},
                    {"endpoint": "192.168.4.11:8081", "status": "OK"},
                ],
            },
            "backendpoolStatus": {"status": "DEG", "groups": [
                {"name": "api-v1-backends", "ok": 1, "total": 2, "status": "DEG"},
                {"name": "api-v2-backends", "ok": 3, "total": 3, "status": "OK"},
                {"name": "static-backends", "ok": 2, "total": 2, "status": "OK"},
            ]},
        },

        # ── Scenario C: grpc-service on grpc-vip, shares api-v2-backends ─────
        # tlsPassthrough: no L7 healthcheck, uses backendpoolStatus only
        {
            "name": "grpc-service",
            "namespace": "prod",
            "vip": "10.100.0.30",
            "port": 443,
            "type": "tlsPassthrough",
            "status": "ONLINE",
            "bgpPeerStatus": {"status": "OK", "ok": 2, "total": 2},
            "bgpRouteStatus": {"status": "OK"},
            "t1NodeStatus": {"status": "OK", "ok": 3, "total": 3},
            "t2NodeStatus": {"status": "OK", "ok": 5, "total": 5},
            "t2BackendHealthcheckStatus": None,
            "backendpoolStatus": {"status": "OK", "groups": [
                {"name": "api-v2-backends", "ok": 3, "total": 3, "status": "OK"},
            ]},
        },

        # ── Scenario D: metrics-service degraded ─────────────────────────────
        {
            "name": "metrics-service",
            "namespace": "staging",
            "vip": "10.100.0.20",
            "port": 9091,
            "type": "tcpProxy",
            "status": "DEGRADED",
            "bgpPeerStatus": {"status": "OK", "ok": 2, "total": 2},
            "bgpRouteStatus": {"status": "OK"},
            "t1NodeStatus": {"status": "DEG", "ok": 0, "total": 1},
            "t2NodeStatus": {"status": "OK", "ok": 1, "total": 1},
            "t2BackendHealthcheckStatus": None,
            "backendpoolStatus": {"status": "DEG", "groups": [
                {"name": "metrics-backends", "ok": 0, "total": 1, "status": "DEG"},
            ]},
        },

        # ── Scenario E: two services share internal-vip AND shared-backends ──
        {
            "name": "worker-service",
            "namespace": "staging",
            "vip": "10.100.0.40",
            "port": 80,
            "type": "httpProxy",
            "status": "ONLINE",
            "bgpPeerStatus": {"status": "OK", "ok": 2, "total": 2},
            "bgpRouteStatus": {"status": "OK"},
            "t1NodeStatus": {"status": "OK", "ok": 1, "total": 1},
            "t2NodeStatus": {"status": "OK", "ok": 2, "total": 2},
            "t2BackendHealthcheckStatus": {
                "status": "OK", "ok": 2, "total": 2,
                "endpoints": [
                    {"endpoint": "192.168.5.10:7070", "status": "OK"},
                    {"endpoint": "192.168.5.11:7070", "status": "OK"},
                ],
            },
            "backendpoolStatus": {"status": "OK", "groups": [
                {"name": "shared-backends", "ok": 2, "total": 2, "status": "OK"},
            ]},
        },
        {
            "name": "monitor-service",
            "namespace": "staging",
            "vip": "10.100.0.40",
            "port": 8080,
            "type": "httpProxy",
            "status": "ONLINE",
            "bgpPeerStatus": {"status": "OK", "ok": 2, "total": 2},
            "bgpRouteStatus": {"status": "OK"},
            "t1NodeStatus": {"status": "OK", "ok": 1, "total": 1},
            "t2NodeStatus": {"status": "OK", "ok": 2, "total": 2},
            "t2BackendHealthcheckStatus": {
                "status": "OK", "ok": 2, "total": 2,
                "endpoints": [
                    {"endpoint": "192.168.5.10:7070", "status": "OK"},
                    {"endpoint": "192.168.5.11:7070", "status": "OK"},
                ],
            },
            "backendpoolStatus": {"status": "OK", "groups": [
                {"name": "shared-backends", "ok": 2, "total": 2, "status": "OK"},
            ]},
        },

        # ── Scenario F: cache-service on cache-vip (infra) ────────────────────
        # tcpProxy: no L7 healthcheck, uses backendpoolStatus only
        {
            "name": "cache-service",
            "namespace": "infra",
            "vip": "10.100.1.10",
            "port": 6379,
            "type": "tcpProxy",
            "status": "ONLINE",
            "bgpPeerStatus": {"status": "OK", "ok": 2, "total": 2},
            "bgpRouteStatus": {"status": "OK"},
            "t1NodeStatus": {"status": "OK", "ok": 2, "total": 2},
            "t2NodeStatus": {"status": "OK", "ok": 3, "total": 3},
            "t2BackendHealthcheckStatus": None,
            "backendpoolStatus": {"status": "OK", "groups": [
                {"name": "redis-backends", "ok": 3, "total": 3, "status": "OK"},
            ]},
        },

        # ── Scenario G: proxy-service on proxy-vip (infra), one backend degraded ─
        {
            "name": "proxy-service",
            "namespace": "infra",
            "vip": "10.100.1.11",
            "port": 80,
            "type": "httpProxy",
            "status": "DEGRADED",
            "bgpPeerStatus": {"status": "OK", "ok": 2, "total": 2},
            "bgpRouteStatus": {"status": "OK"},
            "t1NodeStatus": {"status": "OK", "ok": 2, "total": 2},
            "t2NodeStatus": {"status": "OK", "ok": 2, "total": 2},
            "t2BackendHealthcheckStatus": {
                "status": "DEG", "ok": 1, "total": 2,
                "endpoints": [
                    {"endpoint": "192.168.6.20:3128", "status": "OK"},
                    {"endpoint": "192.168.6.21:3128", "status": "ERR"},  # degraded
                ],
            },
            "backendpoolStatus": {"status": "DEG", "groups": [
                {"name": "proxy-backends", "ok": 1, "total": 2, "status": "DEG"},
            ]},
        },
    ]
}

CILIUM_BGP_PEERS = {
    "node-1": [
        {
            "local-asn": 64512,
            "peer-address": "10.0.0.1",
            "peer-asn": 65001,
            "session-state": "established",
            "uptime-nanoseconds": 86400000000000,
            "num-received-routes": 12,
        },
        {
            "local-asn": 64512,
            "peer-address": "10.0.0.2",
            "peer-asn": 65001,
            "session-state": "established",
            "uptime-nanoseconds": 86400000000000,
            "num-received-routes": 12,
        },
    ],
    "node-2": [
        {
            "local-asn": 64512,
            "peer-address": "10.0.0.1",
            "peer-asn": 65001,
            "session-state": "established",
            "uptime-nanoseconds": 72000000000000,
            "num-received-routes": 12,
        },
        {
            "local-asn": 64512,
            "peer-address": "10.0.0.2",
            "peer-asn": 65001,
            "session-state": "active",
            "uptime-nanoseconds": 0,
            "num-received-routes": 0,
        },
    ],
    "node-3": [
        {
            "local-asn": 64512,
            "peer-address": "10.0.0.1",
            "peer-asn": 65001,
            "session-state": "established",
            "uptime-nanoseconds": 43200000000000,
            "num-received-routes": 12,
        },
        {
            "local-asn": 64512,
            "peer-address": "10.0.0.2",
            "peer-asn": 65001,
            "session-state": "established",
            "uptime-nanoseconds": 43200000000000,
            "num-received-routes": 12,
        },
    ],
}

CILIUM_BGP_ROUTES = {
    "node-1": [
        {
            "vrouter": "instance0",
            "prefix": "10.100.0.10/32",
            "nexthop": "10.0.0.1",
            "age": "24h0m0s",
        },
        {
            "vrouter": "instance0",
            "prefix": "10.100.0.11/32",
            "nexthop": "10.0.0.1",
            "age": "24h0m0s",
        },
        {
            "vrouter": "instance0",
            "prefix": "10.100.0.20/32",
            "nexthop": "10.0.0.1",
            "age": "20h0m0s",
        },
    ],
    "node-2": [
        {
            "vrouter": "instance0",
            "prefix": "10.100.0.10/32",
            "nexthop": "10.0.0.1",
            "age": "20h0m0s",
        },
        {
            "vrouter": "instance0",
            "prefix": "10.100.0.11/32",
            "nexthop": "10.0.0.1",
            "age": "20h0m0s",
        },
    ],
}

CILIUM_NODES = [
    {
        "metadata": {
            "name": "node-1",
            "labels": {
                "service.cilium.io/node": "t1",
                "kubernetes.io/hostname": "node-1",
                "kubernetes.io/arch": "amd64",
                "kubernetes.io/os": "linux",
            },
            "creationTimestamp": "2024-01-10T08:00:00Z",
        },
        "spec": {
            "addresses": [
                {"type": "InternalIP", "ip": "10.0.1.10"},
                {"type": "CiliumInternalIP", "ip": "10.244.1.1"},
            ]
        },
        "status": {"conditions": []},
    },
    {
        "metadata": {
            "name": "node-2",
            "labels": {
                "service.cilium.io/node": "t1",
                "kubernetes.io/hostname": "node-2",
                "kubernetes.io/arch": "amd64",
                "kubernetes.io/os": "linux",
            },
            "creationTimestamp": "2024-01-10T08:01:00Z",
        },
        "spec": {
            "addresses": [
                {"type": "InternalIP", "ip": "10.0.1.11"},
                {"type": "CiliumInternalIP", "ip": "10.244.2.1"},
            ]
        },
        "status": {"conditions": []},
    },
    {
        "metadata": {
            "name": "node-3",
            "labels": {
                "service.cilium.io/node": "t1",
                "kubernetes.io/hostname": "node-3",
                "kubernetes.io/arch": "amd64",
                "kubernetes.io/os": "linux",
            },
            "creationTimestamp": "2024-01-10T08:02:00Z",
        },
        "spec": {
            "addresses": [
                {"type": "InternalIP", "ip": "10.0.1.12"},
                {"type": "CiliumInternalIP", "ip": "10.244.3.1"},
            ]
        },
        "status": {"conditions": []},
    },
    {
        "metadata": {
            "name": "node-4",
            "labels": {
                "service.cilium.io/node": "t2",
                "kubernetes.io/hostname": "node-4",
                "kubernetes.io/arch": "amd64",
                "kubernetes.io/os": "linux",
            },
            "creationTimestamp": "2024-01-10T08:03:00Z",
        },
        "spec": {
            "addresses": [
                {"type": "InternalIP", "ip": "10.0.2.10"},
                {"type": "CiliumInternalIP", "ip": "10.244.4.1"},
            ]
        },
        "status": {"conditions": []},
    },
    {
        "metadata": {
            "name": "node-5",
            "labels": {
                "service.cilium.io/node": "t2",
                "kubernetes.io/hostname": "node-5",
                "kubernetes.io/arch": "amd64",
                "kubernetes.io/os": "linux",
            },
            "creationTimestamp": "2024-01-10T08:04:00Z",
        },
        "spec": {
            "addresses": [
                {"type": "InternalIP", "ip": "10.0.2.11"},
                {"type": "CiliumInternalIP", "ip": "10.244.5.1"},
            ]
        },
        "status": {"conditions": []},
    },
]

SECRETS = {
    "prod": [
        {
            "metadata": {
                "name": "api-tls",
                "namespace": "prod",
                "creationTimestamp": "2024-01-15T10:00:00Z",
            },
            "type": "kubernetes.io/tls",
        },
        {
            "metadata": {
                "name": "web-basic-auth",
                "namespace": "prod",
                "creationTimestamp": "2024-01-15T10:01:00Z",
            },
            "type": "Opaque",
        },
    ],
    "staging": [
        {
            "metadata": {
                "name": "metrics-tls",
                "namespace": "staging",
                "creationTimestamp": "2024-01-16T09:00:00Z",
            },
            "type": "kubernetes.io/tls",
        },
    ],
    "infra": [
        {
            "metadata": {
                "name": "proxy-tls",
                "namespace": "infra",
                "creationTimestamp": "2024-02-01T08:00:00Z",
            },
            "type": "kubernetes.io/tls",
        },
    ],
    "default": [],
    "kube-system": [],
}

CRDS = [
    "lbservices.isovalent.com",
    "lbvips.isovalent.com",
    "lbbackendpools.isovalent.com",
    "lbdeployments.isovalent.com",
    "isovalentbgpclusterconfigs.isovalent.com",
    "isovalentbgppeerconfigs.isovalent.com",
    "isovalentbgpadvertisements.isovalent.com",
    "isovalentbfdprofiles.isovalent.com",
    "ciliumloadbalancerippools.cilium.io",
    "ciliumnodes.cilium.io",
]

ACCESS_LOG_LINES = [
    '2024-01-29T12:00:01Z [web-service] 203.0.113.10:45231 -> 10.100.0.10:80 GET /index.html 200 1ms',
    '2024-01-29T12:00:02Z [web-service] 203.0.113.11:51234 -> 10.100.0.10:80 GET /static/app.js 304 0ms',
    '2024-01-29T12:00:03Z [api-service] 203.0.113.12:38721 -> 10.100.0.11:443 POST /v1/orders 201 12ms',
    '2024-01-29T12:00:04Z [api-service] 203.0.113.13:44312 -> 10.100.0.11:443 GET /v2/users 200 5ms',
    '2024-01-29T12:00:05Z [web-service] 203.0.113.14:56789 -> 10.100.0.10:80 GET /health 200 0ms',
    '2024-01-29T12:00:06Z [api-service] 203.0.113.15:42100 -> 10.100.0.11:443 GET /v1/products 200 8ms',
    '2024-01-29T12:00:07Z [web-service] 203.0.113.16:33445 -> 10.100.0.10:80 GET /favicon.ico 200 0ms',
    '2024-01-29T12:00:08Z [api-service] 203.0.113.17:61234 -> 10.100.0.11:443 DELETE /v2/sessions/abc 200 3ms',
    '2024-01-29T12:00:09Z [metrics-service] 10.0.0.5:9001 -> 10.100.0.20:9091 CONNECT (tcp) ERR connection refused',
    '2024-01-29T12:00:10Z [web-service] 203.0.113.18:55123 -> 10.100.0.10:80 GET /api/data 200 22ms',
]

RESOURCE_MAP = {
    "lbservices": LBSERVICES,
    "lbvips": LBVIPS,
    "lbbackendpools": LBBACKENDPOOLS,
    "lbdeployments": LBDEPLOYMENTS,
    "bgpclusterconfigs": BGPCLUSTERCONFIGS,
    "bgppeerconfigs": BGPPEERCONFIGS,
    "bgpadvertisements": BGPADVERTISEMENTS,
    "bfdprofiles": BFDPROFILES,
    "lbippools": LBIPPOOLS,
}

# ── Handler ────────────────────────────────────────────────────────────────────

class MockHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        print(f"[mock] {format % args}")

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
        return self.rfile.read(length).decode() if length else ""

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

        # Static file
        if path in ("", "/"):
            self._serve_file("index.html", "text/html")
            return
        if path == "/favicon.ico":
            self.send_response(404)
            self.end_headers()
            return

        if path == "/api/namespaces":
            self.send_json(NAMESPACES)
            return

        if path == "/api/summary":
            self.send_json(SUMMARY)
            return

        if path == "/api/crds":
            self.send_json(CRDS)
            return

        if path == "/api/ciliumnodes":
            self.send_json(CILIUM_NODES)
            return

        if path == "/api/secrets":
            if not ns:
                self.send_error_json("namespace required", 400)
                return
            self.send_json(SECRETS.get(ns, []))
            return

        if path == "/api/cilium/lb/status":
            self.send_json(CILIUM_LB_STATUS)
            return

        if path.startswith("/api/cilium/lb/service/"):
            svc_name = path[len("/api/cilium/lb/service/"):]
            # Find in LB status
            for svc in CILIUM_LB_STATUS["services"]:
                if svc["name"] == svc_name:
                    self.send_json(svc)
                    return
            self.send_error_json(f"service {svc_name} not found", 404)
            return

        if path == "/api/cilium/bgp/peers":
            self.send_json(CILIUM_BGP_PEERS)
            return

        if path == "/api/cilium/bgp/routes":
            self.send_json(CILIUM_BGP_ROUTES)
            return

        if path == "/api/cilium/lb/accesslog":
            vip = qs.get("vip", [None])[0]
            lines = ACCESS_LOG_LINES
            if vip:
                lines = [l for l in lines if vip in l]
            self.send_json({"lines": lines})
            return

        if path == "/api/status/export":
            self.send_json({
                "collected_at": "2024-01-29T12:00:00+00:00",
                "lb_status": CILIUM_LB_STATUS,
                "bgp_peers": CILIUM_BGP_PEERS,
                "inventory": SUMMARY,
            })
            return

        # CRD resource list: /api/<resource>
        for resource, items in RESOURCE_MAP.items():
            if path == f"/api/{resource}":
                filtered = items if resource == "lbippools" else (
                    [i for i in items if i["metadata"].get("namespace") == ns] if ns else items
                )
                self.send_json(filtered)
                return

            # CRD single resource: /api/<resource>/<name> or /api/<resource>/<ns>/<name>
            if path.startswith(f"/api/{resource}/"):
                remainder = path[len(f"/api/{resource}/"):]
                parts = remainder.split("/", 1)
                if resource == "lbippools":
                    name = parts[0]
                    match = next((i for i in items if i["metadata"]["name"] == name), None)
                else:
                    if len(parts) == 2:
                        r_ns, name = parts
                    else:
                        r_ns, name = ns or "default", parts[0]
                    match = next((i for i in items
                                  if i["metadata"]["name"] == name
                                  and i["metadata"].get("namespace", r_ns) == r_ns), None)
                if match:
                    self.send_json(match)
                else:
                    self.send_error_json(f"{resource}/{remainder} not found", 404)
                return

        self.send_error_json("Not found", 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        body = self.read_body()

        if path == "/api/namespaces":
            data = json.loads(body) if body else {}
            name = data.get("name", "").strip()
            if not name:
                self.send_error_json("name required", 400)
                return
            if name not in NAMESPACES:
                NAMESPACES.append(name)
            self.send_json({"message": f"Namespace '{name}' created"})
            return

        if path == "/api/secrets":
            data = json.loads(body) if body else {}
            ns_ = data.get("namespace", "default")
            name = data.get("name", "").strip()
            stype = data.get("type", "Opaque")
            if not name:
                self.send_error_json("name required", 400)
                return
            if ns_ not in SECRETS:
                SECRETS[ns_] = []
            SECRETS[ns_].append({
                "metadata": {"name": name, "namespace": ns_, "creationTimestamp": "2024-01-29T12:00:00Z"},
                "type": "kubernetes.io/tls" if stype == "tls" else "Opaque",
            })
            self.send_json({"message": f"Secret '{name}' created"})
            return

        # Fall through to apply
        self._handle_apply(path, body)

    def do_PUT(self):
        body = self.read_body()
        self._handle_apply(urlparse(self.path).path.rstrip("/"), body)

    def do_DELETE(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path.startswith("/api/secrets/"):
            parts = path[len("/api/secrets/"):].split("/", 1)
            r_ns, name = (parts[0], parts[1]) if len(parts) == 2 else ("default", parts[0])
            bucket = SECRETS.get(r_ns, [])
            before = len(bucket)
            SECRETS[r_ns] = [s for s in bucket if s["metadata"]["name"] != name]
            if len(SECRETS[r_ns]) < before:
                self.send_json({"message": f"Deleted secret {name}"})
            else:
                self.send_json({"message": f"Secret {name} not found (ignored)"})
            return

        for resource, items in RESOURCE_MAP.items():
            if path.startswith(f"/api/{resource}/"):
                remainder = path[len(f"/api/{resource}/"):]
                parts = remainder.split("/", 1)
                if resource == "lbippools":
                    name = parts[0]
                    before = len(items)
                    RESOURCE_MAP[resource] = [i for i in items if i["metadata"]["name"] != name]
                else:
                    if len(parts) == 2:
                        r_ns, name = parts
                    else:
                        r_ns, name = "default", parts[0]
                    before = len(items)
                    RESOURCE_MAP[resource] = [
                        i for i in items
                        if not (i["metadata"]["name"] == name and i["metadata"].get("namespace") == r_ns)
                    ]
                self.send_json({"message": f"Deleted {name}"})
                return

        self.send_error_json("Not found", 404)

    def _handle_apply(self, path, body):
        """Simulate kubectl apply — upsert the resource into in-memory store."""
        if not body:
            self.send_error_json("Empty body", 400)
            return
        try:
            manifest = json.loads(body)
        except json.JSONDecodeError as e:
            self.send_error_json(f"Invalid JSON: {e}", 400)
            return

        name = manifest.get("metadata", {}).get("name", "")
        ns_ = manifest.get("metadata", {}).get("namespace", "")

        for resource, items in RESOURCE_MAP.items():
            if path == f"/api/{resource}":
                # Upsert
                if resource == "lbippools":
                    idx = next((i for i, x in enumerate(items) if x["metadata"]["name"] == name), None)
                else:
                    idx = next((i for i, x in enumerate(items)
                                if x["metadata"]["name"] == name
                                and x["metadata"].get("namespace") == ns_), None)
                if idx is not None:
                    RESOURCE_MAP[resource][idx] = manifest
                    msg = f"{resource}/{name} configured"
                else:
                    RESOURCE_MAP[resource].append(manifest)
                    msg = f"{resource}/{name} created"
                self.send_json({"message": msg})
                return

        self.send_error_json("Not found", 404)

    def _serve_file(self, filename, content_type):
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
    print(f"[mock] Isovalent LB GUI mock server → http://localhost:{PORT}")
    print(f"[mock] Serving index.html from {os.path.dirname(os.path.abspath(__file__))}")
    server = HTTPServer(("0.0.0.0", PORT), MockHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[mock] Shutting down.")
