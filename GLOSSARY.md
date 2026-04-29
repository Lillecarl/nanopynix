# pynixd Glossary

This document defines technical terms and concepts used within the `pynixd` codebase.

> **Note**: This glossary is a living document. It is not exhaustive and SHOULD be expanded as new concepts are discovered, clarified, or added to the system.

## Foundational Nix Concepts

### Expression
The high-level code written in the Nix language (typically in `.nix` files). Expressions are evaluated to produce Derivations.

### Derivation (.drv)
A low-level build recipe. It is a static representation of a build task, containing paths to all required inputs (sources and other derivations), the builder script, and the intended output names.

### StorePath
A unique, immutable path within the Nix store (e.g., `/nix/store/z12...-hello-2.12`). The hash in the path is derived from the inputs used to produce the item.

### NAR (Nix Archive)
The serialization format Nix uses to represent a file system object (file, directory, or symlink) as a single byte stream. It is used for transferring paths between stores.

### Hash
Nix primarily uses SHA-256 hashes to identify store paths and ensure the integrity of the store's contents.

## pynixd Core Concepts

### Store
A polymorphic representation of a Nix store. `pynixd` can interact with local stores, remote SSH stores, S3 buckets, etc., using a unified interface.

### DAG (Directed Acyclic Graph)
The dependency graph of derivations. `pynixd`'s scheduler is "DAG-aware," meaning it understands the order in which builds must execute based on their dependencies.

### Content-Addressed (CA) Derivation
A derivation where the output path is determined by the hash of the *actual produced content* rather than the derivation's inputs. This allows for "early cutoff" optimizations.

### Realisation
The concrete mapping between a derivation output and its final `StorePath`. For CA derivations, this mapping is only known after the build is complete.

### Deferred Output
An output path that cannot be calculated before the build (common in CA derivations). These are resolved "on the fly" by the `DynamicDerivationResolver`.

### Trampolining
A Nix-native concept for "unknown output" building (common in CA derivations). It is the process where a build is intercepted because it has "deferred" dependencies that must be resolved first. Once resolved (possibly by triggering sub-builds), the original derivation is updated and re-injected into the build process. In `pynixd`, this is handled by the `DynamicDerivationResolver`.

## pynixd Architecture

### Three-Tier Execution
The pattern used to separate protocol IO from logic:
1. **Server Dispatch** (`handle`): Entry point that decodes from the client wire.
2. **Logic Hook** (`execute`): Implements optimizations and core business logic.
3. **Transport** (`call`): Low-level wire protocol implementation for upstream communication.

### Proxy (`DaemonProxy`)
The component that manages the connection from a Nix client, handles the protocol handshake, and routes requests to the internal logic.

### Tracker (`PathTracker`)
An internal database (SQLite) that tracks which `StorePath`s are available on which `Store`, enabling locality-aware scheduling.

### Ranker / Allocator
*   **Ranker**: Scores available stores based on telemetry (CPU pressure, latency, etc.).
*   **Allocator**: Matches a pending build task to the most appropriate store based on ranks and requirements.
