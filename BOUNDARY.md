<!-- SPDX-FileCopyrightText: 2026 Sungmoon Park -->
<!-- SPDX-License-Identifier: Apache-2.0 -->
# Synthetic network boundary

Three distinct MSPs are required: SourceAMSP, SourceBMSP and TargetTMSP.
One peer per MSP and one synthetic orderer. No existing wallet, profile,
channel, database, Redis key, container, volume or production endpoint is used.
The local runner must create an internal network with unique run labels,
no published ports and explicit memory/PID/CPU limits. Only resources whose
IDs and labels match the current run may be removed. Host-wide prune is forbidden.

`bootstrap.py` creates a synthetic TLS network and performs CCaaS installation,
approval, readiness and commit at lifecycle version 0.1.0, sequence 1. Historical
PC1 package/lifecycle identities are not reused. Source version and lifecycle
version are separate from contract versions 1.2 and 1.1.
This boundary document is not execution evidence; use the runner's raw receipts.
