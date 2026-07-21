{ lib, ... }:
{
  resource.random_id.suffix.byte_length = 4;

  resource.random_password.example = {
    length = 20;
    special = true;
  };

  output.suffix_hex.value = lib.tfRef "random_id.suffix.hex";
}
