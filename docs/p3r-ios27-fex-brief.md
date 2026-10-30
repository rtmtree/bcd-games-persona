# P3R on iOS 27: what FEX needs (for a human to write)

FEX/CLAUDE.md forbids AI-written FEX code, so this note only describes the two FEX changes. The BCD-side work is already in `build/ntdll-unix`.

## 1. A store that rewrites the rest of its own block (the crash)

**What happens.** P3R's protection decrypts the next instruction in place. At 0x151C67190, `xor dword [rip+0], 0x5bba25fd` turns `6d b5 b5 f9` (insd) into `90 90 0f a2` (nop nop cpuid) at 0x151C6719A.

**The failure.** With `FEX_MAXINST >= 2`, the xor and the instruction it rewrites can share one translated block. The SMC write fault is serviced (HandleRWXAccessViolation invalidates, and Module.cpp retries the store at the same Pc). The block then carries on into its stale translation of the next instruction: insd, then #GP, then c0000096.

**Proof.** A crash-time dump (process_ios.c `[guest-insn] CURRENT`) shows plaintext in memory at 0x151C6719A while the executed instruction was insd.

**Why it only shows on iOS 27.** Where blocks start depends on how execution enters this code. The iOS 26 phone happened to enter so that the xor ended its block.

**What's needed.** After an SMC write fault is serviced, if the written range overlaps the guest bytes of the block that is currently running, past the faulting instruction, FEX must not resume in that block. It should either:
- reconstruct state at the next guest RIP and return to the dispatcher, the same path as the SuspendTrapMagic / ReconstructThreadState handling in ARM64EC/Module.cpp; or
- end blocks after any store whose rip-relative target lands inside the block (a decode-time check).

**Today's workaround** is `env.FEX_MAXINST = 1`. It works, but it costs speed and memory, as described in section 2.

## 2. Heap growth that follows recompiles (what ends the session)

**What happens.** With MAXINST=1 the code buffer (128 MB, the branch-range cap) fills about every 15 s of new code. Every rotation recompiles the working set: about 60M compiles in 25 min, an 18–39 % hit rate.

**The growth.**
- FEX's rpmalloc spans in the arena [0x7c00000000, 0x8000000000) step up with every rotation burst: live spans went from 23 to 244, about 4 GB dirty.
- They stay flat when the game stops compiling (5.3 GB held for 8 minutes at a dialogue).
- The lookup caches are only 164 MB (L1-only).
- "Failed to mprotect last page of code buffer" was logged 768 times.

**The next step.** Find which per-compile allocations survive a code-buffer clear (block lists or code-page maps, IR pools), or trim rpmalloc's thread caches after a clear. Fixing section 1 lets MAXINST go back up, which by itself cuts compiles and growth several-fold.

## BCD-side settings in use on the test iPhone (00008150-000828423CB8401C)

- **The P3R game config:** `env.FEX_MAXINST = 1`, `env.MADEIRA_POOL_HEAD_RESERVE_MB = 64`, `exe-window-mb = 368`, `pool-split = 1`, `env.MADEIRA_X64_IMAGE_NOCOPY = 1`.
- **bcd.cfg:** `mempool-mb = 5632`, `env.MADEIRA_POOL_FEX = 2` (new), `env.MADEIRA_POOL_MARGIN_MB = 640`, `env.MADEIRA_POOL_MIN_KB = 1024`, `swap-mb = 8192`, broad coverage.
