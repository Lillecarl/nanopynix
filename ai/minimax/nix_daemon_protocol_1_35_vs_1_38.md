# Nix Daemon Protocol Differences: 1.35 (Lix) vs 1.38 (Nix)

> Documentation of every serialization difference between Nix daemon protocol version 1.35 and 1.38.
> Used for implementing both versions in Python (e.g., pynixd).

## Version Semantics

- Protocol version is encoded as `(major << 8) | minor`
- Nix uses `GET_PROTOCOL_MINOR(v)` which is `(v) & 0xFF` — so `>= 36` means version 1.36+
- All integers on the wire are **little-endian uint64**
- Strings are uint64-length-prefixed, zero-padded to 8-byte alignment

---

## Handshake

### Magic Constants (identical in both)

```cpp
#define WORKER_MAGIC_1 0x6e697863  // "nixc"
#define WORKER_MAGIC_2 0x6478696f  // "dxio"
```

### Lix 1.35 Handshake (no feature negotiation)

```
CLIENT → SERVER: WORKER_MAGIC_1 (0x6e697863)
SERVER → CLIENT: WORKER_MAGIC_2 (0x6478696f) + PROTOCOL_VERSION (0x0123 = 291)
CLIENT → SERVER: client protocol version (uint64)
CLIENT → SERVER: 0 (obsolete CPU affinity, if proto >= 1.14)
CLIENT → SERVER: 0 (obsolete reserveSpace, if proto >= 1.11)
SERVER → CLIENT: nixVersion string (if proto >= 1.33)
SERVER → CLIENT: TrustedFlag as uint64: 0=NotTrusted, 1=Trusted, 2=NotTrusted (if proto >= 1.35)
```

### Nix 1.38 Handshake (with feature negotiation)

```
CLIENT → SERVER: WORKER_MAGIC_1 (0x6e697863)
SERVER → CLIENT: WORKER_MAGIC_2 (0x6478696f) + daemon protocol version (uint64)
CLIENT → SERVER: client protocol version (uint64)
CLIENT → SERVER: localVersion.features (string set, since 1.38)
SERVER → CLIENT: daemonFeatures (string set, since 1.38)
```

**Post-handshake (both, for backwards compat):**

```
CLIENT → SERVER: 0 (obsolete CPU affinity, if proto >= 1.14)
CLIENT → SERVER: 0 (obsolete reserveSpace, if proto >= 1.11)
SERVER → CLIENT: daemonNixVersion string (if proto >= 1.33)
SERVER → CLIENT: remoteTrustsUs (TrustedFlag wrapped in optional, if proto >= 1.35)
```

**Key Difference:** Feature negotiation (string set exchange) only exists in Nix 1.38+.

### Feature Negotiation (1.38+ only)

After basic handshake, if `protoVersion >= 1.38`:

```python
# Client sends its features as string set
wire.write_string_set(w, local_features)  # e.g., {"recursive-nix-copy"}
await w.drain()

# Server responds with its features
server_features = await wire.read_string_set(r)

# Effective features = intersection
effective_features = server_features & local_features
```

Lix 1.35 does **not** perform this step.

---

## Operations with Version-Dependent Serialization

### Op::IsValidPath (op code 1)

**Request:** `store path` (string)
**Response:** `isValid` (bool)

No version differences between 1.35 and 1.38.

---

### Op::QueryValidPaths (op code 31)

**Request:**
```
paths (StorePathSet) + substitute flag (uint64, only if proto >= 1.27)
```

```cpp
// Nix 1.38 daemon.cc
if (conn.protoVersion >= WorkerProto::Version{.number = {1, 27}}) {
    substitute = readInt(conn.from) ? Substitute : NoSubstitute;
}
```

**Lix 1.35:** Always reads substitute flag.

**Response:** `StorePathSet` (no version difference)

---

### Op::AddToStore (op code 7)

**Two serialization formats based on protocol version:**

**Protocol >= 1.25:**
```
name (string) + camStr (string, content address method) +
references (StorePathSet) + repair (bool) + NAR (framed)
```

**Protocol < 1.25 (legacy):**
```
baseName (string) + fixed (bool, obsolete) + recursive (uint8) +
hashAlgoRaw (string) + NAR (direct dump)
```

**Response:**
- Protocol >= 1.25: `ValidPathInfo` (keyed)
- Protocol < 1.25: `StorePath` only

Lix 1.35 only supports >= 1.25 format. Nix 1.38 handles both.

---

### Op::BuildDerivation (op code 36)

**Request:**
```
drvPath (string, store path) + BasicDerivation + BuildMode (uint64)
```

**BasicDerivation wire format:**
```
[outputs count:uint64]
  [output name:string]
  [DerivationOutput]
    InputAddressed: [path:string] [hashAlgo:""] [hash:""]
    CAFixed: [path:""] [hashAlgo:string] [hash:string (base16)]
[inputSrcs:StorePathSet]
[platform:string]
[builder:string]
[args:Strings]
[env count:uint64]
  [key:string] [value:string]
```

No derivation format changes between 1.35 and 1.38.

**Response:** `BuildResult` (see BuildResult section)

---

### Op::NarFromPath (op code 38)

**Request:** `path` (string)
**Response:** NAR dump (bytes)

No version guards — identical in both versions.

---

### Op::AddToStoreNar (op code 39)

**Request:**
```
path (StorePath) + deriver (optional StorePath) + narHash (string) +
references (StorePathSet) + registrationTime (uint64) + narSize (uint64) +
ultimate (bool) + sigs (StringSet) + ca (string) +
repair (bool) + dontCheckSigs (bool) + NAR
```

**Version-dependent NAR transmission (Nix 1.38):**
```cpp
if (conn.protoVersion >= WorkerProto::Version{.number = {1, 23}}) {
    FramedSource source(conn.from);        // framed NAR
} else if (conn.protoVersion >= WorkerProto::Version{.number = {1, 21}}) {
    TunnelSource source(conn.from, conn.to); // tunnel mode
} else {
    // Direct NAR copy
}
```

---

### Op::QueryMissing (op code 40)

**Request:** `DerivedPaths`
**Response:**
```
willBuild (StorePathSet) + willSubstitute (StorePathSet) +
unknown (StorePathSet) + downloadSize (uint64) + narSize (uint64)
```

No version guards since minimum protocol 1.19.

---

### Op::QueryDerivationOutputMap (op code 41)

**Request:** `path` (string, store path to .drv)
**Response:** `std::map<std::string, std::string>` (output name → path)

No server-side version guards. Client has fallback for < 1.22.

---

### Op::RegisterDrvOutput (op code 42)

**Request changes by version:**

**Version < 1.31:**
```
DrvOutput (string, e.g. "sha256:abc...!out") + outputPath (string)
```

**Version >= 1.31:**
```
Realisation (JSON string)
```

```cpp
// Nix 1.38 daemon.cc
if (conn.protoVersion.number < WorkerProto::Version::Number{1, 31}) {
    auto outputId = WorkerProto::Serialise<DrvOutput>::read(*store, rconn);
    auto outputPath = StorePath(readString(conn.from));
    store->registerDrvOutput(Realisation{{.outPath = outputPath}, outputId});
} else {
    auto realisation = WorkerProto::Serialise<Realisation>::read(*store, rconn);
    store->registerDrvOutput(realisation);
}
```

**Lix 1.35:** Throws `UnimplementedError("ca derivations are not supported")`

---

### Op::QueryRealisation (op code 43)

**Response changes by version:**

**Version < 1.31:** `std::set<StorePath>`
**Version >= 1.31:** `std::set<Realisation>` (JSON-serialized)

```cpp
if (conn.protoVersion.number < WorkerProto::Version::Number{1, 31}) {
    std::set<StorePath> outPaths;
    if (info) outPaths.insert(info->outPath);
    WorkerProto::write(*store, wconn, outPaths);
} else {
    std::set<Realisation> realisations;
    if (info) realisations.insert({*info, outputId});
    WorkerProto::write(*store, wconn, realisations);
}
```

**Lix 1.35:** Throws `UnimplementedError("ca derivations are not supported")`

---

### Op::AddMultipleToStore (op code 44)

**Request:**
```
repair (bool) + dontCheckSigs (bool) + [ValidPathInfo + framed NAR]*
```

**Version guard (Nix 1.38 client):**
```cpp
// Nix 1.38 remote-store.cc
if (getConnection()->protoVersion >= WorkerProto::Version{.number = {1, 32}}) {
    conn->to << WorkerProto::Op::AddMultipleToStore << repair << !checkSigs;
    conn.withFramedSink([&](Sink & sink) { source.drainInto(sink); });
} else {
    // Legacy path
}
```

**Lix 1.35:** Uses legacy format without framed sink optimization.

---

### Op::BuildPathsWithResults (op code 46)

**Request:** `DerivedPaths` + `BuildMode` (uint64)
**Response:** `std::vector<KeyedBuildResult>`

Introduced in Nix 1.34.

---

## New Operations in Nix 1.38 (not in Lix 1.35)

### Op::AddPermRoot (op code 47) — **Only in Nix 1.38**

Lix 1.35 max op code is 46.

**Request:**
```
storePath (StorePath) + gcRoot (string, absolute filesystem path)
```

**Response:** `gcRoot` (string)

```cpp
// Nix 1.38 daemon.cc
case WorkerProto::Op::AddPermRoot: {
    if (!trusted) throw Error("you are not privileged to create perm roots");
    auto storePath = WorkerProto::Serialise<StorePath>::read(*store, rconn);
    std::filesystem::path gcRoot = absPath(readString(conn.from));
    auto & localFSStore = require<LocalFSStore>(*store);
    localFSStore.addPermRoot(storePath, gcRoot);
    conn.to << gcRoot.string();
    break;
}
```

---

## Type Serialization Changes

### BuildResult

**Wire format:**
```
[status:uint64] + [errorMsg:string]
```

**Version-guarded fields:**

| Field | Version Added | Nix 1.38 Guard | Lix 1.35 Behavior |
|-------|---------------|----------------|-------------------|
| `builtOutputs` (DrvOutputs) | 1.28 | `>= 1.28` | Always sent |
| `timesBuilt` | 1.29 | `>= 1.29` | Always sent |
| `isNonDeterministic` | 1.29 | `>= 1.29` | Always sent |
| `startTime` | 1.29 | `>= 1.29` | Always sent |
| `stopTime` | 1.29 | `>= 1.29` | Always sent |
| `cpuUser` | 1.37 | `>= 1.37` | Always sent |
| `cpuSystem` | 1.37 | `>= 1.37` | Always sent |

**Nix 1.38 BuildResult read (with guards):**
```cpp
auto status = WorkerProto::Serialise<BuildResultStatus>::read(store, conn);
conn.from >> errorMsg;

if (conn.version >= WorkerProto::Version{.number = {1, 29}}) {
    conn.from >> res.timesBuilt >> isNonDeterministic >> res.startTime >> res.stopTime;
}

if (conn.version >= WorkerProto::Version{.number = {1, 37}}) {
    res.cpuUser = WorkerProto::Serialise<std::optional<std::chrono::microseconds>>::read(store, conn);
    res.cpuSystem = WorkerProto::Serialise<std::optional<std::chrono::microseconds>>::read(store, conn);
}

if (conn.version >= WorkerProto::Version{.number = {1, 28}}) {
    auto builtOutputs = WorkerProto::Serialise<DrvOutputs>::read(store, conn);
    for (auto && [output, realisation] : builtOutputs)
        success.builtOutputs.insert_or_assign(std::move(output.outputName), std::move(realisation));
}
```

**Critical issue:** Lix 1.35 sends ALL fields unconditionally (no version guards). When Lix 1.35 client talks to Nix 1.38 server, this is fine. When Nix 1.38 talks to older Nix, server properly guards. But Python implementations must handle both patterns.

---

### ValidPathInfo

**UnkeyedValidPathInfo wire format:**
```
[deriver:string or ""] + [narHash:string (SHA256 base16)] +
[references:StorePathSet] + [registrationTime:uint64] + [narSize:uint64]
```

**Version-guarded fields (added at 1.16):**
```
[ultimate:bool] + [sigs:StringSet] + [ca:string]
```

```cpp
if (conn.version >= WorkerProto::Version{.number = {1, 16}}) {
    conn.from >> info.ultimate;
    info.sigs = WorkerProto::Serialise<std::set<Signature>>::read(store, conn);
    info.ca = ContentAddress::parseOpt(readString(conn.from));
}
```

**Keyed variant** (`ValidPathInfo` = `UnkeyedValidPathInfo` + `StorePath`):
```
[path:string] + [UnkeyedValidPathInfo fields above]
```

---

### TrustedFlag Serialization

**Lix 1.35 wire format (simple uint64):**
```
0 = NotTrusted, 1 = Trusted, 2 = NotTrusted (explicit)
```

**Nix 1.38 ClientHandshakeInfo (optional-wrapped for >= 1.35):**
```
[1] + [TrustedFlag value]  // Some(TrustedFlag)
[0]                        // None
```

```cpp
// Nix 1.38 worker-protocol.cc
if (conn.version >= WorkerProto::Version{.number = {1, 35}}) {
    res.remoteTrustsUs = WorkerProto::Serialise<std::optional<TrustedFlag>>::read(store, conn);
}
```

---

### DerivedPath Serialization

**Version >= 1.30:**
```cpp
conn.to << req.to_string_legacy(store);  // legacy string format
```

**Version < 1.30:**
```cpp
// Must use StorePathWithOutputs format
```

```cpp
if (conn.version >= WorkerProto::Version{.number = {1, 30}}) {
    return DerivedPath::parseLegacy(store, s);
} else {
    return parsePathWithOutputs(store, s).toDerivedPath();
}
```

---

### DrvOutput and Realisation Serialization

**DrvOutput** (since 1.31):
```
[string]  // e.g., "sha256:abc...!out"
```

**Realisation** (since 1.31):
```
[string]  // JSON string, e.g., "{\"outPath\":\"/nix/store/...\",\"id\":\"sha256:abc...!out\"}"
```

```cpp
// Nix 1.38 common-protocol.cc
Realisation CommonProto::Serialise<Realisation>::read(const StoreDirConfig & store, CommonProto::ReadConn conn)
{
    std::string rawInput = readString(conn.from);
    return nlohmann::json::parse(rawInput);
}

void CommonProto::Serialise<Realisation>::write(
    const StoreDirConfig & store, CommonProto::WriteConn conn, const Realisation & realisation)
{
    conn.to << static_cast<nlohmann::json>(realisation).dump();
}
```

---

### Error Serialization

**Version >= 1.26:**
```cpp
ex = std::make_exception_ptr(readError(from));  // ErrorInfo format
```

**Version < 1.26:**
```cpp
auto error = readString(from);
unsigned int status = readInt(from);
ex = std::make_exception_ptr(Error(status, error));
```

---

## Complete Version Gate Reference

| Version | Feature |
|---------|---------|
| 1.10+ | Minimum Nix daemon version |
| 1.11+ | `reserveSpace` obsolete field in post-handshake |
| 1.14+ | CPU affinity obsolete field in post-handshake |
| 1.16+ | `ultimate`, `sigs`, `ca` fields in ValidPathInfo |
| 1.17+ | Valid path check flag in `QueryPathInfo` response |
| 1.18+ | **Protocol minimum** |
| 1.19+ | `QueryMissing` operation |
| 1.21+ | TunnelSource for NAR in `AddToStoreNar` |
| 1.22+ | `StorePathCAMap` for `QuerySubstitutablePathInfos` |
| 1.23+ | FramedSource for NAR in `AddToStoreNar` |
| 1.25+ | New `AddToStore` format with content-address method |
| 1.26+ | Error serialization uses ErrorInfo |
| 1.27+ | `SubstituteFlag` in `QueryValidPaths` |
| 1.28+ | `builtOutputs` (DrvOutputs) in BuildResult |
| 1.29+ | `timesBuilt`, `isNonDeterministic`, `startTime`, `stopTime` in BuildResult |
| 1.30+ | `DerivedPath` uses legacy format string |
| 1.31+ | `RegisterDrvOutput`/`QueryRealisation` use Realisation format |
| 1.32+ | Optimized `AddMultipleToStore` with FramedSink |
| 1.33+ | `daemonNixVersion` in ClientHandshakeInfo |
| 1.34+ | `BuildPathsWithResults` operation |
| 1.35+ | `remoteTrustsUs` (TrustedFlag) in ClientHandshakeInfo |
| 1.36+ | Dynamic derivations compatibility |
| 1.37+ | `cpuUser`, `cpuSystem` in BuildResult |
| **1.38+** | **Feature negotiation (current latest)** |

---

## Dynamic Derivations (Protocol 1.36+)

**Important:** Lix 1.35 does **not** support dynamic derivations at all. This is a Nix-specific feature introduced around protocol 1.36.

### What Are Dynamic Derivations?

Dynamic derivations allow a derivation to depend on **specific outputs** of another derivation, rather than the entire derivation. This is necessary for:

1. **Text-hashed outputs** (`textHash` CA method) — where the output hash depends on the output content, but that content isn't known until the derivation producing it has been built
2. **Floating CA derivations** — where the output path isn't fixed until the derivation is built
3. **Impure derivations** — where output can vary across builds

### The Problem with Traditional Derivation Dependencies

**Traditional format (`Derive(...)`):**
```
Derive([...outputs...],[
  (...drvPath1..., ["out" "lib"])   # input drv + ALL its outputs
  (...drvPath2..., ["dev"])          # input drv + specific outputs
],[...inputSrcs...],...)
```

The old format allows specifying which outputs of an input derivation you want, but:
- It doesn't support dependencies on outputs of outputs (dynamic -> dynamic chains)
- It doesn't properly handle the case where an output's path isn't known until build time

### New Dynamic Derivations Format (`DrvWithVersion("xp-dyn-drv",...)`)

**When triggered:** Any derivation that has `inputDrvs.map[drvPath].childMap` non-empty (i.e., depends on outputs of dynamic derivations).

**Wire format difference (ATerm level):**

**Traditional `Derive`:**
```
Derive([...outputs...],[
  (drvPath,[outputName,outputName2])   # Simple list of output names
],[...],[platform],[builder],[args],[...env...])
```

**Dynamic `DrvWithVersion("xp-dyn-drv")`:**
```
DrvWithVersion("xp-dyn-drv",[...outputs...],[
  (drvPath,(outputName1,outputName2,[   # Can have nested structure
    (nestedOutputName,[...])
  ]))
],[...],[platform],[builder],[args],[...env...])
```

### Parsing the Input Drv Map

**File:** `/home/lillecarl/Code/nix/src/libstore/derivations.cc`

```cpp
enum struct DerivationATermVersion {
    Traditional,         // "Derive(...)"
    DynamicDerivations,  // "DrvWithVersion("xp-dyn-drv",...)"
};

DerivedPathMap<StringSet>::ChildNode parseDerivedPathMapNode(..., DerivationATermVersion version)
{
    switch (version) {
    case DerivationATermVersion::Traditional:
        // Old format: just a list of output names
        node.value = parseStrings(str, false);
        break;

    case DerivationATermVersion::DynamicDerivations:
        // New format: can be either:
        switch (str.peek()) {
        case '[':
            // Non-dynamic: just a list of output names
            node.value = parseStrings(str, false);
            break;
        case '(':
            // Dynamic: nested structure with childMap
            expect(str, '(');
            node.value = parseStrings(str, false);  // output names
            expect(str, ",["sv);
            while (!endOfList(str)) {
                expect(str, '(');
                auto outputName = parseString(str);
                // Recursive parsing for nested dynamic outputs
                node.childMap.insert_or_assign(
                    outputName,
                    parseDerivedPathMapNode(store, str, version));
                expect(str, ')');
            }
            expect(str, ')');
            break;
        }
        break;
    }
}
```

### How `hasDynamicDrvDep()` Works

```cpp
static bool hasDynamicDrvDep(const Derivation & drv)
{
    return std::find_if(
               drv.inputDrvs.map.begin(),
               drv.inputDrvs.map.end(),
               [](auto & kv) { return !kv.second.childMap.empty(); })
           != drv.inputDrvs.map.end();
}
```

If any input derivation has a non-empty `childMap`, the derivation uses the `DrvWithVersion` format.

### Serialization (unparse)

```cpp
if (hasDynamicDrvDep(*this)) {
    s += "DrvWithVersion("sv;
    printUnquotedString(s, "xp-dyn-drv"sv);  // Version identifier
    s += ',';
} else {
    s += "Derive("sv;
}
```

### Protocol Version Gate (1.36)

**File:** `/home/lillecarl/Code/nix/src/libstore/worker-protocol-connection.cc` (lines 122-131)

```cpp
if (experimentalFeatureSettings.isEnabled(Xp::DynamicDerivations)
    && protoVersion.number < WorkerProto::Version::Number{1, 36}) {
    auto m = e.msg();
    if (m.find("parsing derivation") != std::string::npos
        && m.find("expected string") != std::string::npos
        && m.find("Derive([") != std::string::npos)
        return std::make_exception_ptr(Error(
            "%s, this might be because the daemon is too old to understand "
            "dependencies on dynamic derivations. Check to see if the raw "
            "derivation is in the form '%s'",
            std::move(m),
            "Drv WithVersion(..)"));
}
```

This is a **client-side** compatibility check — if we're using dynamic derivations but talking to a daemon older than 1.36, we get a helpful error message.

### Experimental Feature Requirement

Dynamic derivations require the `DynamicDerivations` experimental feature:

```cpp
// derivations.cc
if (*versionS == "xp-dyn-drv"sv) {
    version = DerivationATermVersion::DynamicDerivations;
    xpSettings.require(Xp::DynamicDerivations, [&] {
        return fmt("derivation '%s', ATerm format version 'xp-dyn-drv'", name);
    });
}

// Also required for text-hashed outputs
if (method == ContentAddressMethod::Raw::Text)
    xpSettings.require(Xp::DynamicDerivations, "text-hashed derivation output");
```

### Dynamic Outputs in JSON Format

For JSON derivation representation, dynamic outputs appear as `"dynamicOutputs"`:

```json
{
  "drvPath": "/nix/store/xxx.drv",
  "outputs": {"out": "path"},
  "inputDrvs": {
    "/nix/store/yyy.drv": {
      "outputs": ["out"],
      "dynamicOutputs": {
        "out": {
          "outputs": ["lib"],
          "dynamicOutputs": {}
        }
      }
    }
  }
}
```

### Wire Format Summary

| Aspect | Traditional (`Derive`) | Dynamic (`DrvWithVersion`) |
|--------|------------------------|----------------------------|
| Header | `Derive(` | `DrvWithVersion("xp-dyn-drv",` |
| Input drv outputs | `["out1","out2"]` | `(["out1","out2"],[(nested,[])])` |
| childMap | Always empty | Can be non-empty |
| Protocol min | Any | 1.36+ (self-describing) |
| Experimental feature | None | `DynamicDerivations` |

### Key Takeaways for Python Implementation

1. **Lix 1.35 does not support dynamic derivations** — any derivation with `DrvWithVersion("xp-dyn-drv"` format will fail to parse

2. **The derivation format is self-describing** — the string `"xp-dyn-drv"` identifies the format, independent of protocol version

3. **The `DerivedPathMap<StringSet>::ChildNode` structure** has:
   - `value`: set of output names (strings)
   - `childMap`: `map<OutputName, ChildNode>` for nested dynamic outputs

4. **Recursive structure** — dynamic derivations can have multiple levels of nesting for chains like `drvA.out -> drvB.out -> drvC.out`

5. **Error message from Nix 1.38+** when older daemon can't parse:
   ```
   parsing derivation ... expected string ... Derive([...
   this might be because the daemon is too old to understand dependencies on dynamic derivations
   ```

---

## Key Differences for Python Implementation

1. **Handshake termination:** Lix 1.35 handshake ends after TrustedFlag. Nix 1.38 continues with feature negotiation (string sets).

2. **Feature negotiation:** Nix 1.38 expects to send/receive string sets after basic handshake. Lix 1.35 does not perform this step.

3. **BuildResult fields:** Nix 1.38 guards `cpuUser`/`cpuSystem` at >= 1.37, `timesBuilt` at >= 1.29. Lix 1.35 sends all fields unconditionally without guards.

4. **AddPermRoot (Op 47):** Only exists in Nix 1.38, not in Lix 1.35 (max op is 46).

5. **TrustedFlag wire format:** Lix uses raw uint64 (0/1/2). Nix 1.38 uses optional-wrapped TrustedFlag for protocol >= 1.35.

6. **Error serialization:** Nix 1.38 uses `ErrorInfo` for >= 1.26. Lix 1.35 uses simpler `Error(status, error)` format.

7. **CA derivations:** Lix 1.35 does NOT support CA derivations — `RegisterDrvOutput`/`QueryRealisation` throw `UnimplementedError`. Nix 1.38 supports them with Realisation format (>= 1.31).

8. **NarFromPath (Op 38):** Both Lix 1.35 and Nix 1.38 return raw NAR bytes written directly to `conn.to` via `store->narFromPath(path, conn.to)`. No framing. This is identical in both versions. The `copy_nar_and_reframe` step is needed when forwarding to `AddToStoreNar` since that op's NAR input **does** require framing (for protocol >= 1.23).

9. **Dynamic derivations:** Lix 1.35 cannot parse derivations using `DrvWithVersion("xp-dyn-drv"` format (dynamic output dependencies). Nix 1.36+ supports this through the `DynamicDerivations` experimental feature. The format is self-describing but requires the feature flag to be enabled.

---

## Source Files

### Nix 1.38
- `/home/lillecarl/Code/nix/src/libstore/worker-protocol.hh` — Protocol version, Op enum, magic constants
- `/home/lillecarl/Code/nix/src/libstore/worker-protocol.cc` — Serializers (BuildResult, ValidPathInfo, etc.)
- `/home/lillecarl/Code/nix/src/libstore/worker-protocol-impl.hh` — Container serializers
- `/home/lillecarl/Code/nix/src/libstore/daemon.cc` — Server-side operation handlers
- `/home/lillecarl/Code/nix/src/libstore/remote-store.cc` — Client-side operation handlers
- `/home/lillecarl/Code/nix/src/libstore/build-result.hh` — BuildResult struct
- `/home/lillecarl/Code/nix/src/libstore/path-info.hh` — ValidPathInfo structs
- `/home/lillecarl/Code/nix/src/libstore/derivations.cc` — readDerivation

### Lix 1.35
- `/home/lillecarl/Code/lix/lix/libstore/worker-protocol.hh` — Protocol version 1.35 fixed
- `/home/lillecarl/Code/lix/lix/libstore/worker-protocol.cc` — Serializers
- `/home/lillecarl/Code/lix/lix/libstore/daemon.cc` — Server-side (no CA derivation support)
- `/home/lillecarl/Code/lix/lix/libstore/build-result.hh` — BuildResult struct (no version guards)
