# Useful commands
- nix run --file . pytest -- $args
- nix run --file . pyright -- $args
- nix run --file . ruff -- $args

# Design notes

**Nix "stderr" = logging, not OS stderr**: Nix uses "stderr" terminology to
refer to `nix::Logger` log events. These already flow through the worker↔master
RPC pipe as `action: "msg"` / `action: "error"` events. Worker IPC uses only
stdin/stdout (JSON-RPC protocol); actual subprocess fd 2 inherits the parent.
Do NOT add a separate stderr pipe — it would be redundant and conflate Nix's
logging abstraction with OS-level stderr.
