# The smallest thing that proves the mechanism: Python writes a derivation,
# and Nix builds it.
{
  bash,
  coreutils,
  ddrn,
}:

ddrn.mkPlanner {
  name = "hello-from-python";
  plan = ./plan.py;
  tools = { inherit bash coreutils; };
  env.MESSAGE = "This derivation was written by Python, inside a build.";
}
