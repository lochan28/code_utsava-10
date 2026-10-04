# Problem Statement 4 — Code Utsava X.0

Transcribed from the official problem-statement screenshots. One stray bullet
under "The system must" ("Capture live camera frames of passing billets under
varying industrial ...") belongs to a different problem statement and is
omitted on the team lead's instruction.

**Domain:** Database Optimization, Artificial Intelligence, Data Privacy,
Reinforcement Learning, Graph Neural Networks

## Background
Database administrators struggle to maintain optimal performance in massive
relational databases as application query patterns evolve over time.
Identifying missing composite indexes, implementing sharding, or manually
rewriting complex joins is a heavy operational burden that risks disrupting
live production environments. Furthermore, leveraging AI to solve this
introduces severe security risks, as exposing raw, sensitive company data to
AI tuning models violates privacy and compliance standards.

## Problem Statement
There is a critical need for an AI-driven interactive dashboard that automates
database performance tuning while enforcing strict data guardrails. The system
must never read raw production data; instead, it must abstract incoming data
patterns to feed safe, anonymized representations to the AI.

**Example.** For a query such as "Why is the weekly sales reporting dashboard
timing out?", the dashboard should analyze the database's recent execution
plans using only metadata and masked attributes. It identifies a highly nested
join causing a sequential scan on a massive, unpartitioned table. The system
then safely rewrites the SQL, simulates the changes, and presents an
evidence-backed response like: "Partitioning the target table by month,
rewriting the join, and adding a composite index on (region_id,
transaction_date) will reduce query time by 85%, with only a 2 ms simulated
increase in write latency. Zero raw data was exposed during this analysis."

## The system must
- Abstract incoming data patterns using attribute bitmasking and structural
  metadata hashing to guarantee anonymity.
- Ingest these anonymized slow query logs and historical execution plans securely.
- Recommend optimal composite indexes and robust table partitioning or sharding
  strategies based on abstracted access patterns using Reinforcement Learning.
- Automatically rewrite inefficient SQL queries. Use AI and Graph Neural
  Networks (GNN) to provide natural language explanations of complex query
  execution trees.
- Leverage database extensions (e.g. PostgreSQL Hooks) to simulate the exact
  impact of proposed structural changes on write latency and storage without
  applying them to production.

## Deliverables
- **Data Obfuscation & Ingestion Engine:** a secure middleware module using data
  obfuscation / bitmasking to convert slow query logs and execution plans into
  structural metadata hashes before AI ingestion.
- **AI & RL Optimization Agent:** a Reinforcement Learning engine that analyzes
  abstracted access patterns to recommend optimal composite indexes,
  database sharding / partitioning schemes, and automatically rewrites
  inefficient SQL queries.
- **Execution Tree Analytics Module:** a Graph Neural Network (GNN) integrated
  with AI to map complex query execution trees and translate performance
  bottlenecks into clear, natural language explanations.
- **Simulation Sandbox:** a robust simulation framework leveraging PostgreSQL
  (Hooks / Extensions) to calculate the theoretical impact of structural
  recommendations (e.g. storage overhead, write-latency spikes) purely in a
  sandbox state.
- **Interactive Dashboard:** a user-facing AI agent interface that allows
  administrators to query performance issues naturally, view GNN-backed
  explanations, and assess simulated metrics prior to authorizing production
  deployment.

## Impact
- **Data Protection & Compliance:** ensures zero exposure of raw, sensitive
  company data to AI models, strictly adhering to enterprise privacy standards.
- **Operational Efficiency:** drastically reduces the time database
  administrators spend manually analyzing slow queries, rewriting SQL, and
  planning shards.
- **Zero-Risk Testing:** simulates structural alterations safely, preventing
  disastrous performance degradations in live production environments.
- **Explainability & Trust:** demystifies black-box AI recommendations by
  visually and textually explaining the underlying execution paths and bottlenecks.

## Performance & Technical Requirements
- **Zero Data Exposure:** 100% of sensitive raw data must be successfully
  bitmasked or hashed prior to AI processing.
- **Simulation Accuracy:** the simulated impacts on write latency and storage via
  PostgreSQL Hooks must closely mirror actual theoretical metrics without
  applying physical changes to live tables.
- **Optimization Efficacy:** the system's rewritten SQL and indexing
  recommendations should demonstrably reduce query execution time for targeted
  workloads (e.g. targeting a >50% improvement on inefficient joins).
- **Explainability:** every optimization intervention must include a
  human-readable GNN-generated reason and the specific metadata evidence that
  triggered the recommendation.
- **Robustness:** the RL model must generalize effectively to unseen, evolving
  query patterns rather than only optimizing for static historical logs.
