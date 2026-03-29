# NAR Format vs Framed Format

## Raw NAR Format (NarFromPath / dumpPath)

Nix uses NAR (Nix ARchive) format for storing and transferring store paths. Every token is padded to an 8-byte boundary.

### Token Format
```
uint64 length (LE) + length bytes + padding to 8-byte boundary
```

### Example Tokens

Token: `"type"`
- length = 4
- data = "type"
- padding = 4 bytes (to reach 8-byte boundary)
- Total written: 8 bytes length + 4 bytes "type" + 4 bytes padding = 16 bytes

Token: `"("`
- length = 1
- data = "("
- padding = 7 bytes
- Total written: 8 bytes length + 1 byte "(" + 7 bytes padding = 16 bytes

### NAR Structure

```
NAR ::= "(" ")"                                    # directory (empty)
      | "(" "contents" NAR+ ")"                   # directory with entries
      | "(" "type" "regular" "contents" <data> ")"    # regular file
      | "(" "type" "symlink" "target" <target> ")"
```

Each string token goes through SerializingTransform which adds length prefix and padding.

### Nix C++ Implementation

From `libutil/archive.hh`:
```cpp
// encS(s) = encN(len(s)) + s + (padding until next 64-bit boundary)
// encN(n) = 64-bit little-endian encoding of n.
```

From `libutil/serialise.cc`:
```cpp
WireFormatGenerator SerializingTransform::operator()(std::string_view s)
{
    co_yield s.size();
    co_yield Bytes(s.begin(), s.size());
    co_yield SerializingTransform::padding(s.size());
}
```

The NAR format also starts with a magic header: `narVersionMagic1 = "nix-archive-1"`.

---

## Framed Format (AddToStoreNar input)

When sending NAR data TO a store via AddToStoreNar:

```
uint64 chunk_size (LE) + chunk_size bytes (repeated) + uint64 0 (terminator)
```

No padding. Just size-prefixed chunks.

### Example

```
0x40 0x0000000000000001  "("           # chunk of 1 byte: "("
0x40 0x0000000000000003  "foo"        # chunk of 3 bytes: "foo"
...all NAR bytes without padding...
0x08 0x0000000000000000                 # terminator (0 chunk size)
```

---

## The copyNAR Function

From `libutil/archive.cc`:

```cpp
WireFormatGenerator copyNAR(Source & source)
{
    // FIXME: if 'source' is the output of dumpPath() followed by EOF,
    // we should just forward all data directly without parsing.

    auto items = nar::parse(source);

    // we can't use dump() here because we must read the entire nar *before*
    // returning the final `)` tag, otherwise the source will not be emptied
    // before the returned generator is exhausted.
    co_yield narVersionMagic1;
    co_yield "(";
    for (auto && item : items) {
        co_yield std::visit([](auto i) { return dumpSingle(std::move(i)); }, std::move(item));
    }
    co_yield ")";
}
```

The `nar::parse()` function parses the NAR format and yields structured entries.

The `AsyncCopier` (from `archive.cc`) handles async streaming:

```cpp
struct AsyncCopier : AsyncInputStream
{
    AsyncInputStream & source;
    std::vector<char> buffer;
    Parser parser{buffer};

    struct Fragment
    {
        uint64_t pending = 0;
        bool pendingFileContents = false;  // KEY: distinguishes metadata vs contents
    };

    kj::Promise<Result<std::optional<size_t>>> read(void * buffer, size_t size) override
    {
        while (current.pending == 0) {
            // get next fragment from parser
        }

        // For file contents: pass through directly without buffering
        // For metadata: buffer for parser validation
        auto got = TRY_AWAIT(source.read(buffer, size));
        if (!current.pendingFileContents) {
            // buffer metadata for parser
        }
        co_return *got;
    }
};
```

### Key Insight

The AsyncCopier:
1. Parses NAR format (length-prefixed + padded)
2. For **file contents**: returns raw bytes without buffering (no length prefix, no padding)
3. For **metadata tokens**: buffers bytes so the parser can validate structure

This means the output of copyNAR is NOT the same as the input NAR - it strips the metadata structure and passes through file contents directly.

---

## The Problem for Python Implementation

When implementing streaming NAR copy in Python:

1. **NAR is self-delimiting**: Track `(` and `)` depth to know when NAR ends (no size prefix)
2. **Every token is padded**: Must skip padding bytes when reading
3. **Framed format has no padding**: Just size + raw bytes, 0 terminator

The re-framing is complex because:

- NAR tokens have: length prefix + data + padding
- Framed chunks have: size prefix + raw data (no padding)
- We need to strip padding while preserving the byte stream

A simplified re-framer that just buffers and re-emits won't work correctly because padding bytes get mixed with subsequent token data when splitting into chunks.

---

## Nix Copy Implementation

When you run `nix copy --from ssh-ng://builder --to ssh-ng://pynixd@localhost $path`:

1. Source read: `srcStore.narFromPath()` returns AsyncInputStream of NAR data
2. Destination write: `dstStore.addToStore()` accepts the stream

From `libstore/remote-store.cc`:

```cpp
kj::Promise<Result<void>> RemoteStore::addToStore(
    const ValidPathInfo & info,
    AsyncInputStream & source, ...)
{
    auto copier = copyNAR(source);  // Handles re-framing
    conn.sendCommand(
        WorkerProto::Op::AddToStoreNar,
        ...params...,
        [&](AsyncOutputStream & stream) { return copier->drainInto(stream); }
    );
}
```

The daemon side (`libstore/daemon.cc`) uses FramedSource:

```cpp
case WorkerProto::Op::AddToStoreNar: {
    FramedSource source(from);
    AsyncSourceInputStream stream{source};
    aio.blockOn(store->addToStore(info, stream, ...));
}
```

---

## References

- `lix/libutil/archive.hh` - dumpPath documentation with format specification
- `lix/libutil/archive.cc` - copyNAR and AsyncCopier implementation
- `lix/libutil/serialise.cc` - SerializingTransform for padding
- `lix/libstore/daemon.cc` - daemon-side AddToStoreNar handler
- `lix/libstore/remote-store.cc` - RemoteStore::addToStore
