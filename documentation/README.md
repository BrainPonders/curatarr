# Documentation

Keep only tracked project documentation here.

Use this folder for:

- public product documentation
- architecture and decision-model references
- tracked maintainer-facing operational references
- repository governance material that belongs with the project

## Governance Model

- Principle documents own rules and shared policy.
- `README.md` files own scope, inventory, and navigation.
- Runbooks own executable procedures.
- Keep each mutable fact authoritative in one place and reference that owner document elsewhere.
- Normalize existing docs progressively instead of forcing destructive rewrites.

## Index

- [Feature register](../maintainer/FEATURES.md): current implementation/availability inventory
- [Roadmap](../maintainer/ROADMAP.md): delivery sequence and remaining data work
- [Decision register](Decisions.md): material changes, rationale and approval status

- [Maintainer workflow](../maintainer/WORKFLOW.md#resuming-in-a-new-chat)
  - new-chat resume instructions, verified development baseline and next implementation priority
- [Deployment and database correction](../maintainer/development/README.md#deployment-and-database-correction)
  - repository role, deployment handoff, supported correction boundaries and outside-app commissioning safeguards

- [Ryot Collection Contract](Ryot%20Collection%20Contract.md)
  - authoritative collection mapping, native Owned/Completed boundaries, accepted setup baseline, maintainer briefing, and implemented read-model boundary

- [Media Status and Display Rules](Media%20Status%20and%20Display%20Rules.md)
  - human-facing labels, evidence requirements, display priority, and pending UI decisions

- `documentation/Documentation Governance Principles.md`
  - tracked rules for documentation ownership and runtime header usage
- [System Architecture](System%20Architecture.md)
  - Curatarr product model, request flow, and decision-state model
  - [Coordinator transition review](System%20Architecture.md#coordinator-transition-review--2026-10-24): sole policy ownership, complete workflow review, bypass integrations and staged Ryot cutover
  - [Major development planning](System%20Architecture.md#major-development-planning--thresholds-and-discovery-intake) and [answered build contract](System%20Architecture.md#answered-build-contract--2026-10-24): Q01–Q22 decisions and exceptions
  - [Coordinated movie release specification](System%20Architecture.md#coordinated-movie-release--build-specification): shared execution, source/UX contracts and AC01–AC20 acceptance cases
  - [Implementation plan](../maintainer/ROADMAP.md#coordinated-release-implementation-plan): W01–W09 dependencies and release gates
- `documentation/Reference Survey.md`
  - comparison of upstream Searcharr lineage and selective reuse boundaries for Curatarr
- [NFO Audio Evidence Evaluation](NFO%20Audio%20Evidence%20Evaluation.md)
  - verified household findings, integration limits, and deferred manual/automatic enrichment options; original roadmap takes priority

## Runtime Headers

- Operational artifacts such as compose files, `.env` files, Dockerfiles, and scripts should use stable header blocks.
- When a project uses the local header-template library, keep the visible `Header template reference:` line in the generated file.
- Treat `.local/Header Templates/` as a local source mirror only; tracked docs should describe the rule, not duplicate the local library.

Do not use this folder for:

- personal runbooks
- scratch notes
- reusable private templates
- local-only overlays

Those belong in `.local/` instead.
