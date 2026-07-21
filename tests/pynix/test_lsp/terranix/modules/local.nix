# local_file's `filename` is written relative to whatever directory `tofu`
# was invoked in -- not the (read-only, -chdir'd) module directory in the
# store -- via Terraform's own `path.cwd`, which tracks the pre-`-chdir`
# working directory precisely for this reason. See ../default.nix's `tofu`
# wrapper for the other half: it sets $TF_DATA_DIR from that same directory.
{ lib, ... }:
{
  resource.local_file.greeting = {
    filename = "\${path.cwd}/terranix-demo-output/greeting-\${random_id.suffix.hex}.txt";
    content = lib.tfRef "random_password.example.result";
  };

  output.greeting_path.value = lib.tfRef "local_file.greeting.filename";
}
