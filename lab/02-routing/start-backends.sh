#! /bin/bash

docker network create --subnet 172.21.0.0/24 ilb-backends

docker run -d --name backend1 --network ilb-backends --ip 172.21.0.11 --restart unless-stopped ilb-backend
docker run -d --name backend2 --network ilb-backends --ip 172.21.0.12 --restart unless-stopped ilb-backend
docker run -d --name backend3 --network ilb-backends --ip 172.21.0.13 --restart unless-stopped ilb-backend
docker run -d --name backend4 --network ilb-backends --ip 172.21.0.14 --restart unless-stopped ilb-backend

# Allow forwarding between Kind cluster bridge and the backends bridge
BACKENDS_BRIDGE=$(docker network inspect ilb-backends --format '{{.Id}}' | cut -c1-12)
BACKENDS_BRIDGE="br-${BACKENDS_BRIDGE}"
KIND_BRIDGE=$(docker network inspect kind-cilium --format '{{.Id}}' | cut -c1-12)
KIND_BRIDGE="br-${KIND_BRIDGE}"

sudo iptables -I FORWARD 1 -i "$KIND_BRIDGE"     -o "$BACKENDS_BRIDGE" -j ACCEPT
sudo iptables -I FORWARD 1 -i "$BACKENDS_BRIDGE" -o "$KIND_BRIDGE"     -j ACCEPT

# Masquerade traffic entering the backends network so replies are routed back via the host
# (backend containers only know their own bridge gateway, not the Kind subnet)
sudo iptables -t nat -A POSTROUTING -o "$BACKENDS_BRIDGE" -d 172.21.0.0/24 -j MASQUERADE

# Docker adds raw PREROUTING DROP rules for each container IP (anti-spoofing).
# Allow traffic arriving from the Kind bridge to bypass those drops.
sudo iptables -t raw -I PREROUTING 1 -i "$KIND_BRIDGE" -d 172.21.0.0/24 -j ACCEPT
