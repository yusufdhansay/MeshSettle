"""Shared Pydantic models for MeshSettle.

These models are the wire contract. Every service imports them from here
rather than redefining a local copy, so a schema change cannot silently
desynchronize the sender from the settlement consumer.

The packet layout matches ARCHITECTURE.md "Packet Format" exactly:

    PaymentPacket = cleartext header + EncryptedEnvelope + signature
    PaymentInstruction = the plaintext that lives inside the envelope

Design notes worth knowing before editing:

* ``bytes`` fields serialize as unpadded base64url strings so the packet is
  JSON-safe for queue transport, and deserialize back to ``bytes``.
* ``PaymentPacket`` is frozen. A packet is an immutable artifact from
  creation through settlement; the only permitted change is appending a hop
  record, which returns a new instance and does not touch the signed region.
* ``created_at`` is always timezone-aware UTC and always serialized through
  one canonical formatter, because the signature covers its string form.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Self
from uuid import UUID, uuid4

from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    field_validator,
)

from shared.crypto import (
    b64u_decode,
    b64u_encode,
    build_aad,
    build_signing_bytes,
    canonical_json,
    decrypt_payload,
    derive_idempotency_key,
    encrypt_payload,
    sign_bytes,
    verify_signature,
)

# --- Field types --------------------------------------------------------------

#: Identifiers are constrained so a malicious sender cannot smuggle control
#: characters or unbounded strings into logs and downstream systems.
IDENTIFIER_PATTERN = r"^[A-Za-z0-9_.:\-]{1,64}$"

#: Upper bound on a single transfer, in minor units. Not a business rule so
#: much as a sanity bound, so an absurd or overflowing amount is rejected at
#: the schema boundary instead of reaching the ledger.
MAX_AMOUNT_MINOR = 10**12


def _coerce_bytes(value: Any) -> bytes:
    """Accept raw bytes or an unpadded base64url string, return bytes."""
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return b64u_decode(value)
    raise ValueError("expected bytes or a base64url string")


#: Bytes on the wire: base64url without padding, in both directions.
B64UBytes = Annotated[
    bytes,
    BeforeValidator(_coerce_bytes),
    PlainSerializer(b64u_encode, return_type=str, when_used="always"),
]


def canonical_timestamp(moment: datetime) -> str:
    """Render a datetime as canonical UTC ISO 8601.

    The signature covers this string, so sender and settlement must produce
    byte-identical output for the same instant. Normalizing to UTC first
    means an offset-shifted but equal instant still signs the same.
    """
    return moment.astimezone(UTC).isoformat()


#: Timestamps serialize through the canonical formatter, never Pydantic's default.
UtcTimestamp = Annotated[
    datetime,
    PlainSerializer(canonical_timestamp, return_type=str, when_used="always"),
]


# --- Enums -------------------------------------------------------------------


class ErrorCode(StrEnum):
    """Specific rejection reasons.

    Distinct codes exist so tests and load-test reports can tell failure
    types apart rather than seeing one generic error (RULES.md).
    """

    MALFORMED_PAYLOAD = "MALFORMED_PAYLOAD"
    INVALID_SIGNATURE = "INVALID_SIGNATURE"
    DUPLICATE_PACKET = "DUPLICATE_PACKET"
    DECRYPTION_FAILED = "DECRYPTION_FAILED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class PacketStatus(StrEnum):
    """Stages of a packet's journey, as shown in the demo UI stepper."""

    CREATED = "created"
    RELAYED = "relayed"
    BRIDGED = "bridged"
    SETTLED = "settled"
    REJECTED = "rejected"


# --- Instruction (plaintext, never transmitted in the clear) -----------------


class PaymentInstruction(BaseModel):
    """The payment itself. Lives only inside the AES-GCM ciphertext."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    packet_id: UUID
    payer_id: str = Field(pattern=IDENTIFIER_PATTERN)
    payee_id: str = Field(pattern=IDENTIFIER_PATTERN)
    amount_minor: int = Field(gt=0, le=MAX_AMOUNT_MINOR)
    currency: str = Field(default="INR", pattern=r"^[A-Z]{3}$")
    created_at: UtcTimestamp

    @field_validator("created_at")
    @classmethod
    def _require_utc(cls, value: datetime) -> datetime:
        return _normalize_to_utc(value)

    @field_validator("payee_id")
    @classmethod
    def _payee_differs_from_payer(cls, value: str, info: Any) -> str:
        if info.data.get("payer_id") == value:
            raise ValueError("payer_id and payee_id must differ")
        return value

    def to_canonical_bytes(self) -> bytes:
        """Deterministic plaintext bytes, for encryption."""
        return canonical_json(
            {
                "packet_id": str(self.packet_id),
                "payer_id": self.payer_id,
                "payee_id": self.payee_id,
                "amount_minor": self.amount_minor,
                "currency": self.currency,
                "created_at": canonical_timestamp(self.created_at),
            }
        )

    @classmethod
    def from_canonical_bytes(cls, raw: bytes) -> Self:
        """Parse and validate decrypted plaintext back into an instruction."""
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("decrypted payload is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("decrypted payload is not a JSON object")
        return cls.model_validate(payload)


# --- Envelope ----------------------------------------------------------------


class EncryptedEnvelope(BaseModel):
    """The sealed payload: a wrapped content key plus AES-256-GCM ciphertext."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    encrypted_key: B64UBytes = Field(description="AES-256 content key, RSA-OAEP wrapped")
    nonce: B64UBytes = Field(description="96-bit AES-GCM nonce, unique per packet")
    ciphertext: B64UBytes = Field(description="AES-256-GCM sealed PaymentInstruction")
    aad: B64UBytes = Field(description="Additional authenticated data binding id and sender")

    @field_validator("nonce")
    @classmethod
    def _nonce_is_96_bits(cls, value: bytes) -> bytes:
        if len(value) != 12:
            raise ValueError("nonce must be exactly 12 bytes (96 bits)")
        return value

    @field_validator("encrypted_key", "ciphertext", "aad")
    @classmethod
    def _not_empty(cls, value: bytes) -> bytes:
        if not value:
            raise ValueError("value must not be empty")
        return value


# --- Hop metadata (untrusted, outside the signed region) ---------------------


class HopRecord(BaseModel):
    """One device-to-device hop.

    Deliberately unsigned. Relay nodes append these in flight, so they
    cannot be covered by the sender's signature. Treated as observability
    data only; settlement never reads them.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: str = Field(pattern=IDENTIFIER_PATTERN)
    received_at: UtcTimestamp

    @field_validator("received_at")
    @classmethod
    def _require_utc(cls, value: datetime) -> datetime:
        return _normalize_to_utc(value)


# --- Packet ------------------------------------------------------------------


class PaymentPacket(BaseModel):
    """The unit that travels from sender to settlement, unchanged.

    "Unchanged" refers to the signed region: header fields plus the whole
    envelope plus the signature. ``hops`` grows as the packet is relayed and
    is excluded from the signature by design.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    packet_id: UUID
    sender_id: str = Field(pattern=IDENTIFIER_PATTERN)
    created_at: UtcTimestamp
    envelope: EncryptedEnvelope
    signature: B64UBytes
    hops: tuple[HopRecord, ...] = ()

    @field_validator("created_at")
    @classmethod
    def _require_utc(cls, value: datetime) -> datetime:
        return _normalize_to_utc(value)

    @field_validator("signature")
    @classmethod
    def _signature_is_ed25519_sized(cls, value: bytes) -> bytes:
        if len(value) != 64:
            raise ValueError("Ed25519 signature must be exactly 64 bytes")
        return value

    # --- Derived values ---

    @property
    def signing_bytes(self) -> bytes:
        """The canonical bytes the signature covers."""
        return build_signing_bytes(
            packet_id=str(self.packet_id),
            sender_id=self.sender_id,
            created_at=canonical_timestamp(self.created_at),
            encrypted_key=self.envelope.encrypted_key,
            nonce=self.envelope.nonce,
            ciphertext=self.envelope.ciphertext,
            aad=self.envelope.aad,
        )

    @property
    def idempotency_key(self) -> str:
        """Redis dedupe key, derived from the signed region only.

        Deliberately a plain property and not a serialized field: the key is
        never transmitted. Every node recomputes it from the signed bytes it
        actually received, so a sender cannot supply a key that disagrees
        with its own packet in order to dodge deduplication.
        """
        return derive_idempotency_key(self.signing_bytes)

    @property
    def hop_count(self) -> int:
        return len(self.hops)

    # --- Construction ---

    @classmethod
    def create(
        cls,
        *,
        instruction: PaymentInstruction,
        sender_id: str,
        signing_key: Ed25519PrivateKey,
        recipient_public_key: rsa.RSAPublicKey,
        created_at: datetime | None = None,
    ) -> Self:
        """Build a fully signed, encrypted packet from an instruction.

        The envelope's AAD binds the ciphertext to this packet's id and
        sender, so the sealed payload cannot be re-headered later.
        """
        packet_id = instruction.packet_id
        moment = _normalize_to_utc(created_at or datetime.now(UTC))
        created_at_text = canonical_timestamp(moment)

        aad = build_aad(packet_id=str(packet_id), sender_id=sender_id)
        payload = encrypt_payload(
            instruction.to_canonical_bytes(),
            recipient_public_key,
            aad,
        )

        signature = sign_bytes(
            build_signing_bytes(
                packet_id=str(packet_id),
                sender_id=sender_id,
                created_at=created_at_text,
                encrypted_key=payload.encrypted_key,
                nonce=payload.nonce,
                ciphertext=payload.ciphertext,
                aad=payload.aad,
            ),
            signing_key,
        )

        return cls(
            packet_id=packet_id,
            sender_id=sender_id,
            created_at=moment,
            envelope=EncryptedEnvelope(
                encrypted_key=payload.encrypted_key,
                nonce=payload.nonce,
                ciphertext=payload.ciphertext,
                aad=payload.aad,
            ),
            signature=signature,
        )

    def append_hop(self, hop: HopRecord) -> Self:
        """Return a copy with one more hop recorded.

        The signed region is untouched, so ``signature`` and
        ``idempotency_key`` are identical before and after.
        """
        return self.model_copy(update={"hops": (*self.hops, hop)})

    # --- Verification and opening ---

    def verify(self, public_key: Ed25519PublicKey) -> None:
        """Verify the signature. Raises ``SignatureVerificationError`` on failure."""
        verify_signature(self.signing_bytes, self.signature, public_key)

    def open_instruction(self, recipient_private_key: rsa.RSAPrivateKey) -> PaymentInstruction:
        """Decrypt the envelope and validate the instruction inside it.

        Also enforces the cross-check from ARCHITECTURE.md: the inner
        ``packet_id`` must equal the envelope-level one. Without this, a
        sender could sign a header for one id while sealing an instruction
        for another.

        Raises:
            DecryptionError: bad key, bad tag, or altered ciphertext/AAD.
            ValueError: plaintext is not a valid instruction, or the inner
                and outer packet ids disagree.
        """
        plaintext = decrypt_payload(
            encrypted_key=self.envelope.encrypted_key,
            nonce=self.envelope.nonce,
            ciphertext=self.envelope.ciphertext,
            aad=self.envelope.aad,
            recipient_private_key=recipient_private_key,
        )
        instruction = PaymentInstruction.from_canonical_bytes(plaintext)
        if instruction.packet_id != self.packet_id:
            raise ValueError("inner packet_id does not match envelope packet_id")
        return instruction

    def expected_aad(self) -> bytes:
        """The AAD this packet's header implies, for comparison against the envelope."""
        return build_aad(packet_id=str(self.packet_id), sender_id=self.sender_id)


# --- Service request/response models ----------------------------------------


class TransactionRequest(BaseModel):
    """Public input to the sender service. Validated before any crypto runs."""

    model_config = ConfigDict(extra="forbid")

    payer_id: str = Field(pattern=IDENTIFIER_PATTERN)
    payee_id: str = Field(pattern=IDENTIFIER_PATTERN)
    amount_minor: int = Field(gt=0, le=MAX_AMOUNT_MINOR)
    currency: str = Field(default="INR", pattern=r"^[A-Z]{3}$")

    @field_validator("payee_id")
    @classmethod
    def _payee_differs_from_payer(cls, value: str, info: Any) -> str:
        if info.data.get("payer_id") == value:
            raise ValueError("payer_id and payee_id must differ")
        return value

    def to_instruction(self, *, created_at: datetime | None = None) -> PaymentInstruction:
        """Turn a request into an instruction with a fresh packet id."""
        return PaymentInstruction(
            packet_id=uuid4(),
            payer_id=self.payer_id,
            payee_id=self.payee_id,
            amount_minor=self.amount_minor,
            currency=self.currency,
            created_at=_normalize_to_utc(created_at or datetime.now(UTC)),
        )


class PacketCreatedResponse(BaseModel):
    """What the sender service returns: the packet plus its derived identifiers.

    ``idempotency_key`` is surfaced here for the demo UI and for tests, as a
    convenience of this response envelope. It is not part of the packet and
    is not carried across hops.
    """

    model_config = ConfigDict(extra="forbid")

    packet: PaymentPacket
    idempotency_key: str
    status: PacketStatus = PacketStatus.CREATED

    @classmethod
    def of(cls, packet: PaymentPacket) -> Self:
        return cls(packet=packet, idempotency_key=packet.idempotency_key)


class ErrorResponse(BaseModel):
    """Uniform error body, so callers can branch on ``code`` not on prose."""

    model_config = ConfigDict(extra="forbid")

    code: ErrorCode
    detail: str


# --- Helpers -----------------------------------------------------------------


def _normalize_to_utc(value: datetime) -> datetime:
    """Require an explicit timezone and normalize to UTC.

    A naive datetime is rejected rather than assumed local: guessing the
    zone would make the signed timestamp ambiguous across hosts.
    """
    if value.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC)
