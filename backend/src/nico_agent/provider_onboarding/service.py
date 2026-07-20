"""Tenant-scoped Provider probe application service."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select

from nico_agent.database import Database, TenantContext
from nico_agent.domain.errors import DomainConflict, ResourceNotFound
from nico_agent.domain.models import AuditRecord, ProviderProbe
from nico_agent.provider_onboarding.catalog import get_provider_catalog
from nico_agent.provider_onboarding.contracts import (
    CandidateConfiguration,
    ProviderProbeCreate,
    canonical_candidate_hash,
)


class ProviderOnboardingService:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def create_probe(
        self,
        context: TenantContext,
        command: ProviderProbeCreate,
    ) -> ProviderProbe:
        self._validate_candidate(command.candidate)
        candidate_hash = canonical_candidate_hash(command.candidate)
        async with self.database.tenant_transaction(context) as session:
            existing = await session.scalar(
                select(ProviderProbe).where(
                    ProviderProbe.tenant_id == context.tenant_id,
                    ProviderProbe.idempotency_key == command.idempotency_key,
                )
            )
            if existing is not None:
                if existing.kind != command.kind or existing.candidate_hash != candidate_hash:
                    raise DomainConflict(
                        "PROVIDER_PROBE_IDEMPOTENCY_CONFLICT",
                        "provider probe idempotency key was already used for another candidate",
                    )
                return existing
            candidate = command.candidate
            probe = ProviderProbe(
                tenant_id=context.tenant_id,
                kind=command.kind,
                provider_key=candidate.provider_key,
                protocol=candidate.protocol,
                base_url=candidate.base_url,
                credential_ref=candidate.credential_ref,
                provider_options=candidate.provider_options,
                model_name=candidate.model,
                catalog_revision=candidate.catalog_revision,
                candidate_hash=candidate_hash,
                idempotency_key=command.idempotency_key,
            )
            session.add(probe)
            await session.flush()
            session.add(
                AuditRecord(
                    tenant_id=context.tenant_id,
                    action="provider_probe.create",
                    resource_type="provider_probe",
                    resource_id=probe.id,
                    actor_id=context.actor_id,
                    details={
                        "kind": probe.kind,
                        "provider_key": probe.provider_key,
                        "candidate_hash": probe.candidate_hash,
                    },
                    correlation_id=context.correlation_id,
                )
            )
            await session.flush()
            return probe

    async def get_probe(self, context: TenantContext, probe_id: UUID) -> ProviderProbe:
        async with self.database.tenant_transaction(context) as session:
            probe = await session.scalar(
                select(ProviderProbe).where(
                    ProviderProbe.tenant_id == context.tenant_id,
                    ProviderProbe.id == probe_id,
                )
            )
            if probe is None:
                raise ResourceNotFound("provider_probe", str(probe_id))
            return probe

    async def cancel_probe(self, context: TenantContext, probe_id: UUID) -> ProviderProbe:
        async with self.database.tenant_transaction(context) as session:
            probe = await session.scalar(
                select(ProviderProbe)
                .where(
                    ProviderProbe.tenant_id == context.tenant_id,
                    ProviderProbe.id == probe_id,
                )
                .with_for_update()
            )
            if probe is None:
                raise ResourceNotFound("provider_probe", str(probe_id))
            if probe.status == "cancelled":
                return probe
            if probe.status not in {"pending", "running"}:
                raise DomainConflict(
                    "PROVIDER_PROBE_TERMINAL",
                    "completed provider probes cannot be cancelled",
                )
            probe.status = "cancelled"
            probe.completed_at = datetime.now(UTC)
            probe.lease_token = None
            probe.lease_expires_at = None
            probe.revision += 1
            session.add(
                AuditRecord(
                    tenant_id=context.tenant_id,
                    action="provider_probe.cancel",
                    resource_type="provider_probe",
                    resource_id=probe.id,
                    actor_id=context.actor_id,
                    details={"candidate_hash": probe.candidate_hash},
                    correlation_id=context.correlation_id,
                )
            )
            await session.flush()
            return probe

    @staticmethod
    def _validate_candidate(candidate: CandidateConfiguration) -> None:
        catalog = get_provider_catalog()
        if candidate.catalog_revision != catalog.catalog_revision:
            raise DomainConflict(
                "PROVIDER_CATALOG_STALE",
                "provider candidate uses a stale catalog revision",
            )
        provider = next(
            (item for item in catalog.providers if item.key == candidate.provider_key),
            None,
        )
        if provider is None:
            raise DomainConflict("PROVIDER_NOT_FOUND", "provider preset is not available")
        if candidate.protocol != provider.protocol:
            raise DomainConflict(
                "PROVIDER_PROTOCOL_ERROR",
                "provider candidate protocol does not match the catalog",
            )
        if candidate.base_url not in {location.base_url for location in provider.locations}:
            raise DomainConflict(
                "PROVIDER_LOCATION_INVALID",
                "provider candidate service location is not in the catalog",
            )
        allowed_options = {option.key for option in provider.options}
        unknown_options = sorted(set(candidate.provider_options) - allowed_options)
        if unknown_options:
            raise DomainConflict(
                "PROVIDER_OPTIONS_INVALID",
                "provider candidate contains unsupported options",
                details={"option_keys": unknown_options},
            )
