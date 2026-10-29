#!/usr/bin/env python3
"""Build the tessellation shaders with Xcode 27's Metal compiler too.

dxmt/src/airconv/shaders/air_tessellation.metal calls the Metal builtin
__metal_atomic_fetch_add_explicit directly with four arguments. The Metal
toolchain of Xcode 27 (Metal 32023) adds a fifth, the memory flags its own
metal_atomic header passes as __METAL_MEMORY_FLAGS_NONE__, and stops with
"too few arguments to function call, expected 5, have 4" (local build on
Xcode 27.1, 2026-10-29). That toolchain predefines __METAL_MEMORY_FLAGS_NONE__;
older ones do not, so the call keeps four arguments there and the shader built
with Xcode 26 is unchanged.

Idempotent; fails by name if the anchor moves. Run from the repository root.
"""
import pathlib
import sys

PATH = pathlib.Path("dxmt/src/airconv/shaders/air_tessellation.metal")
MARKER = "madeira-bcd: Xcode 27 Metal atomic flags"
OLD = ("  return __metal_atomic_fetch_add_explicit(out_count, 1, int(memory_order_relaxed), "
       "__METAL_MEMORY_SCOPE_THREADGROUP__);\n")
NEW = ("#ifdef __METAL_MEMORY_FLAGS_NONE__\n"
       "  /* " + MARKER + ": the builtin takes the memory flags too */\n"
       "  return __metal_atomic_fetch_add_explicit(out_count, 1, int(memory_order_relaxed), "
       "__METAL_MEMORY_SCOPE_THREADGROUP__, __METAL_MEMORY_FLAGS_NONE__);\n"
       "#else\n" + OLD + "#endif\n")

s = PATH.read_text()
if MARKER in s:
    print(f"{PATH}: already patched")
    sys.exit(0)
if s.count(OLD) != 1:
    sys.exit(f"{PATH}: the four-argument __metal_atomic_fetch_add_explicit call moved; patch not applied")
PATH.write_text(s.replace(OLD, NEW))
print(f"{PATH}: patched (memory flags argument under Xcode 27's Metal)")
