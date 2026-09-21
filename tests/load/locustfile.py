"""Locust load test for the MeshSettle pipeline.

Run against a live stack (see ``scripts/run_load_test.sh``, which also handles
the rate limit and reconciles the results against the database).

Two user classes, because the interesting questions are different:

``PaymentSender``
    Drives the full pipeline: create a signed, encrypted packet, hand it to
    the mesh, let it hop, bridge, queue, and settle. Each task is one distinct
    payment, so this measures end-to-end submission throughput including
    RSA-OAEP wrapping, Ed25519 signing, AES-256-GCM sealing, and two relay
    hops. This is the number that matters for "how many payments can enter
    the system".

``DuplicateFlooder``
    Creates one packet, then submits that same packet over and over. Every
    copy after the first must be rejected as a duplicate and exactly one must
    settle. This measures the cost of the dedupe path and, more importantly,
    puts the exactly-once guarantee under sustained concurrent load rather
    than the controlled conditions of the pytest suite.

What is deliberately NOT measured here: settlement latency. Submission
returns as soon as the bridge has the packet on the queue, because that is
the real contract; settlement happens asynchronously afterwards. The runner
script measures settlement completion separately by draining the queue and
counting rows.
"""

from __future__ import annotations

import json
import random
from typing import Any

from locust import HttpUser, between, events, task

# Ports match docker-compose.yml. Override per-user-class with --host if needed.
SENDER_HOST = "http://127.0.0.1:18001"
RELAY_HOST = "http://127.0.0.1:18002"

PAYERS = [f"device-payer-{index:03d}" for index in range(50)]
PAYEES = [f"device-payee-{index:03d}" for index in range(50)]


def _transaction() -> dict[str, Any]:
    """A distinct, valid payment request."""
    payer = random.choice(PAYERS)  # noqa: S311 - load shaping, not cryptographic
    payee = random.choice([p for p in PAYEES if p != payer])  # noqa: S311
    return {
        "payer_id": payer,
        "payee_id": payee,
        # Integer minor units, always positive.
        "amount_minor": random.randint(100, 500_000),  # noqa: S311
        "currency": "INR",
    }


class PaymentSender(HttpUser):
    """Submits distinct payments through the whole pipeline."""

    host = SENDER_HOST
    # Think time between payments. Kept small but non-zero: a real payer is not
    # a tight loop, and zero wait makes the numbers a function of client CPU.
    wait_time = between(0.05, 0.2)
    weight = 4

    @task
    def submit_payment(self) -> None:
        """Create, sign, seal, and hand a payment to the mesh."""
        with self.client.post(
            "/packets/submit",
            json=_transaction(),
            name="POST /packets/submit (full pipeline)",
            catch_response=True,
        ) as response:
            if response.status_code == 202:
                response.success()
            elif response.status_code == 429:
                # Rate limited. Not a system failure, but it must not be
                # silently counted as success or the throughput number lies.
                response.failure("rate limited (429)")
            else:
                response.failure(f"unexpected status {response.status_code}")


class DuplicateFlooder(HttpUser):
    """Submits the same packet repeatedly to exercise deduplication.

    The relay accepts every copy (202) because deduplication is settlement's
    job, not the mesh's. The guarantee being stressed is that only one of them
    produces a settlement row, which the runner script verifies afterwards.
    """

    host = RELAY_HOST
    wait_time = between(0.05, 0.2)
    weight = 1

    def on_start(self) -> None:
        """Mint one packet this user will replay for the rest of the run."""
        self.packet: str | None = None
        self.idempotency_key: str | None = None

        with self.client.post(
            f"{SENDER_HOST}/packets",
            json=_transaction(),
            name="POST /packets (mint one to replay)",
            catch_response=True,
        ) as response:
            if response.status_code != 201:
                response.failure(f"could not mint a packet: {response.status_code}")
                return
            response.success()
            body = response.json()
            self.packet = json.dumps(body["packet"])
            self.idempotency_key = body["idempotency_key"]

    @task
    def replay_packet(self) -> None:
        """Re-submit the same packet. Only the first may ever settle."""
        if self.packet is None:
            return

        with self.client.post(
            "/relay",
            data=self.packet,
            headers={"Content-Type": "application/json"},
            name="POST /relay (duplicate replay)",
            catch_response=True,
        ) as response:
            if response.status_code == 202:
                response.success()
            elif response.status_code == 429:
                response.failure("rate limited (429)")
            else:
                response.failure(f"unexpected status {response.status_code}")


@events.test_start.add_listener
def _announce(environment: Any, **_kwargs: Any) -> None:
    print("MeshSettle load test starting")
    print(f"  sender : {SENDER_HOST}")
    print(f"  relay  : {RELAY_HOST}")


@events.test_stop.add_listener
def _summarize(environment: Any, **_kwargs: Any) -> None:
    stats = environment.stats.total
    print("MeshSettle load test finished")
    print(f"  requests    : {stats.num_requests}")
    print(f"  failures    : {stats.num_failures}")
    print(f"  median (ms) : {stats.median_response_time}")
    print(f"  p95 (ms)    : {stats.get_response_time_percentile(0.95)}")
    print(f"  rps         : {stats.total_rps:.2f}")
