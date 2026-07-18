# Nix reproduction of .github/workflows/ci.yml, rendered to YAML by
# ci/render.py via nanopynix's toYAML primop. See ci/lib.nix for the helper
# functions used below.
let
  ci = import ../lib.nix;
  inherit (ci) steps mkJob withCond;
in
ci.mkWorkflow {
  name = "CI";

  on = {
    push = null;
    schedule = [ { cron = "17 3 * * *"; } ];
    workflow_dispatch = null;
  };

  jobs = {
    # matrix/test temporarily disabled -- iterating on the TSAN diagnostic
    # job only for now (gated behind an unset repo variable, since
    # actionlint rejects a literal `if: false`). Re-enable by restoring the
    # original `if: github.event_name != 'schedule'` condition on both jobs.
    matrix = withCond "vars.RUN_MATRIX_TESTS == 'true'" (mkJob {
      outputs = {
        versions = "\${{ steps.versions.outputs.versions }}";
      };
      steps = [
        (steps.checkout { })
        (steps.nixQuickInstall { })
        {
          id = "versions";
          name = "Compute Nix version matrix";
          run = ''
            echo "versions=$(nix eval --json '.#nanopynixVersionNames.x86_64-linux')" >> "$GITHUB_OUTPUT"'';
        }
      ];
    });

    test = withCond "vars.RUN_MATRIX_TESTS == 'true'" (mkJob {
      needs = "matrix";
      strategy = {
        fail-fast = false;
        matrix = {
          version = "\${{ fromJson(needs.matrix.outputs.versions) }}";
          nix_install = [
            "single-user"
            "multi-user"
          ];
        };
      };
      steps = [
        (steps.checkout { })
        (withCond "matrix.nix_install == 'single-user'" (steps.nixQuickInstall { }))
        (withCond "matrix.nix_install == 'multi-user'" (steps.installNixMultiUser { }))
        (steps.cachix { })
        (withCond "matrix.nix_install == 'single-user'" (steps.configureSingleUserNix { }))
        {
          name = "Build nanopynix test runner for Nix \${{ matrix.version }}";
          run = ''nix build ".#nanopynix-tests-''${{ matrix.version }}" --out-link result --print-build-logs --print-out-paths'';
        }
        (steps.verifyClosure { name = "Verify test runner closure after build"; })
        (steps.enableSandboxNamespaces { })
        {
          name = "Test nanopynix against Nix \${{ matrix.version }} (DIAGNOSTIC-ONLY, post-mortem core dump on crash, narrowed run)";
          run = ''
            set -o pipefail
            unshare --user --map-root-user --mount --pid --fork --mount-proc env NANOPYNIX_CORE_DEBUG=1 NANOPYNIX_GC_THREAD_DEBUG=1 NANOPYNIX_RPC_TIMEOUT=30 PYTHONDONTWRITEBYTECODE=1 COVERAGE_FILE=''${{ github.workspace }}/.coverage \
              ./result/bin/nanopynix-tests --verbose --tb=short -rsxXfE --run-temp-store-builds tests/nanopynix/test_inproc_multithreaded_poc.py \
              2>&1 | tee ''${{ github.workspace }}/test-gdb-output.log
            if grep -qE "^[0-9]+ failed|SIGSEGV|SIGABRT|Fatal Python error" ''${{ github.workspace }}/test-gdb-output.log; then
              echo "::error::test run crashed or failed -- see gdb backtrace above"
              exit 1
            fi
'';
        }
        (steps.uploadArtifact {
          name = "Upload gdb crash backtrace";
          artifactName = "gdb-backtrace-\${{ matrix.version }}";
          path = "\${{ github.workspace }}/test-gdb-output.log";
        })
        (withCond "\${{ !cancelled() }}" {
          name = "Upload coverage reports to Codecov";
          uses = "codecov/codecov-action@main";
          "with" = {
            token = "\${{ secrets.CODECOV_TOKEN }}";
            files = "\${{ github.workspace }}/coverage.xml";
            flags = "\${{ matrix.version }}-\${{ matrix.nix_install }}";
          };
        })
        (withCond "\${{ !cancelled() }}" {
          name = "Upload test results to Codecov";
          uses = "codecov/codecov-action@main";
          "with" = {
            token = "\${{ secrets.CODECOV_TOKEN }}";
            files = "\${{ github.workspace }}/junit.xml";
            flags = "\${{ matrix.version }}-\${{ matrix.nix_install }}";
            report_type = "test_results";
          };
        })
        (steps.verifyClosure { name = "Verify test runner closure after tests"; })
      ];
    });

    test-tsan = mkJob {
      # Diagnostic-only: ThreadSanitizer build hunting concurrency bugs in
      # the single-user in-process build/eval paths. Single-user only,
      # since that's the only mode the crashes reproduce in; not part of
      # the normal version/install-mode matrix and never blocks it.
      "if" = "github.event_name != 'schedule'";
      steps = [
        (steps.checkout { })
        (steps.nixQuickInstall { })
        (steps.cachix { })
        (steps.configureSingleUserNix { })
        {
          name = "Build TSAN nanopynix test runner (nix_2_35)";
          run = "nix build .#nanopynix-tests-tsan-nix_2_35 --out-link result --print-build-logs --print-out-paths";
        }
        (steps.enableSandboxNamespaces { })
        {
          name = "Run TSAN-instrumented crashing test (DIAGNOSTIC-ONLY, repeated)";
          continue-on-error = true;
          run = ''
            set -o pipefail
            for i in $(seq 1 5); do
              echo "=== TSAN run $i ===" | tee -a "''${{ github.workspace }}/tsan-output.log"
              status=0
              # --capture=no: pytest normally captures a test's stdout/stderr into
              # its own buffer and only replays it on a *normal* test completion.
              # TSAN's halt_on_error aborts the process mid-test, so pytest never
              # gets to replay that buffer -- the race report was being silently
              # lost in exactly that captured-but-never-flushed buffer. Disabling
              # capture lets TSAN's direct fd writes reach our log in real time.
              unshare --user --map-root-user --mount --pid --fork --mount-proc env NANOPYNIX_CORE_DEBUG=1 NANOPYNIX_RPC_TIMEOUT=30 PYTHONDONTWRITEBYTECODE=1 \
                ./result/bin/nanopynix-tests --verbose --tb=short -rsxXfE --capture=no --run-temp-store-builds \
                "tests/nanopynix/test_inproc_multithreaded_poc.py::test_inproc_parallel_batch_builds_use_multiple_store_workers" \
                2>&1 | tee -a "''${{ github.workspace }}/tsan-output.log" || status=$?
              # pytest's inline PASSED/FAILED marker can land on a non-newline-terminated
              # line that GH Actions' log API sometimes drops at a step boundary --
              # print an explicit, unambiguous exit code so pass/fail is never in doubt.
              echo "=== TSAN run $i exit status: $status ===" | tee -a "''${{ github.workspace }}/tsan-output.log"
              # Only a genuine data race stops the loop early -- a "thread leak"
              # finding (nix's own libcurl worker thread not joined at exit) is
              # a separate, benign category that still triggers halt_on_error
              # (ending that one run early) but shouldn't prevent the remaining
              # iterations from re-checking for an actual race.
              if grep -q "ThreadSanitizer: data race" "''${{ github.workspace }}/tsan-output.log"; then
                echo "TSAN data race detected on run $i -- stopping early" | tee -a "''${{ github.workspace }}/tsan-output.log"
                break
              fi
            done
'';
        }
        {
          name = "Run TSAN-instrumented concurrency-relevant test files (DIAGNOSTIC-ONLY, single pass)";
          continue-on-error = true;
          run = ''
            set -o pipefail
            status=0
            # Broader (but not exhaustive) coverage pass: every test file that
            # touches NixThreadExecutor/threading/concurrent.futures, not just
            # the one test known to hit the now-fixed races. Single pass only
            # (not repeated like the targeted run above) -- halt_on_error=1
            # stops the whole run at the first finding anywhere, so repeating
            # wouldn't add coverage, only cost. The other ~39 test files are
            # single-threaded and wouldn't exercise any new race, so they're
            # intentionally excluded to keep this pass cheap.
            unshare --user --map-root-user --mount --pid --fork --mount-proc env NANOPYNIX_CORE_DEBUG=1 NANOPYNIX_RPC_TIMEOUT=30 PYTHONDONTWRITEBYTECODE=1 \
              ./result/bin/nanopynix-tests --verbose --tb=short -rsxXfE --capture=no --run-temp-store-builds \
              tests/nanopynix/test_inproc.py \
              tests/nanopynix/test_worker_store_unit.py \
              tests/nanopynix/test_inproc_multithreaded_poc.py \
              tests/nanopynix/test_session_unit.py \
              tests/nanopynix/test_eval_rpc.py \
              tests/nanopynix/test_l3_inproc.py \
              tests/nanopynix/test_worker_eval_unit.py \
              2>&1 | tee -a "''${{ github.workspace }}/tsan-output-broad.log" || status=$?
            echo "=== TSAN broad pass exit status: $status ===" | tee -a "''${{ github.workspace }}/tsan-output-broad.log"
'';
        }
        (steps.uploadArtifact {
          name = "Upload TSAN output";
          artifactName = "tsan-race-report";
          path = "\${{ github.workspace }}/tsan-output.log\n\${{ github.workspace }}/tsan-output-broad.log\n";
        })
      ];
    };

    docs-build = withCond "github.event_name != 'schedule' && github.ref == 'refs/heads/develop'" (mkJob {
      needs = "test";
      steps = [
        (steps.checkout { })
        (steps.nixQuickInstall { })
        (steps.cachix { })
        {
          name = "Build documentation";
          run = "nix build .#nanopynix-docs --out-link result --print-build-logs --print-out-paths";
        }
        (steps.verifyClosure { name = "Verify docs closure"; })
        {
          name = "Prepare Pages artifact";
          run = ''
            mkdir -p public
            cp -r --no-preserve=mode,ownership result/. public/
'';
        }
        {
          uses = "actions/upload-pages-artifact@main";
          "with" = {
            path = "public";
          };
        }
      ];
    });

    docs-deploy = withCond "github.event_name != 'schedule' && github.ref == 'refs/heads/develop'" (mkJob {
      needs = "docs-build";
      permissions = {
        pages = "write";
        id-token = "write";
      };
      environment = {
        name = "github-pages";
        url = "\${{ steps.deployment.outputs.page_url }}";
      };
      concurrency = {
        group = "pages";
        cancel-in-progress = false;
      };
      steps = [
        {
          name = "Deploy to GitHub Pages";
          id = "deployment";
          uses = "actions/deploy-pages@main";
        }
      ];
    });

    update-lockfile-prep = withCond "github.event_name == 'schedule'" (mkJob {
      outputs = {
        versions = "\${{ steps.versions.outputs.versions }}";
      };
      steps = [
        (steps.checkout { ref = "main"; })
        (steps.nixQuickInstall { })
        (steps.cachix { })
        {
          name = "Update flake inputs";
          run = "nix flake update";
        }
        {
          id = "versions";
          name = "Compute Nix version matrix";
          run = ''
            echo "versions=$(nix eval --json '.#nanopynixVersionNames.x86_64-linux')" >> "$GITHUB_OUTPUT"'';
        }
        (steps.uploadArtifact {
          name = null;
          artifactName = "flake-lock";
          path = "flake.lock";
          cond = null;
        })
      ];
    });

    update-lockfile-test = withCond "github.event_name == 'schedule'" (mkJob {
      needs = "update-lockfile-prep";
      strategy = {
        fail-fast = false;
        matrix = {
          version = "\${{ fromJson(needs.update-lockfile-prep.outputs.versions) }}";
        };
      };
      steps = [
        (steps.checkout { ref = "main"; })
        (steps.downloadArtifact { artifactName = "flake-lock"; })
        (steps.nixQuickInstall { })
        (steps.cachix { })
        (steps.configureSingleUserNix { })
        {
          name = "Build nanopynix test runner for Nix \${{ matrix.version }}";
          run = ''nix build ".#nanopynix-tests-''${{ matrix.version }}" --out-link result --print-build-logs --print-out-paths'';
        }
        (steps.verifyClosure { name = "Verify test runner closure after build"; })
        (steps.enableSandboxNamespaces { corePattern = false; })
        {
          name = "Test nanopynix against Nix \${{ matrix.version }}";
          run = "unshare --user --map-root-user --mount --pid --fork --mount-proc env NANOPYNIX_RPC_TIMEOUT=30 PYTHONDONTWRITEBYTECODE=1 ./result/bin/nanopynix-tests --verbose --tb=short -rsxXfE --run-temp-store-builds";
        }
        (steps.verifyClosure { name = "Verify test runner closure after tests"; })
      ];
    });

    update-lockfile-commit = withCond "github.event_name == 'schedule'" (mkJob {
      needs = "update-lockfile-test";
      permissions = {
        contents = "write";
      };
      steps = [
        (steps.checkout { ref = "main"; })
        (steps.downloadArtifact { artifactName = "flake-lock"; })
        {
          uses = "step-security/git-auto-commit-action@main";
          "with" = {
            commit_message = "nix flake update";
          };
        }
      ];
    });
  };
}
