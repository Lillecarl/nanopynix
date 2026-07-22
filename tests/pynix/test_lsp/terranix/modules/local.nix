# pynix-lsp: terranixEntry = import ../default.nix { }
#
# local_file's `filename` is written relative to whatever directory `tofu`
# was invoked in -- not the (read-only, -chdir'd) module directory in the
# store -- via Terraform's own `path.cwd`, which tracks the pre-`-chdir`
# working directory precisely for this reason. See ../default.nix's `tofu`
# wrapper for the other half: it sets $TF_DATA_DIR from that same directory.
#
# LSPOINT/LSSTART/LSEND/LSLINE comments below are scenario markers for
# tests/pynix/test_lsp_scenarios.py (see tests/support/lsp_markers.py) --
# not part of the terranix config itself.
{ lib, ... }:
{
  resource.local_file.greeting = {
    filename = "\${path.cwd}/terranix-demo-output/greeting-\${random_id.suffix.hex}.txt";
#                LSPOINT1v"r"
    content = lib.tfRef "random_password.example.result";
#                                         LSSTART2v"r"  vLSEND2
    content2 = lib.tfRef "random_password.example.result";
    # LSPOINT4/LSPOINT5 exercise `count`, a core (not provider) meta-argument.
#                                              vLSPOINT4"o"
    content3 = lib.tfRef "local_file.greeting.count";
#                                              vLSPOINT5
    content4 = lib.tfRef "local_file.greeting.c";
  };

  # LSLINE3

  output.greeting_path.value = lib.tfRef "local_file.greeting.filename";
}
