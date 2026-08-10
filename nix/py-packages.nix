# This repo's own Python projects, as a pyproject.nix builders overlay.
#
# One definition per project, rendered two ways -- built, and editable for the
# dev shell -- and used as each other's dependencies in both. The two overlays
# below differ only in which renderer they call, so a build input added for
# one is present in the other and they cannot drift.
{
  lib,
  callPackage,
  runCommand,
  ps,
  python,
  root,
  # The linked Nix version, appended as a PEP 440 local segment to every
  # project here that reaches nanopynix-bindings -- see `nixLinked` below.
  version,
}:

let
  protoSrc = callPackage (root + "/nanopynix-proto/generated.nix") { inherit python; };
  greeterSrc = callPackage (root + "/greeter-proto/generated.nix") { inherit python; };

  # Every project that reaches nanopynix-bindings -- directly, or through
  # `nanopynix` -- is built against one specific Nix version's C++ components
  # and is not interchangeable with the same source built against another, so
  # the version says which one. See nanopynix-bindings/package.nix for why
  # this is a PEP 440 local segment (`+nix...`) and not a plain suffix.
  #
  # nanopynix-proto, greeter-proto, grpclib-transports and pytest-agent
  # deliberately don't get it: none of them reaches the bindings, so each
  # really is the same package whatever Nix is linked, and suffixing them
  # would rebuild them once per version for nothing.
  nixLinked = rendered: { version = "${rendered.version}+nix${version}"; };

  # Applied to both the built and the editable form of a project, so a build
  # input added for one is present in the other.
  extraFor = {
    nanopynix-proto = pySelf: rendered: {
      # Built from the generated tree rather than the checkout: this
      # project's entire `src/` comes out of protoc, and generated.nix
      # emits a complete project (pyproject.toml included) precisely so
      # this can be an ordinary `src` and not a codegen step wedged into
      # someone else's build. `projectRoot` still points at the checkout,
      # which is where the pyproject.toml that Nix *reads* at eval time
      # lives -- generated.nix copies that same file into `$out`, so the
      # metadata and the build cannot disagree.
      src = protoSrc;

      # The builders have no `pythonImportsCheck` equivalent, and this is
      # the project that most needs one: its entire contents come from a
      # code generator, so a `.proto` renamed or dropped from
      # generated.nix's file list produces a package that installs
      # perfectly and is missing a module. Checking through a venv (rather
      # than `$out` directly) is what makes the imports resolvable at all,
      # since a builders package propagates nothing.
      passthru = rendered.passthru // {
        tests.imports =
          runCommand "nanopynix-proto-imports"
            {
              nativeBuildInputs = [
                (pySelf.mkVirtualEnv "nanopynix-proto-check-env" { nanopynix-proto = [ ]; })
              ];
            }
            ''
              python -c 'import nanopynix_proto, nanopynix_proto.nix.common, nanopynix_proto.google.rpc'
              touch "$out"
            '';
      };

      meta = rendered.meta // {
        license = lib.licenses.lgpl21Plus;
        platforms = lib.platforms.unix;
      };
    };

    # The same three moves as nanopynix-proto above, for the same three
    # reasons -- generated `src`, an imports check the builders otherwise
    # cannot express, and `meta`. Read that entry for the argument.
    greeter-proto = pySelf: rendered: {
      src = greeterSrc;

      passthru = rendered.passthru // {
        tests.imports =
          runCommand "greeter-proto-imports"
            {
              nativeBuildInputs = [
                (pySelf.mkVirtualEnv "greeter-proto-check-env" { greeter-proto = [ ]; })
              ];
            }
            ''
              python -c 'import greeter.greeter.common, greeter.greeter.server, greeter.greeter.worker'
              touch "$out"
            '';
      };

      meta = rendered.meta // {
        license = lib.licenses.mit;
        platforms = lib.platforms.unix;
      };
    };

    grpclib-transports = _pySelf: rendered: {
      meta = rendered.meta // {
        license = lib.licenses.mit;
        platforms = lib.platforms.unix;
      };
    };

    nanopynix =
      _pySelf: rendered:
      nixLinked rendered
      // {
        # No LD_PRELOAD of the TSAN runtime here, deliberately. It used to be
        # set, reasoning that the bindings get dlopen()ed into whatever process
        # imports nanopynix -- but a derivation's `env` is its *build*
        # environment and reaches no consumer, so it never served that purpose.
        # What it did do was preload libtsan into every process this build runs,
        # `uv` included, and TSAN then reported data races inside uv's own Rust
        # code and failed the build with exit 66 -- before a single test ran.
        # That is the whole reason all three test-tsan-* jobs were red.
        #
        # Nothing here needs it: this is the pure-Python distribution, the
        # builders have no `pythonImportsCheck` equivalent (see the comment on
        # nanopynix-proto above), so no phase of this build ever imports
        # nanopynix and nothing dlopens the instrumented .so.
        #
        # The two places that genuinely need the preload still have it, both
        # scoped to a process that really does load the extension:
        # nanopynix-bindings/package.nix (stubgen + pythonImportsCheck) and
        # nanopynix/tests.nix's runner script (the pytest process itself).
        meta = rendered.meta // {
          license = lib.licenses.lgpl21Plus;
          platforms = lib.platforms.unix;
        };
      };

    nanopynix-helpers =
      _pySelf: rendered:
      nixLinked rendered
      // {
        meta = rendered.meta // {
          platforms = lib.platforms.unix;
        };
      };

    pynix =
      _pySelf: rendered:
      nixLinked rendered
      // {
        meta = rendered.meta // {
          platforms = lib.platforms.unix;
        };
      };

    pytest-agent = _pySelf: rendered: {
      meta = rendered.meta // {
        platforms = lib.platforms.unix;
      };
    };
  };

  # Per-project options. No `extras` here on purpose: the renderer's `extras`
  # argument only feeds PEP-508 marker evaluation -- `optional-dependencies`
  # is populated from *every* declared extra regardless. Which extras are
  # actually installed is chosen per environment, in the `mkVirtualEnv` spec,
  # which is where it belongs: the same `pynix` is a release application
  # without its test extra and a dev install with it.
  projects = {
    # `editable = false`: this project has no working-tree source to point an
    # editable install at -- every module is generated (generated.nix), so
    # `src/` does not exist in the checkout. Editing it means editing a
    # `.proto` and rebuilding, which is what the built form already does.
    nanopynix-proto = {
      editable = false;
    };
    # `editable = false` for the same reason as nanopynix-proto: there is no
    # `src/` in the checkout to point an editable install at. Every module
    # comes out of protoc (greeter-proto/generated.nix).
    greeter-proto = {
      editable = false;
    };
    # Vendored, and a first-class project of this repo rather than a
    # third-party dependency -- nanopynix is its only consumer, and it is
    # meant to be changed for that consumer. Editable like the rest, so a
    # change to a transport is live in the dev shell without a rebuild.
    grpclib-transports = { };
    nanopynix = { };
    nanopynix-helpers = { };
    pynix = { };
    # In the set so the dev shell can install it editable -- pytest-agent is
    # developed here alongside everything else. Deliberately *not* in the
    # test runner's venv: it auto-activates on import and would start writing
    # `.pytest-agent/` run directories from CI.
    pytest-agent = { };
  };
in
{
  # The project directories, for `nixpkgsRootsFor`. Derived from `projects`
  # rather than written out again, so adding a project is one edit.
  projectRoots = lib.mapAttrsToList (name: _: root + "/${name}") projects;

  # The overlay: every project built normally.
  built =
    pySelf: _pyPrev:
    lib.mapAttrs (
      name: _opts:
      pySelf.callPackage (ps.mkProject {
        projectRoot = root + "/${name}";
        inherit python;
        extra = extraFor.${name} pySelf;
      }) { }
    ) projects;

  /*
    The same projects as editable installs, for the dev shell and the test
    runner. Layered *over* `built` so that anything not listed keeps its
    built form, and so a project's dependencies resolve to the editable copy
    of its siblings rather than a store copy of them. A project marked
    `editable = false` is exactly that case: it stays as `built` supplied it.
  */
  editable =
    pySelf: _pyPrev:
    lib.mapAttrs (
      name: _opts:
      pySelf.callPackage (ps.mkEditableProject {
        projectRoot = root + "/${name}";
        # The *project* root, not its `src/` -- `mkDerivationEditable` reads
        # the wheel's own package layout out of pyproject.toml and appends
        # the subdirectory itself. Passing `src/` produced `.../src/src` path
        # hooks that silently pointed nowhere; nothing failed, because
        # pytest.ini's `pythonpath` was quietly supplying the source instead.
        #
        # `toString`, not a path value: a path would be copied into the store
        # and the install would stop being live. That also makes this set
        # local-checkout-only -- under flake evaluation `root` *is* a store
        # path and the renderer rejects it outright -- which is why only the
        # dev shell uses it. See nanopynix/tests.nix.
        root = toString (root + "/${name}");
        inherit python;
        extra = extraFor.${name} pySelf;
      }) { }
    ) (lib.filterAttrs (_: opts: opts.editable or true) projects);
}
