let
# vLSPOINTGDEF"g"
  greeting = "hello";
  first = greeting + " there";
#          vLSPOINTGREF"g"
  second = greeting + " again";
  shadowed =
    let
#     vLSPOINTSHADOWDEF"g"
      greeting = "shadowed";
    in
    greeting;
in
{
  outer = greeting;
  keepShadowed = shadowed;
  withFormals =
#     vLSPOINTADEF"a"
    { a, b ? a + 1 }:
#   vLSPOINTAREF"a"
    a + b;
}
