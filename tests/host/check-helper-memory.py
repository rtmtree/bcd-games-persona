#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 thebcd69 (BCD)
"""Helper memory (bcd.cfg mempool-mb, the ml1150 pool in virtual_ios.c) reaches more of a game's memory (madeira-bcd, 2026-10-29).

The pool maps guest data from memory entries the MadeiraMemoryHost extension created and left behind when it exited: memory owned
by an exited task is billed to no process (MemoryHostTest.m runs 2-6), the trick MeloNX ships as its "RAM limit bypass". Four
limits kept it small:
  - a range had to fit inside one 512 MB region, so broad's whole reservations and big heaps went to the file;
  - in broad, once the swap file's disk reached swap-mb every new range was refused before the pool was asked;
  - a decommit inside a pool-backed reservation released the range to anonymous memory, billed to BCD from then on;
  - freed ranges were never merged.
And helper memory needed swap-mb. On a device (Persona 3 Reload, build 622) its guard then refused every take: it compared the
free pages alone with its 1 GB margin, and BCD's own limit warning reached it as the system's CRITICAL pressure, which never
cleared. Broad also left every reservation under its 4 MB floor anonymous, most of that game's memory.

This compiles the production swap-tier core (between the "swap-tier core" markers) on macOS with real memory entries
(mach_make_memory_entry_64, as the extension makes them) and a real sparse file, and checks: a reservation larger than a region is
mapped from the pool piece by piece, in pool order (the data is where the regions' windows see it); a decommit inside it reads zero
and stays pool memory (a write lands in the region again) and the data beside it is intact; with the file's disk past swap-mb a
new reservation still comes from the pool and none is refused; once the pool is used up the disk cap refuses as before; a pool
map that fails part-way leaves no piece of the pool mapped (anonymous memory instead) and returns the space; released space reads
zero when it is handed out again; and a randomized take/give run against a model keeps live ranges apart, merges free ranges and
ends with the pool empty (bump pointer 0, no free entries); the guard lets a take through on free + file-backed pages, closes
below the margin or at the system's critical level (not its warning) and logs both ways; with MADEIRA_POOL_MIN_KB set (opt-in), a reservation between 1 and
4 MB is taken whole from the pool, or stays anonymous when the guard says no, never reaching the file; the margin and floor are read
when the tier starts. Source checks cover the app's start, the pressure source and the Settings rows.
Device runs are still required: this proves the bookkeeping and the mappings, not iOS's footprint ledger.
"""
from pathlib import Path
import re
import subprocess
import sys
import tempfile

root = Path(__file__).resolve().parents[2]
virt = (root / 'build/ntdll-unix/virtual_ios.c').read_text()
failures = []


def check(cond, what):
    print(('ok   ' if cond else 'FAIL ') + what)
    if not cond:
        failures.append(what)


def body_of(src, signature):
    start = src.find(signature)
    while start >= 0:
        semi, brace = src.find(';', start), src.find('{', start)
        if brace != -1 and (semi == -1 or brace < semi):
            break
        start = src.find(signature, start + 1)
    if start < 0:
        raise ValueError(signature)
    brace = src.index('{', start)
    depth = 0
    for i in range(brace, len(src)):
        if src[i] == '{':
            depth += 1
        elif src[i] == '}':
            depth -= 1
            if depth == 0:
                return src[brace:i + 1]
    raise AssertionError('unterminated body: ' + signature)


core = virt[virt.index('/* swap-tier core begin'):virt.index('/* swap-tier core end */')]

prelude = r'''
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <errno.h>
#include <unistd.h>
#include <time.h>
#include <fcntl.h>
#include <assert.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <mach/mach.h>
typedef unsigned long ULONG_PTR;
#define VPROT_READ       0x01
#define VPROT_WRITE      0x02
#define VPROT_EXEC       0x04
#define VPROT_WRITECOPY  0x08
#define VPROT_GUARD      0x10
#define VPROT_COMMITTED  0x20
#define VPROT_WRITEWATCH 0x40
#define VPROT_ARM64EC          0x0100
#define VPROT_SYSTEM           0x0200
#define VPROT_PLACEHOLDER      0x0400
#define VPROT_FREE_PLACEHOLDER 0x0800
#define SEC_FILE    0x00800000
#define SEC_IMAGE   0x01000000
#define SEC_RESERVE 0x04000000
#define SEC_COMMIT  0x08000000
struct file_view { void *base; size_t size; unsigned int protect; };
static inline int is_view_valloc( const struct file_view *view )
{ return !(view->protect & (SEC_FILE | SEC_RESERVE | SEC_COMMIT)); }
static uintptr_t host_page_mask = 0x3fff;
ULONG_PTR ios_fex_arena_base_unix = 0x7c00000000ULL, ios_fex_arena_end_unix = 0x8000000000ULL;
void *ios_jit_rw_base_global = (void *)0x7000000000ULL;
void *ios_jit_rx_base_global = (void *)0x119400000ULL;
size_t ios_jit_pool_size_global = 0x20000000;
uintptr_t ios_jit_low_rx_global, ios_jit_low_rw_global;
size_t ios_jit_low_size_global;
static size_t jit_pool_offset, ios_jit_tail_reserved;
static int ios_jit_low_overlaps( uintptr_t a, size_t size )
''' + body_of(virt, 'static int ios_jit_low_overlaps(') + r'''
static int get_unix_prot( unsigned char v )
{
    int p = 0;
    if ((v & VPROT_COMMITTED) && !(v & VPROT_GUARD))
    {
        if (v & VPROT_READ) p |= PROT_READ;
        if (v & VPROT_WRITE) p |= PROT_READ | PROT_WRITE;
        if (v & VPROT_EXEC) p |= PROT_READ;
    }
    return p;
}
static unsigned char get_host_page_vprot( const void *addr ) { (void)addr; return VPROT_READ | VPROT_WRITE | VPROT_COMMITTED; }
static int mprotect_range( void *base, size_t size, unsigned char set, unsigned char clear )
{
    char *a = (char *)((uintptr_t)base & ~host_page_mask);
    char *e = (char *)(((uintptr_t)base + size + host_page_mask) & ~host_page_mask);
    for (; a < e; a += host_page_mask + 1)
        if (mprotect( a, host_page_mask + 1, get_unix_prot( (get_host_page_vprot( a ) & ~clear) | set ) )) return -1;
    return 0;
}
static void *anon_mmap_fixed( void *a, size_t l, int prot, int flags )
{
    (void)flags;
    assert( !((uintptr_t)a & host_page_mask) );
    assert( !(l & host_page_mask) );
    return mmap( a, l, prot, MAP_PRIVATE | MAP_ANON | MAP_FIXED, -1, 0 );
}
'''

harness = r'''
static unsigned long long ios_swap_footprint_mb( void ) { return 4321; }
static void ios_swap_cfg( int *mode, int *min_mb ) { *mode = 2; *min_mb = 0; }
static unsigned long long ios_swap_disk_used( void )
{
    struct stat st;
    if (ios_swap_fd < 0 || fstat( ios_swap_fd, &st )) return 0;
    return (unsigned long long)st.st_blocks * 512;
}
static int bad;
#define CHECK(c, what) do { if (!(c)) { printf("FAIL: %s (line %d)\n", what, __LINE__); bad++; } else printf("ok: %s\n", what); } while (0)
#define MB (1ull << 20)
#define REGION (32 * MB)
#define NREG 4

static char *region( uintptr_t at, size_t len )
{
    void *p = mmap( (void *)at, len, PROT_NONE, MAP_PRIVATE | MAP_ANON | MAP_FIXED, -1, 0 );
    if (p == MAP_FAILED || p != (void *)at) { printf( "FAIL: cannot map a test region at %p (errno %d)\n", (void *)at, errno ); exit( 2 ); }
    return p;
}
static void commit( char *p, size_t len, struct file_view *v )
{
    mprotect( p, len, PROT_READ | PROT_WRITE );
    ios_swap_commit( p, len, VPROT_READ | VPROT_WRITE, v );
}
static int all( const char *p, size_t len, char c ) { size_t i; for (i = 0; i < len; i += 4096) if (p[i] != c) return 0; return 1; }
/* the byte pool offset off holds, seen through its region's window */
static char *pool_at( uint64_t off ) { return ios_pool_reg[off / REGION].window + off % REGION; }
static int ext_of( const void *va )
{
    unsigned i;
    for (i = 0; i < ios_swap_n; i++)
        if (ios_swap_ext[i].va <= (const char *)va && (const char *)va < ios_swap_ext[i].va + ios_swap_ext[i].len) return (int)i;
    return -1;
}
static int is_pool( const void *va ) { int i = ext_of( va ); return i >= 0 && (ios_swap_ext[i].off & IOS_POOL_FLAG); }

/* randomized take/give against a model: live ranges never overlap each other or a free range, free ranges never touch (they
 * are merged) and nothing free reaches the bump pointer; after everything is given back the pool is empty */
static uint64_t rng = 0x9e3779b97f4a7c15ull;
static uint64_t next( void ) { rng ^= rng << 13; rng ^= rng >> 7; rng ^= rng << 17; return rng; }
static int model_ok( uint64_t *loff, uint64_t *llen, int n )
{
    int a, b;
    for (a = 0; a < n; a++)
    {
        if (loff[a] + llen[a] > ios_pool_bump) return 0;
        for (b = a + 1; b < n; b++) if (loff[a] < loff[b] + llen[b] && loff[b] < loff[a] + llen[a]) return 0;
        for (b = 0; b < (int)ios_pool_nfree; b++)
            if (loff[a] < ios_pool_free[b].off + ios_pool_free[b].len && ios_pool_free[b].off < loff[a] + llen[a]) return 0;
    }
    for (a = 0; a < (int)ios_pool_nfree; a++)
    {
        if (ios_pool_free[a].off + ios_pool_free[a].len >= ios_pool_bump) return 0;
        for (b = a + 1; b < (int)ios_pool_nfree; b++)
            if (ios_pool_free[a].off <= ios_pool_free[b].off + ios_pool_free[b].len &&
                ios_pool_free[b].off <= ios_pool_free[a].off + ios_pool_free[a].len) return 0;
    }
    return 1;
}

int main( void )
{
    char path[] = "/tmp/madeira-helpermem-XXXXXX";
    struct file_view v = { 0, 0, 0 }, v2 = { 0, 0, 0 }, v3 = { 0, 0, 0 }, v4 = { 0, 0, 0 }, v5 = { 0, 0, 0 };
    char *r, *r2, *r3, *r4, *r5;
    uint64_t base;
    unsigned long long refused;
    int i, fd = mkstemp( path );
    if (fd < 0) return 2;
    close( fd );
    setenv( "MADEIRA_SWAP_FILE", path, 1 );
    setenv( "MADEIRA_SWAP_MB", "64", 1 );   /* what the app arms the tier with when only mempool-mb is set */
    setenv( "MADEIRA_SWAP_COVERAGE", "broad", 1 );
    setenv( "MADEIRA_POOL_MARGIN_MB", "0", 1 );   /* the guard is the device's free RAM; not tested here */
    setenv( "MADEIRA_POOL_MIN_KB", "1024", 1 );   /* reservations under broad's floor: opt-in */
    ios_swap_init();
    if (ios_swap_fd < 0 || !ios_swap_broad) { printf( "FAIL: the tier did not start in broad\n" ); return 1; }
    unlink( path );

    /* the extension's regions: named, owned memory entries nothing has touched */
    for (i = 0; i < NREG; i++)
    {
        memory_object_size_t size = REGION;
        mach_port_t entry = MACH_PORT_NULL;
        vm_prot_t flags = VM_PROT_READ | VM_PROT_WRITE | MAP_MEM_NAMED_CREATE;
#ifdef MAP_MEM_LEDGER_TAGGED
        flags |= MAP_MEM_LEDGER_TAGGED;
#endif
        if (mach_make_memory_entry_64( mach_task_self(), &size, 0, flags, &entry, MACH_PORT_NULL ) != KERN_SUCCESS)
        { printf( "FAIL: mach_make_memory_entry_64\n" ); return 2; }
        CHECK( madeira_pool_add( entry, REGION ) == 0, "a region is registered" );
    }
    CHECK( ios_pool_nreg == NREG && ios_pool_bump == 0, "four 32 MB regions, nothing handed out" );

    /* 1. a reservation of 80 MB, two and a half regions: mapped from the pool whole, in pool order */
    r = region( 0x7050000000ULL, 80 * MB );
    v.base = r; v.size = 80 * MB;
    CHECK( ios_swap_whole_resv( &v, VPROT_READ | VPROT_WRITE ) == 1 && ios_swap_n == 1 && ios_swap_ext[0].resv, "an 80 MB reservation is mapped" );
    CHECK( is_pool( r ) && is_pool( r + 79 * MB ), "... from the pool, though it is larger than a region (it was the file)" );
    CHECK( ios_pool_bytes == 80 * MB && ios_pool_takes == 1 && ios_pool_bump == 80 * MB, "... one take of 80 MB" );
    base = ios_swap_ext[0].off & ~IOS_POOL_FLAG;
    commit( r, 80 * MB, &v );
    for (i = 0; i < 80; i++) memset( r + (size_t)i * MB, 0x10 + i, MB );
    {
        int ok = 1;
        for (i = 0; i < 80; i++) if (*pool_at( base + (uint64_t)i * MB ) != (char)(0x10 + i) || pool_at( base + (uint64_t)i * MB )[MB - 1] != (char)(0x10 + i)) ok = 0;
        CHECK( ok, "every MB written lands in the region and offset the pool gave it (pieces across three regions)" );
    }
    CHECK( ios_swap_live == 0, "pool data is not counted as file data" );

    /* 2. decommit 16 MB across the first region boundary: zeroed in place, still pool memory, the rest intact */
    CHECK( ios_swap_punch_resv( r + 24 * MB, 16 * MB ) == 1, "a decommit inside the pool reservation is handled in place (it was released)" );
    CHECK( all( r + 24 * MB, 16 * MB, 0 ), "the decommitted range reads zero" );
    CHECK( ios_pool_holes == 1 && ios_swap_n == 1 && is_pool( r + 30 * MB ), "... and stays in the pool extent" );
    memset( r + 24 * MB, 0x77, 16 * MB );
    CHECK( *pool_at( base + 24 * MB ) == 0x77 && *pool_at( base + 39 * MB ) == 0x77, "a recommit's writes land in the pool again, not in anonymous memory" );
    CHECK( all( r, MB, 0x10 ) && all( r + 23 * MB, MB, 0x10 + 23 ) && all( r + 40 * MB, MB, 0x10 + 40 ), "the data beside it is intact" );
    CHECK( ios_pool_bytes == 80 * MB, "a decommit gives nothing back to the pool" );

    /* 3. the swap file's disk past swap-mb (64 MB): the pool guard on, an 80 MB reservation from the file, written back */
    ios_pool_pressure = 2;
    r2 = region( 0x7060000000ULL, 80 * MB );
    v2.base = r2; v2.size = 80 * MB;
    CHECK( ios_swap_whole_resv( &v2, VPROT_READ | VPROT_WRITE ) == 1 && ios_swap_n == 2 && !is_pool( r2 ) && ext_of( r2 ) >= 0, "under pressure a reservation goes to the file" );
    CHECK( ios_pool_guarded >= 1, "... and the guard is counted" );
    commit( r2, 80 * MB, &v2 );
    memset( r2, 0x55, 80 * MB );
    msync( r2, 80 * MB, MS_SYNC );
    CHECK( ios_swap_disk_used() >= 64 * MB, "the file's disk has reached swap-mb" );
    ios_pool_pressure = 0;
    refused = ios_swap_disk_refused;
    r3 = region( 0x7070000000ULL, 32 * MB );
    v3.base = r3; v3.size = 32 * MB;
    CHECK( ios_swap_whole_resv( &v3, VPROT_READ | VPROT_WRITE ) == 1 && is_pool( r3 ), "with the disk full a new reservation still comes from the pool (it was refused)" );
    CHECK( ios_swap_disk_refused == refused && ios_pool_bytes == 112 * MB, "... nothing refused, the pool holds 112 MB" );

    /* 4. the pool has 16 MB left: a 32 MB reservation goes past it, and the disk cap refuses it as before */
    r4 = region( 0x7078000000ULL, 32 * MB );
    v4.base = r4; v4.size = 32 * MB;
    CHECK( ios_swap_whole_resv( &v4, VPROT_READ | VPROT_WRITE ) == 1 && ext_of( r4 ) < 0 && ios_swap_disk_refused == refused + 1, "past the pool, the disk cap refuses as before" );

    /* 5. a pool map that fails part-way: region 3's entry is a dead name (MACH_PORT_NULL would map fresh zero memory), a 16 MB
     * range in regions 2-3 */
    {
        mach_port_t keep = ios_pool_reg[3].entry;
        uint64_t off, bump = ios_pool_bump;
        char *z = region( 0x7080000000ULL, 16 * MB );
        mprotect( z, 16 * MB, PROT_READ | PROT_WRITE );   /* a fresh commit, as the callers map */
        ios_swap_release_range( r3, 32 * MB, 0 );   /* gives [80, 112) MB back: the bump pointer drops to 80 MB */
        CHECK( ios_pool_bump == 80 * MB && ios_pool_nfree == 0 && bump == 112 * MB, "a range at the bump pointer lowers it" );
        ios_pool_take( 8 * MB );                     /* [80, 88) MB: the next range starts 8 MB before region 3 */
        ios_pool_reg[3].entry = (mach_port_t)0x7ffffff3;
        off = ios_pool_take( 16 * MB );
        CHECK( off == 88 * MB, "the next range crosses into region 3" );
        CHECK( ios_pool_map( z, 16 * MB, PROT_READ | PROT_WRITE, off ) == -1, "the map fails at region 3" );
        memset( z, 0x66, 16 * MB );
        CHECK( *pool_at( 88 * MB ) == 0 && all( z, 16 * MB, 0x66 ), "the piece it had mapped is anonymous memory again: no write reaches the pool" );
        ios_pool_reg[3].entry = keep;
        ios_pool_give( off, 16 * MB );
        ios_pool_give( 80 * MB, 8 * MB );
        CHECK( ios_pool_bump == 80 * MB && ios_pool_nfree == 0, "given back, merged into the bump pointer" );
        ios_pool_bytes += 24 * MB;   /* give subtracts what take did not add here */
        munmap( z, 16 * MB );
    }

    /* 6. release the first reservation: the pool is empty except nothing; reused space reads zero */
    ios_swap_release_range( r, 80 * MB, 0 );
    CHECK( ios_pool_bump == 0 && ios_pool_nfree == 0 && ios_pool_bytes == 0, "released: the pool is empty again (bump 0, no free entries)" );
    r5 = region( 0x7090000000ULL, 64 * MB );
    v5.base = r5; v5.size = 64 * MB;
    CHECK( ios_swap_whole_resv( &v5, VPROT_READ | VPROT_WRITE ) == 1 && is_pool( r5 ) && (ios_swap_ext[ext_of( r5 )].off & ~IOS_POOL_FLAG) == 0, "a new reservation reuses the pool from the start" );
    commit( r5, 64 * MB, &v5 );
    CHECK( all( r5, 64 * MB, 0 ), "... and reads zero where the old data was" );
    ios_swap_release_range( r5, 64 * MB, 0 );
    ios_swap_release_range( r2, 80 * MB, 0 );
    CHECK( ios_swap_n == 0 && ios_pool_bytes == 0 && ios_pool_bump == 0, "everything released" );

    /* 7. randomized takes and gives (host-page sizes up to 48 MB, so many cross regions) against the model */
    {
        enum { N = 64 };
        uint64_t loff[N], llen[N];
        int n = 0, step, ok = 1;
        for (step = 0; step < 20000 && ok; step++)
        {
            if (n && (n == N || (next() & 1)))
            {
                int k = (int)(next() % n);
                ios_pool_give( loff[k], llen[k] );
                ios_pool_bytes += llen[k];
                loff[k] = loff[--n]; llen[k] = llen[n];
            }
            else
            {
                uint64_t len = ((next() % (48 * MB)) & ~(uint64_t)host_page_mask) + host_page_mask + 1, off = ios_pool_take( len );
                if (off != (uint64_t)-1) { loff[n] = off; llen[n] = len; n++; }
            }
            ok = model_ok( loff, llen, n );
        }
        CHECK( ok, "random takes and gives: live ranges apart, free ranges merged and below the bump pointer" );
        while (n) { n--; ios_pool_give( loff[n], llen[n] ); ios_pool_bytes += llen[n]; }
        CHECK( ios_pool_bump == 0 && ios_pool_nfree == 0 && ios_pool_lost == 0, "... and after everything is given back the pool is empty" );
    }

    /* 8. the guard (madeira-bcd, P3R build 622: 0 takes, 178 guarded). It counts what jetsam counts, free and file-backed
     * pages: a margin above the free pages alone but below free + file-backed lets a take through (free_count refused it). */
    {
        vm_statistics64_data_t vm;
        mach_msg_type_number_t count = HOST_VM_INFO64_COUNT;
        uint64_t off, closings = ios_pool_closings;
        host_statistics64( mach_host_self(), HOST_VM_INFO64, (host_info64_t)&vm, &count );
        if (vm.external_page_count > 2048)
        {
            ios_pool_margin = ((uint64_t)vm.free_count + vm.external_page_count / 2) * vm_kernel_page_size;
            off = ios_pool_take( 4 * MB );
            CHECK( off != (uint64_t)-1, "a margin above the free pages but below free + file-backed pages lets a take through" );
            if (off != (uint64_t)-1) { ios_pool_give( off, 4 * MB ); ios_pool_bytes += 4 * MB; }
        }
        else printf( "note: this Mac has %u file-backed pages; the margin check is skipped\n", vm.external_page_count );
        ios_pool_margin = ~0ull >> 1;
        CHECK( ios_pool_take( 4 * MB ) == (uint64_t)-1 && ios_pool_closings == closings + 1, "below the margin the guard closes, once" );
        CHECK( ios_pool_take( 4 * MB ) == (uint64_t)-1 && ios_pool_closings == closings + 1 && ios_pool_closed == 2, "... and counts refusals while closed" );
        ios_pool_margin = 0;
        ios_pool_pressure = 1;
        off = ios_pool_take( 4 * MB );
        CHECK( off != (uint64_t)-1 && !ios_pool_closed, "the system's warning level does not stop takes (the margin decides)" );
        ios_pool_give( off, 4 * MB ); ios_pool_bytes += 4 * MB;
        ios_pool_pressure = 2;
        CHECK( ios_pool_take( 4 * MB ) == (uint64_t)-1, "the system's critical level does" );
        ios_pool_pressure = 0;
    }

    /* 9. reservations under broad's 4 MB floor: from 1 MB, the pool takes them whole, and only the pool */
    {
        struct file_view v6 = { 0, 0, 0 }, v7 = { 0, 0, 0 };
        char *r6 = region( 0x70a0000000ULL, 2 * MB ), *r7 = region( 0x70a8000000ULL, 512 * 1024 );
        unsigned n = ios_swap_n;
        unsigned long long disk = ios_swap_disk_used(), refusals = ios_swap_refused;
        CHECK( ios_swap_min == 4 * MB && ios_pool_min == MB, "broad's floor is 4 MB, the pool's 1 MB" );
        v6.base = r6; v6.size = 2 * MB;
        ios_pool_pressure = 2;
        CHECK( ios_swap_whole_resv( &v6, VPROT_READ | VPROT_WRITE ) == 0 && ios_swap_n == n && ext_of( r6 ) < 0,
               "a 2 MB reservation the guard refuses stays anonymous (the commit rules decide)" );
        CHECK( ios_swap_disk_used() == disk && ios_swap_refused == refusals, "... it never reaches the file" );
        ios_pool_pressure = 0;
        CHECK( ios_swap_whole_resv( &v6, VPROT_READ | VPROT_WRITE ) == 1 && is_pool( r6 ) && ios_swap_ext[ext_of( r6 )].resv,
               "with the guard open it is taken whole from the pool" );
        CHECK( ios_pool_small_n == 1 && ios_pool_small_bytes == 2 * MB, "... and counted as under the file's floor" );
        commit( r6, 2 * MB, &v6 );
        memset( r6, 0x42, 2 * MB );
        CHECK( *pool_at( ios_swap_ext[ext_of( r6 )].off & ~IOS_POOL_FLAG ) == 0x42, "its data is in the pool" );
        v7.base = r7; v7.size = 512 * 1024;
        CHECK( ios_swap_whole_resv( &v7, VPROT_READ | VPROT_WRITE ) == 0 && ext_of( r7 ) < 0, "512 KB, under the pool's 1 MB, stays anonymous" );
        ios_swap_release_range( r6, 2 * MB, 0 );
        CHECK( ios_pool_bytes == 0 && ios_pool_bump == 0, "released, the pool is empty again" );
        munmap( r7, 512 * 1024 );
    }

    /* 10. the margin and the floor come from the env when the tier starts, after the app has exported bcd.cfg's env lines
     * (madeira_pool_add ran before that, so a bcd.cfg MADEIRA_POOL_MARGIN_MB never reached it) */
    setenv( "MADEIRA_POOL_MARGIN_MB", "777", 1 );
    setenv( "MADEIRA_POOL_MIN_KB", "256", 1 );
    ios_pool_config();
    CHECK( ios_pool_margin == 777 * MB && ios_pool_min == 256 * 1024, "ios_pool_config reads MADEIRA_POOL_MARGIN_MB and MADEIRA_POOL_MIN_KB" );
    unsetenv( "MADEIRA_POOL_MIN_KB" );
    ios_pool_config();
    CHECK( ios_pool_min == 0, "unset, the pool takes no reservation under the file's floor (opt-in until a device run)" );
    {
        struct file_view v8 = { 0, 0, 0 };
        char *r8 = region( 0x70b0000000ULL, 2 * MB );
        v8.base = r8; v8.size = 2 * MB;
        CHECK( ios_swap_whole_resv( &v8, VPROT_READ | VPROT_WRITE ) == 0 && ext_of( r8 ) < 0, "... a 2 MB reservation stays anonymous" );
        munmap( r8, 2 * MB );
    }
    ios_pool_margin = 0;

    /* 11. space whose zeroing fails is never handed out again (a fresh reservation must read zero): region 0's window is
     * pointed at nothing, so MADV_ZERO fails */
    {
        char *keep = ios_pool_reg[0].window;
        uint64_t off = ios_pool_take( 4 * MB ), off2;
        CHECK( off == 0 && ios_pool_bump == 4 * MB, "a 4 MB take from an empty pool starts at 0" );
        ios_pool_bytes += 4 * MB;
        ios_pool_reg[0].window = (char *)(uintptr_t)0x1000;
        ios_pool_give( off, 4 * MB );
        ios_pool_reg[0].window = keep;
        CHECK( ios_pool_lost == 4 * MB && ios_pool_nfree == 0 && ios_pool_bump == 4 * MB && ios_pool_bytes == 0,
               "a range that could not be zeroed is counted as lost: not on the free list, the bump pointer stays" );
        off2 = ios_pool_take( 4 * MB );
        CHECK( off2 == 4 * MB, "... and the next take does not get it back" );
        ios_pool_give( off2, 4 * MB ); ios_pool_bytes += 4 * MB;
    }

    ios_swap_stats_line();
    printf( "%d failures\n", bad );
    return bad != 0;
}
'''

if sys.platform != 'darwin':
    print('note: the runtime part needs Darwin (mach memory entries, F_PUNCHHOLE); source checks only')
else:
    with tempfile.TemporaryDirectory() as tmp:
        c = Path(tmp) / 'helpermem.c'
        exe = Path(tmp) / 'helpermem'
        c.write_text(prelude + core + harness)
        b = subprocess.run(['cc', '-O1', '-Wall', '-Wno-unused-function', '-Wno-deprecated-declarations', '-o', str(exe), str(c)],
                           capture_output=True, text=True)
        check(b.returncode == 0, 'the swap-tier core compiles with the harness' + ('' if b.returncode == 0 else ':\n' + b.stderr[-4000:]))
        if b.returncode == 0:
            r = subprocess.run([str(exe)], capture_output=True, text=True)
            fails = [l for l in r.stdout.splitlines() if l.startswith('FAIL')]
            check(r.returncode == 0 and not fails, 'runtime: %d checks' % r.stdout.count('ok: ') + ('' if not fails else ':\n' + '\n'.join(fails)))
            err = r.stderr
            check(re.search(r'^\[swap\] ml1150 pool: decommit 0x[0-9a-f]+\+0x1000000 zeroed in place \(1\)$', err, re.M) is not None,
                  'the first in-place decommit is logged')
            check(re.search(r'^\[swap\] ml1150 pool: zeroing 0x0\+0x400000 failed errno=\d+; that space is not used again$', err, re.M) is not None,
                  'a failed zeroing is logged')
            check(re.search(r'^\[swap\] ml1150 pool: \d+ MB in use \(peak 112\), \d+ takes, \d+ guarded, 1 decommits zeroed in place, 4 MB lost \(free list full, or not zeroed\)', err, re.M)
                  is not None, 'the stats line carries the peak and the in-place decommits')
            check(re.search(r"\| 1 reservations under the file's floor \(2 MB\) \| system has \d+ MB available$", err, re.M) is not None,
                  "the stats line counts the reservations under the file's floor and what the system has available")
            check(re.search(r'^\[swap\] ml1150 pool: guard closed \(\d+ MB available, margin 0 MB, system pressure 2\); \d+ MB in use$', err, re.M)
                  is not None, 'the guard logs why it closed')
            check(re.search(r'^\[swap\] ml1150 pool: guard open again after \d+ refusals \(\d+ MB available\)$', err, re.M) is not None,
                  '... and when it opens again')
            check(re.search(r'^\[swap\] ml1150 pool: 4 regions; takes stop below 777 MB available \(now \d+ MB\) or at critical system '
                            r'pressure; reservations under the file\'s floor: from 256 KB', err, re.M) is not None, 'ios_pool_config logs the guard it set')
            check(re.search(r'^\[swap\] ml1077 backed 0x70a0000000\+0x200000 \(whole reservation, helper memory only\)', err, re.M) is not None,
                  'a reservation under the floor is logged as helper memory only')

# --------------------------------------------------------------- source checks
take = body_of(virt, 'static uint64_t ios_pool_take( size_t len )\n{')
check('ios_pool_region)' not in take.split('ios_pool_guarded++')[0], 'a take is not limited to one region')
smap = body_of(virt, 'static int ios_swap_map( void *base, size_t size, int unix_prot, int committed )\n{')
pool_at, disk_at = smap.find('ios_pool_take( len )'), smap.find('ios_swap_disk_used() >= ios_swap_cap')
check(0 <= pool_at < disk_at, 'ios_swap_map asks the pool before the disk cap')
punch = body_of(virt, 'static int ios_swap_punch_resv( char *lo, size_t len )\n{')
check('if (ios_pool_zero( off, len ))' in body_of(virt, 'static void ios_pool_give( uint64_t off, size_t len )\n{'),
      'space that could not be zeroed is not handed out again')
check('ios_pool_zero(' in punch and 'if (ios_swap_ext[i].off & IOS_POOL_FLAG) return 0;' not in punch,
      'a decommit inside a pool reservation is zeroed in place')
check('pressure >= 2 || avail < ios_pool_margin' in take, 'only the system\'s critical level or the margin closes the guard')
check('vm.free_count + vm.external_page_count' in body_of(virt, 'static uint64_t ios_pool_system_free( void )\n{'),
      'the guard counts free and file-backed pages, as jetsam does')
check('getenv' not in body_of(virt, 'int madeira_pool_add( unsigned int entry, unsigned long long size )\n{'),
      'madeira_pool_add reads no env (it runs before bcd.cfg\'s env lines are exported)')
whole = body_of(virt, 'static int ios_swap_whole_resv( struct file_view *view, unsigned int vprot )\n{')
check('ios_swap_pool_only = pool_only;' in whole and 'view->size < ios_pool_min' in whole, 'broad takes reservations under its floor from the pool only')
check('if (ios_swap_pool_only) return IOS_SW_SMALL;' in smap and smap.index('if (ios_swap_pool_only)') < disk_at,
      '... and never from the file')
pool_m = (root / 'app/Madeira/MemoryPool.m').read_text()
src = body_of(pool_m, 'static void MPStartPressureSource(void) {')
check('#define MP_MEMORYPRESSURE_PROC_LIMIT_WARN     0x10' in pool_m and '#define MP_MEMORYPRESSURE_PROC_LIMIT_CRITICAL 0x20' in pool_m,
      "MemoryPool.m names libdispatch's process-limit bits")
check('system | MP_MEMORYPRESSURE_PROC_LIMIT_WARN | MP_MEMORYPRESSURE_PROC_LIMIT_CRITICAL' in src,
      "the pressure source asks for BCD's own limit events apart from the system's levels")
limit = src[src.index('if (level & (MP_MEMORYPRESSURE_PROC_LIMIT_WARN'):]
limit = limit[:limit.index('return;')]
check('madeira_pool_set_pressure' not in limit, "BCD's own limit events never reach the guard")
check('!split && system_level == 2 ? 1 : system_level' in src, 'without the split a CRITICAL reaches the guard as a warning')
bridge = (root / 'app/Madeira/WineProcessBridge.m').read_text()
check(re.search(r'if \(capMB < 64 && poolMB >= 512\) \{\s*capMB = 64;', bridge) is not None, 'mempool-mb arms the tier without swap-mb')
check(bridge.index('long poolMB = (long)madeira_cfg_int("mempool-mb", 0);') < bridge.index('if (capMB >= 64) {'),
      'mempool-mb is read before the tier is armed')
sections = (root / 'app/Madeira/BCDSettingsSections.swift').read_text()
row = sections[sections.index('row(.memoryPool, "Helper memory"'):]
row = row[:row.index(')\n')]
check('enabled:' not in row and 'Works without the swap file' in row, 'Settings: Helper memory does not need the swap file')
check('BCDSettingsInfoRow(title: "Helper memory adds"' in sections and 'value: "Up to " + memoryText(poolMB << 20)' in sections,
      'Settings › This device shows what helper memory adds to the limit')
wf = (root / '.github/workflows/build-ipa.yml').read_text()
check('python3 tests/host/check-helper-memory.py' in wf, 'CI runs this check')

if failures:
    print('FAILED: helper memory')
    sys.exit(1)
print('PASS: helper memory backs reservations across regions, past a full swap file and through decommits')
