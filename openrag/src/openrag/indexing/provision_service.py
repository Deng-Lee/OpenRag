"""Stateful orchestration around the idempotent Milvus provisioner."""

from dataclasses import dataclass

from openrag.indexing.milvus_provisioner import MilvusCollectionProvisioner
from openrag.models.index_generation import IndexGeneration, IndexGenerationState
from openrag.services.index_generation_service import (
    GenerationStateTransitionError,
    IndexGenerationService,
)


@dataclass(frozen=True)
class ProvisionResult:
    generation: IndexGeneration
    physical_collections: dict[str, str]
    changed: bool


class ProvisionService:
    def __init__(
        self,
        generation_service: IndexGenerationService,
        provisioner: MilvusCollectionProvisioner,
    ):
        self.generation_service = generation_service
        self.provisioner = provisioner

    def provision(self, generation_id: str) -> ProvisionResult:
        generation = self.generation_service._require_generation(generation_id)
        if generation.state == IndexGenerationState.BUILDING.value:
            resources = self.verify_result(generation)
            return ProvisionResult(generation, resources, False)
        if generation.state == IndexGenerationState.FAILED.value:
            if generation.last_error_code != "INDEX_PROVISION_FAILED":
                raise GenerationStateTransitionError(
                    "Only a failed provisioning operation can be retried"
                )
        elif generation.state not in {
            IndexGenerationState.DRAFT.value,
            IndexGenerationState.PROVISIONING.value,
        }:
            raise GenerationStateTransitionError(
                f"Generation cannot be provisioned in state {generation.state}"
            )
        if generation.state != IndexGenerationState.PROVISIONING.value:
            generation = self.generation_service.transition_state(
                generation.id, IndexGenerationState.PROVISIONING
            )
        try:
            resources = self.provisioner.provision_generation(generation.manifest)
            resources = self.verify_result(generation, resources)
        except Exception as exc:
            self.generation_service.record_provision_result(
                generation.id,
                success=False,
                error="Candidate collection provisioning failed",
            )
            raise RuntimeError("INDEX_PROVISION_FAILED") from exc
        generation = self.generation_service.record_provision_result(
            generation.id, success=True
        )
        return ProvisionResult(generation, resources, True)

    def verify_result(
        self,
        generation: IndexGeneration,
        resources: dict[str, str] | None = None,
    ) -> dict[str, str]:
        observed = resources or self.provisioner.provision_generation(
            generation.manifest
        )
        expected = {
            "chunks": generation.chunk_collection_name,
            "layers": generation.layer_collection_name,
        }
        if observed != expected:
            raise RuntimeError("INDEX_PROVISION_RESULT_MISMATCH")
        return observed
