{
  lib,
  buildGoModule,
}:
buildGoModule {
  pname = "tofu-core-schema";
  version = "0-unstable";
  src = ./.;
  vendorHash = "sha256-LHwzM8coXV4Q/9VWZJuftUSz3zsDWYoJUeen5rpQCkU=";
  # buildGoModule names the output binary after the module path's last
  # component (the go.mod `module` directive), not `pname` -- confirmed by
  # the built derivation actually containing bin/nanopynix-tofu-core-schema.
  meta.mainProgram = "nanopynix-tofu-core-schema";
}
