"""A Nix value pointer that keeps its discriminator bits is refused.

`nix::ValueStorage` packs a 3-bit discriminator into the low bits of the first
word of a value, so the type is `alignas(16)` and every real `nix::Value *` has
those bits clear. A pointer that keeps them is the *second* word of some other
value, reached through a block that the collector gave away and that now holds
something else. That is issue #70, and
`docs/collector-and-threads.md` carries the core dump it came from.

Without the check the defect reaches `nix::ExprVar::eval` and dies there on a
`movdqa` that wants 16-byte alignment. The instruction gives no way back to the
operation that produced the pointer. These tests hold the check that turns it
into a named failure at the boundary instead.

`_check_value_alignment` takes a raw address because no ordinary caller can
build a misaligned value. That is what the check is for, and it is also what
makes the check untestable from the public API.
"""

from __future__ import annotations

import pytest
from nanopynix_bindings import expr as nanopynix_expr


# 16 on every build this repository supports. The test reads it from the
# message rather than assuming it, so a build whose `Value` is not bit-packed
# still checks whatever that build really needs.
def _alignment_from_message(message: str) -> int:
    for word in message.replace(",", " ").split():
        if word.isdigit() and int(word) > 1:
            return int(word)
    pytest.fail(f"the message names no alignment: {message}")


def test_an_aligned_address_is_accepted() -> None:
    # A multiple of any plausible alignment, and not null.
    nanopynix_expr._check_value_alignment(0x1000)


def test_a_null_address_is_accepted() -> None:
    # `checkedValue` reports a released value with its own message, and the
    # wrapper for a null value is a separate concern. The check must not take
    # that case over.
    nanopynix_expr._check_value_alignment(0)


@pytest.mark.parametrize("offset", [1, 2, 4, 7, 8, 15])
def test_an_address_that_keeps_low_bits_is_refused(offset: int) -> None:
    with pytest.raises(RuntimeError) as caught:
        nanopynix_expr._check_value_alignment(0x1000 + offset)

    message = str(caught.value)
    alignment = _alignment_from_message(message)
    if offset % alignment == 0:
        pytest.skip(f"offset {offset} is aligned for this build")
    # The message has to name the defect, because the reader of a rare CI
    # failure has this line and nothing else.
    assert "#70" in message
    assert "not" in message
    assert "aligned" in message


def test_the_message_names_the_offset() -> None:
    with pytest.raises(RuntimeError) as caught:
        nanopynix_expr._check_value_alignment(0x1002)

    # 2 is the offset the core dump of #70 recorded: the faulting pointer was
    # `0x713dd60fb012`, which is the second word of the value at
    # `0x713dd60fb010` with the discriminator still on it.
    assert " 2 past " in str(caught.value)
