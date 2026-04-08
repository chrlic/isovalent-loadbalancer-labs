#!/bin/bash

kubectl create secret generic auth-users \
  --from-literal="admin=Passw0rd." \
  --from-literal="mdivis=TopSecret" \
  -n auth
  