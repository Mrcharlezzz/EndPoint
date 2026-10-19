# Documentation

This index collects the project documentation included in the public portfolio snapshot. Design documents describe the intended system at the time they were written; consult the source code for current behavior. Private datasets, production records, operational logs, and internal planning documents are excluded.

## Project design

- [Proposal](artifacts/proposal/proposal-v1.md) — problem and approach.
- [Product requirements](artifacts/specs/specs-v2.2.md) — user needs and functional scope.
- [MVP design](artifacts/design-docs/mvp-design-v1.md) — initial scope and design choices.
- [System architecture](artifacts/design-docs/architecture.md) — components and data flow.
- [Component structure](COMPONENT_STRUCTURE.md) — repository layout and boundaries.
- [Database schema](artifacts/db-schema/db-schema-v5.md) — shared data model design.

## Component guides

- [Ingestion overview](ingestion/OVERVIEW.md), [connector reference](ingestion/CONNECTORS.md), [schema](ingestion/SCHEMA.md), [conventions](ingestion/CONVENTIONS.md), [connector guide](ingestion/ADDING_A_CONNECTOR.md), and [integration guide](ingestion/INTEGRATION_GUIDE.md).
- [Processing overview](processing-engine/v4/processor-overview-v4.3.md) and [architecture](processing-engine/v4/processor-architecture-v4.1.md).
- [Shared database developer guide](db-core/common-db-developer-guide.md).

For component-specific setup and commands, see the README in that component's source directory.
