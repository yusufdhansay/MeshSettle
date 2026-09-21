# MeshSettle — Product Requirements

## Product Overview
MeshSettle is a simulation of an offline-first payment settlement system.
Two devices with no internet connection exchange a signed, encrypted
payment instruction over a local transport (Bluetooth/NFC in the real
world, simulated as a local relay here). The instruction propagates
device-to-device until one device regains connectivity and forwards it
to a backend, which settles it exactly once, even if the same packet
arrives multiple times through different paths.

## Not affiliated with any real payment network
MeshSettle is an original system. It is **not** UPI, not a UPI clone, not
an implementation of UPI or UPI 123PAY, and has **no affiliation with,
endorsement by, or connection to NPCI, UPI, or any real payment network,
bank, or financial institution**. No real payment rails, bank APIs, or
network specifications are used or reimplemented anywhere in this repo.

What MeshSettle takes from the real world is only the *general problem
statement* that UPI's offline/IVR mode and academic offline-payment
research both address: how do you settle a transaction correctly when
neither party has internet at the moment of the transaction? The design,
protocol, packet format, and settlement logic here are all original and
built purely to demonstrate distributed systems techniques. Nothing in
this repo moves real money or is safe to use for real money.

## Problem Statement
Digital payment systems assume both parties have live internet access
at the moment of transaction. That assumption fails in low-connectivity
regions, during network outages, and in emergency scenarios. The
underlying distributed systems problem, settling a transaction
correctly when message delivery is delayed, duplicated, or arrives
out of order, is unsolved in most portfolio-level projects and is a
real problem in distributed systems (also shows up in event-driven
architectures, message queues, and offline-first mobile apps generally).

## Goals
- Demonstrate exactly-once settlement under duplicate/out-of-order delivery
- Demonstrate tamper detection and rejection of forged or altered packets
- Demonstrate the system holding correctness under real concurrent load,
  not just single-threaded demo conditions
- Produce real, measured performance numbers (throughput, latency,
  concurrency correctness) from actual test runs, not estimates

## Target Users
This is a portfolio/reference system, not a production payment app.
"Users" are: (1) the developer demonstrating distributed systems
competence, (2) anyone reviewing the code/architecture in a technical
interview context.

## Core Features (MVP)
1. Client can create a signed, encrypted payment packet (hybrid
   RSA-OAEP + AES-256-GCM)
2. Packet relay simulation: packet hops through 1+ intermediate "mesh"
   nodes before reaching a node with connectivity
3. Bridge node forwards the packet to a backend via a message queue
   (RabbitMQ), not a direct HTTP call, since real mesh relay is
   asynchronous and unreliable
4. Backend consumer validates signature, checks for tamper, and
   deduplicates using an atomic Redis operation before it ever
   touches the database
5. Settlement is written to Postgres inside a transaction; duplicate
   packets are rejected at the Redis layer and never reach Postgres
6. Full test suite: unit tests, a concurrency test that fires N
   duplicate/simultaneous packets and asserts exactly one settles,
   and a tamper test that asserts N/N corrupted packets are rejected
7. Load test script (Locust) producing real throughput/latency numbers
8. Dockerized services, docker-compose for local orchestration,
   Kubernetes manifests (Deployment, Service, HPA) for orchestration
   demonstration (run locally against kind/minikube, not a cloud
   cluster, unless the developer sets one up separately)

## Explicitly out of scope
- Real bank/NPCI integration of any kind
- Real Bluetooth/NFC hardware transport (simulated via local network
  calls standing in for a mesh hop)
- A frontend beyond what's needed to demonstrate the flow (see
  DESIGN.md for scope)
- Multi-currency, KYC, compliance features
