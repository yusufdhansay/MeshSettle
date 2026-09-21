"""The settlement decision pipeline.

This module contains the exactly-once logic and nothing else: no broker, no
HTTP. That separation is deliberate, because it means the correctness tests
drive the real code path rather than a simplified stand-in.

The order of operations is the correctness property. From ARCHITECTURE.md:

1. Parse and validate the packet          -> MALFORMED_PAYLOAD
2. Verify the Ed25519 signature           -> INVALID_SIGNATURE
3. Atomic Redis claim (``SET NX``)        -> DUPLICATE_PACKET
4. Decrypt the envelope                   -> DECRYPTION_FAILED
5. Cross-check the inner instruction      -> MALFORMED_PAYLOAD
6. Write the settlement in a transaction

Swapping any two of these breaks something:

* verifying after decrypting would run crypto on unauthenticated input
* claiming before verifying would let an attacker burn idempotency keys for
  packets they cannot sign, poisoning future legitimate settlements
* claiming after the database write would open the exact double-settlement
  window the whole design exists to close

One thing the code does that the numbered list does not show: a duplicate is
rejected at step 3 and returns immediately, so it never opens a Postgres
transaction. Rejections for cryptographic or structural failures *are*
persisted, because they are rare and security-relevant; duplicates are only
logged and counted, because persisting them would defeat the point of
rejecting them before the database.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from services.settlement.db import Settlement, record_rejection
from services.settlement.dedupe import ClaimOutcome, DedupeStore
from shared.crypto import DecryptionError, SignatureVerificationError
from shared.logging import get_logger, safe_fingerprint
from shared.models import ErrorCode, PacketStatus, PaymentInstruction, PaymentPacket

logger = get_logger("settlement.processor")


@dataclass(frozen=True, slots=True)
class SettlementOutcome:
    """What happened to one packet."""

    status: PacketStatus
    detail: str
    code: ErrorCode | None = None
    idempotency_key: str | None = None
    packet_id: UUID | None = None
    settlement_id: int | None = None
    #: Whether the broker should redeliver this message. True only for
    #: transient infrastructure failures, never for a packet that is simply
    #: invalid or duplicate; redelivering those would loop forever.
    retryable: bool = False

    @property
    def settled(self) -> bool:
        return self.status is PacketStatus.SETTLED


@dataclass
class SettlementMetrics:
    """In-process counters, exposed for tests, the demo UI, and load reports."""

    settled: int = 0
    duplicates: int = 0
    invalid_signature: int = 0
    malformed: int = 0
    decryption_failed: int = 0
    internal_errors: int = 0
    _by_code: dict[str, int] = field(default_factory=dict)

    def record(self, outcome: SettlementOutcome) -> None:
        if outcome.settled:
            self.settled += 1
            return
        code = outcome.code
        if code is None:
            return
        self._by_code[code.value] = self._by_code.get(code.value, 0) + 1
        match code:
            case ErrorCode.DUPLICATE_PACKET:
                self.duplicates += 1
            case ErrorCode.INVALID_SIGNATURE:
                self.invalid_signature += 1
            case ErrorCode.MALFORMED_PAYLOAD:
                self.malformed += 1
            case ErrorCode.DECRYPTION_FAILED:
                self.decryption_failed += 1
            case ErrorCode.INTERNAL_ERROR:
                self.internal_errors += 1

    @property
    def rejected(self) -> int:
        return sum(self._by_code.values())

    def snapshot(self) -> dict[str, int]:
        return {
            "settled": self.settled,
            "rejected": self.rejected,
            "duplicates": self.duplicates,
            "invalid_signature": self.invalid_signature,
            "malformed": self.malformed,
            "decryption_failed": self.decryption_failed,
            "internal_errors": self.internal_errors,
        }


class SettlementProcessor:
    """Settles packets exactly once."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        dedupe: DedupeStore,
        sender_public_key: Ed25519PublicKey,
        settlement_private_key: rsa.RSAPrivateKey,
        metrics: SettlementMetrics | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._dedupe = dedupe
        self._sender_public_key = sender_public_key
        self._settlement_private_key = settlement_private_key
        self.metrics = metrics or SettlementMetrics()

    async def process(self, raw_body: bytes) -> SettlementOutcome:
        """Run one packet through the pipeline."""
        outcome = await self._process(raw_body)
        self.metrics.record(outcome)
        return outcome

    async def _process(self, raw_body: bytes) -> SettlementOutcome:
        # --- 1. Parse and validate -------------------------------------------
        try:
            packet = PaymentPacket.model_validate_json(raw_body)
        except ValidationError as exc:
            detail = _summarize_validation_error(exc)
            logger.warning(
                "settlement.packet_rejected",
                reason=ErrorCode.MALFORMED_PAYLOAD.value,
                detail=detail,
            )
            await self._persist_rejection(ErrorCode.MALFORMED_PAYLOAD, detail)
            return SettlementOutcome(
                status=PacketStatus.REJECTED,
                code=ErrorCode.MALFORMED_PAYLOAD,
                detail=detail,
            )

        idempotency_key = packet.idempotency_key

        # --- 2. Verify the signature -----------------------------------------
        # Nothing below this line runs on unauthenticated data.
        try:
            packet.verify(self._sender_public_key)
        except SignatureVerificationError:
            detail = "packet signature did not verify"
            logger.warning(
                "settlement.packet_rejected",
                reason=ErrorCode.INVALID_SIGNATURE.value,
                packet_id=str(packet.packet_id),
                idempotency_key=idempotency_key,
                signature_fp=safe_fingerprint(packet.signature),
            )
            await self._persist_rejection(
                ErrorCode.INVALID_SIGNATURE,
                detail,
                idempotency_key=idempotency_key,
                packet_id=packet.packet_id,
            )
            return SettlementOutcome(
                status=PacketStatus.REJECTED,
                code=ErrorCode.INVALID_SIGNATURE,
                detail=detail,
                idempotency_key=idempotency_key,
                packet_id=packet.packet_id,
            )

        # --- 2a. AAD/header consistency --------------------------------------
        # The envelope's AAD must be exactly the one this header implies.
        #
        # Without this check the AAD binding is only incidentally effective. An
        # attacker who lifts a sealed envelope onto a fresh, validly signed
        # header and carries the ORIGINAL AAD along with it still decrypts
        # successfully, because the AAD it presents does match the ciphertext.
        # Such a packet is eventually caught by the inner/outer packet id
        # cross-check at step 5, but only as a side effect. Comparing the AAD
        # to the header here makes the binding do the job it exists for, and
        # rejects the replay before it consumes an idempotency key.
        if packet.envelope.aad != packet.expected_aad():
            detail = "envelope AAD does not match the packet header"
            logger.warning(
                "settlement.packet_rejected",
                reason=ErrorCode.MALFORMED_PAYLOAD.value,
                packet_id=str(packet.packet_id),
                idempotency_key=idempotency_key,
                detail=detail,
            )
            await self._persist_rejection(
                ErrorCode.MALFORMED_PAYLOAD,
                detail,
                idempotency_key=idempotency_key,
                packet_id=packet.packet_id,
            )
            return SettlementOutcome(
                status=PacketStatus.REJECTED,
                code=ErrorCode.MALFORMED_PAYLOAD,
                detail=detail,
                idempotency_key=idempotency_key,
                packet_id=packet.packet_id,
            )

        # --- 3. Atomic dedupe claim ------------------------------------------
        # A duplicate returns here, before any database work happens.
        claim = await self._dedupe.claim(idempotency_key)
        if claim is not ClaimOutcome.CLAIMED:
            detail = (
                "packet was already settled"
                if claim is ClaimOutcome.ALREADY_SETTLED
                else "packet is already being settled by another consumer"
            )
            logger.info(
                "settlement.packet_rejected",
                reason=ErrorCode.DUPLICATE_PACKET.value,
                packet_id=str(packet.packet_id),
                idempotency_key=idempotency_key,
                claim_state=claim.value,
            )
            return SettlementOutcome(
                status=PacketStatus.REJECTED,
                code=ErrorCode.DUPLICATE_PACKET,
                detail=detail,
                idempotency_key=idempotency_key,
                packet_id=packet.packet_id,
            )

        # From here on the claim is held, so every exit path must either
        # settle, or release the claim, or deliberately leave it to expire.
        try:
            # --- 4. Decrypt ---------------------------------------------------
            try:
                instruction = packet.open_instruction(self._settlement_private_key)
            except DecryptionError:
                detail = "packet payload could not be decrypted"
                await self._reject_after_claim(
                    ErrorCode.DECRYPTION_FAILED, detail, packet, idempotency_key
                )
                return SettlementOutcome(
                    status=PacketStatus.REJECTED,
                    code=ErrorCode.DECRYPTION_FAILED,
                    detail=detail,
                    idempotency_key=idempotency_key,
                    packet_id=packet.packet_id,
                )
            except ValueError as exc:
                # 5. Cross-check failures surface as ValueError from
                #    open_instruction: inner/outer packet id mismatch, or a
                #    plaintext that is not a valid instruction.
                detail = str(exc)[:256]
                await self._reject_after_claim(
                    ErrorCode.MALFORMED_PAYLOAD, detail, packet, idempotency_key
                )
                return SettlementOutcome(
                    status=PacketStatus.REJECTED,
                    code=ErrorCode.MALFORMED_PAYLOAD,
                    detail=detail,
                    idempotency_key=idempotency_key,
                    packet_id=packet.packet_id,
                )

            # --- 6. Write the settlement in one transaction -------------------
            return await self._write_settlement(packet, instruction, idempotency_key)

        except Exception:
            # Unexpected failure while holding the claim. Release it so a
            # redelivery can retry rather than being rejected as a duplicate.
            await self._dedupe.release_claim(idempotency_key)
            logger.exception(
                "settlement.internal_error",
                packet_id=str(packet.packet_id),
                idempotency_key=idempotency_key,
            )
            return SettlementOutcome(
                status=PacketStatus.REJECTED,
                code=ErrorCode.INTERNAL_ERROR,
                detail="settlement failed unexpectedly",
                idempotency_key=idempotency_key,
                packet_id=packet.packet_id,
                retryable=True,
            )

    async def _write_settlement(
        self,
        packet: PaymentPacket,
        instruction: PaymentInstruction,
        idempotency_key: str,
    ) -> SettlementOutcome:
        """Insert the settlement row, then publish the settled marker."""
        settlement = Settlement(
            idempotency_key=idempotency_key,
            packet_id=packet.packet_id,
            sender_id=packet.sender_id,
            payer_id=instruction.payer_id,
            payee_id=instruction.payee_id,
            amount_minor=instruction.amount_minor,
            currency=instruction.currency,
            packet_created_at=instruction.created_at,
            hop_count=packet.hop_count,
        )

        try:
            async with self._session_factory() as session:
                async with session.begin():
                    session.add(settlement)
                # Committed here. Only now is the payment real.
        except IntegrityError:
            # The durable backstop fired: another consumer committed this same
            # packet despite the Redis claim (possible if Redis lost the key or
            # a claim expired mid-flight). Exactly-once still holds, because the
            # unique constraint refused the second row.
            logger.info(
                "settlement.packet_rejected",
                reason=ErrorCode.DUPLICATE_PACKET.value,
                packet_id=str(packet.packet_id),
                idempotency_key=idempotency_key,
                caught_by="postgres_unique_constraint",
            )
            await self._dedupe.mark_settled(idempotency_key)
            return SettlementOutcome(
                status=PacketStatus.REJECTED,
                code=ErrorCode.DUPLICATE_PACKET,
                detail="packet was already settled",
                idempotency_key=idempotency_key,
                packet_id=packet.packet_id,
            )

        # Promote the short claim to the durable settled marker, so later
        # duplicates are rejected by Redis without reaching Postgres.
        await self._dedupe.mark_settled(idempotency_key)

        logger.info(
            "settlement.packet_settled",
            packet_id=str(packet.packet_id),
            idempotency_key=idempotency_key,
            settlement_id=settlement.id,
            hop_count=packet.hop_count,
        )
        return SettlementOutcome(
            status=PacketStatus.SETTLED,
            detail="settled",
            idempotency_key=idempotency_key,
            packet_id=packet.packet_id,
            settlement_id=settlement.id,
        )

    async def _reject_after_claim(
        self,
        code: ErrorCode,
        detail: str,
        packet: PaymentPacket,
        idempotency_key: str,
    ) -> None:
        """Log, persist, and release the claim for a packet rejected post-claim."""
        logger.warning(
            "settlement.packet_rejected",
            reason=code.value,
            packet_id=str(packet.packet_id),
            idempotency_key=idempotency_key,
            detail=detail,
        )
        await self._persist_rejection(
            code, detail, idempotency_key=idempotency_key, packet_id=packet.packet_id
        )
        # The packet is permanently bad, so nothing will settle under this key.
        # Releasing keeps Redis clean rather than holding a key for a payment
        # that will never happen.
        await self._dedupe.release_claim(idempotency_key)

    async def _persist_rejection(
        self,
        code: ErrorCode,
        detail: str,
        *,
        idempotency_key: str | None = None,
        packet_id: UUID | None = None,
    ) -> None:
        """Store durable evidence of a rejection.

        Best effort: if the database is unavailable we still have the log line,
        and failing to record a rejection must not turn into a second failure
        that masks the first.
        """
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    await record_rejection(
                        session,
                        reason=code.value,
                        detail=detail,
                        idempotency_key=idempotency_key,
                        packet_id=packet_id,
                    )
        except Exception:  # noqa: BLE001 - never mask the original rejection
            logger.warning("settlement.rejection_not_persisted", reason=code.value)


def _summarize_validation_error(exc: ValidationError) -> str:
    """Describe a schema failure without echoing the payload back into logs."""
    errors = exc.errors()
    if not errors:
        return "packet failed schema validation"
    first = errors[0]
    location = ".".join(str(part) for part in first.get("loc", ()))
    reason = first.get("msg", "invalid value")
    return f"{location or 'packet'}: {reason}"[:256]
