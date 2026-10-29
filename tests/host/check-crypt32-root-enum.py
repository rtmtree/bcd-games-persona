#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 thebcd69 (BCD)
# BCD Material: additional terms under GPL-3.0 section 7 apply; see LICENSE-BCD.md.
"""crypt32: every pseudo-process of a session gets all of the bundled root certificates (build 622); no Wine runs.

On iOS one crypt32 unix side (build/crypto-unix/crypt32_unixlib_ios.c) serves every Wine process of the session, and its list
of the bundled Mozilla roots (cacert.pem, 121 certificates) is loaded once for the app's life. The 64-bit enum_root_certs
removed each certificate it handed out, as upstream does in a process of its own; only the WoW64 call walked the list with a
per-thread position. So the first 64-bit process imported the roots, the next one found the list empty, and Wine's
rootstore.c (sync_trusted_roots_from_known_locations) took that for roots deleted on the host: it removed every imported root
from HKLM\\Software\\Microsoft\\SystemCertificates\\Root, leaving the prefix with the five built-in Microsoft roots. Persona 3
Reload (Steam 2161700, iPhone18,4, build 622) then failed Denuvo's activation: winhttp to srv01.codefusion.technology could not
build a chain to Amazon Root CA 1 / Starfield Services Root CA - G2, chased the dead AIA URL http://x.ss2.us/x.cer, and the game
showed "The server is not reachable". The prefix's system.reg had five Root certificates and an empty
Software\\Wine\\HostImportedCertificates key.

Both calls now share ios_enum_root_certs_cursor. The list code and the enumeration are cut out of the source and compiled on
the host, and rootstore.c's loop (a 2048-byte buffer grown when a certificate needs more) is run as several processes would:
  - one process after another on the same thread, and on different threads, each get every certificate, in order, byte for byte;
  - two processes importing at the same time on two threads each get every certificate;
  - a certificate bigger than the first buffer comes again after the buffer grows, and is not skipped;
  - the list still holds every certificate afterwards;
and the source no longer frees a certificate while enumerating, with both entries using the cursor.
"""
from pathlib import Path
import re
import subprocess
import sys
import tempfile

root = Path(__file__).resolve().parents[2]
ok = True


def check(what, cond):
    global ok
    print(('ok   ' if cond else 'FAIL ') + what)
    ok = ok and bool(cond)


src_path = root / 'build/crypto-unix/crypt32_unixlib_ios.c'
src = src_path.read_text()


def cut(start, end_marker='\n}\n'):
    i = src.index(start)
    j = src.index(end_marker, i) + len(end_marker)
    return src[i:j]


def body(name):
    i = src.find('static NTSTATUS %s(' % name)
    return src[i:src.index('\n}\n', i) + 3] if i >= 0 else ''


# ---- the source ---------------------------------------------------------------------------------------------------
enum64 = body('enum_root_certs')
wow = body('wow64_enum_root_certs')
check('the 64-bit enum_root_certs walks with the per-thread cursor', 'return ios_enum_root_certs_cursor( args );' in enum64)
check('the WoW64 enum_root_certs walks with the same cursor', 'return ios_enum_root_certs_cursor( &params );' in wow)
cursor = body('ios_enum_root_certs_cursor')
check('the cursor enumeration is there', cursor)
check('no enumeration removes or frees a certificate',
      not any(w in fn for fn in (enum64, cursor) for w in ('list_remove', 'free(')))
check('the old WoW64-only name is gone', 'ios_enum_root_certs_wow' not in src)
check('both tables list enum_root_certs / wow64_enum_root_certs',
      re.search(r'__wine_unix_call_funcs\[\] =\s*\{[^}]*\benum_root_certs,', src, re.S) and
      re.search(r'__wine_unix_call_wow64_funcs\[\] =\s*\{[^}]*\bwow64_enum_root_certs,', src, re.S))

# ---- compiled on the host -----------------------------------------------------------------------------------------
wine_list = root / 'wine/include/wine/list.h'
if not wine_list.exists():
    print('note SKIPPED the host run: no Wine sources here (%s)' % wine_list)
else:
    pieces = (cut('struct root_cert\n{', '};\n') + '\n' +
              cut('static struct list root_cert_list', ';\n') + '\n' +
              cut('static BYTE *add_cert(') + '\n' +
              src[src.index('#include <pthread.h>'):src.index('const unixlib_entry_t __wine_unix_call_funcs[]')])
    harness = r'''
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "@LIST@"
typedef unsigned char BYTE;
typedef size_t SIZE_T;
typedef int BOOL;
typedef unsigned int DWORD;
typedef unsigned int NTSTATUS;
#define TRUE 1
#define STATUS_SUCCESS 0
#define STATUS_NO_MORE_ENTRIES ((NTSTATUS)0x8000001A)
struct enum_root_certs_params { void *buffer; DWORD size; DWORD *needed; };

#define NCERTS 121
static SIZE_T cert_size(int i) { return i == 40 ? 3000 : 600 + (i * 37) % 1200; }   /* #40 needs a bigger buffer */
static BYTE cert_byte(int i, SIZE_T k) { return (BYTE)(i * 131 + k * 7 + 1); }
static int loads;
static void load_root_certs(void);
@PIECES@
static void load_root_certs(void)
{
    int i; SIZE_T k;
    loads++;
    for (i = 0; i < NCERTS; i++)
    {
        SIZE_T n = cert_size(i);
        BYTE *d = add_cert(n);
        for (k = 0; k < n; k++) d[k] = cert_byte(i, k);
    }
}

/* rootstore.c's sync_trusted_roots_from_known_locations loop: count the certificates and check every byte. */
static int import(int *bad, int *retries)
{
    DWORD needed, size = 2048;
    struct enum_root_certs_params params = { malloc(2048), 2048, &needed };
    int n = 0; SIZE_T k;
    *bad = 0; *retries = 0;
    while (!enum_root_certs(&params))
    {
        if (needed > params.size)
        {
            free(params.buffer);
            params.buffer = malloc(needed);
            params.size = size = needed;
            ++*retries;
            continue;
        }
        if (needed != cert_size(n)) ++*bad;
        else for (k = 0; k < needed; k++) if (((BYTE *)params.buffer)[k] != cert_byte(n, k)) { ++*bad; break; }
        n++;
        if ((n & 7) == 0) sched_yield();
    }
    free(params.buffer);
    return n;
}

struct run { int n, bad, retries; };
static void *thread_import(void *arg) { struct run *r = arg; r->n = import(&r->bad, &r->retries); return NULL; }
static void report(const char *what, struct run *r) { printf("%s n=%d bad=%d retries=%d\n", what, r->n, r->bad, r->retries); }

int main(void)
{
    struct run a, b, c, d, e;
    pthread_t t1, t2, t3;
    struct list *p; int left = 0;

    a.n = import(&a.bad, &a.retries); report("first-process", &a);           /* dockhost.exe, say */
    b.n = import(&b.bad, &b.retries); report("next-process-same-thread", &b);
    pthread_create(&t1, NULL, thread_import, &c); pthread_join(t1, NULL);
    report("next-process-other-thread", &c);                                    /* the game */
    pthread_create(&t2, NULL, thread_import, &d);
    pthread_create(&t3, NULL, thread_import, &e);
    pthread_join(t2, NULL); pthread_join(t3, NULL);
    report("concurrent-1", &d); report("concurrent-2", &e);
    LIST_FOR_EACH(p, &root_cert_list) left++;
    printf("left=%d loads=%d\n", left, loads);
    return 0;
}
'''.replace('@LIST@', str(wine_list)).replace('@PIECES@', pieces)
    with tempfile.TemporaryDirectory() as tmp:
        c = Path(tmp) / 't.c'
        c.write_text(harness)
        exe = Path(tmp) / 't'
        cc = subprocess.run(['cc', '-O1', '-g', '-Wall', '-Wno-unused-function', '-pthread', '-fsanitize=address,undefined',
                             '-o', str(exe), str(c)], capture_output=True, text=True)
        if cc.returncode:   # a host without the sanitizers
            cc = subprocess.run(['cc', '-O1', '-Wall', '-Wno-unused-function', '-pthread', '-o', str(exe), str(c)],
                                capture_output=True, text=True)
        check('the list code and both enumerations compile on the host', cc.returncode == 0)
        if cc.returncode:
            print(cc.stderr)
        else:
            run = subprocess.run([str(exe)], capture_output=True, text=True)
            out = run.stdout
            print('     ' + out.replace('\n', '\n     ').rstrip())
            check('it runs to the end with no sanitizer report', run.returncode == 0 and 'ERROR' not in run.stderr)
            rows = {m.group(1): tuple(map(int, m.group(2, 3, 4)))
                    for m in re.finditer(r'^(\S+) n=(\d+) bad=(\d+) retries=(\d+)$', out, re.M)}

            def full(name):
                return rows.get(name) == (121, 0, 1)

            check('the first process gets all 121 certificates, byte for byte (one buffer growth for the big one)',
                  full('first-process'))
            check('the next process on the same thread gets all 121 again (it found none before the fix)',
                  full('next-process-same-thread'))
            check('the next process on another thread gets all 121 too', full('next-process-other-thread'))
            check('two processes importing at the same time each get all 121', full('concurrent-1') and full('concurrent-2'))
            check('the list keeps every certificate and is loaded once', 'left=121 loads=1' in out)

wf = (root / '.github/workflows/build-ipa.yml').read_text()
check('CI runs this check', 'python3 tests/host/check-crypt32-root-enum.py' in wf)
print('PASS: crypt32 root enumeration' if ok else 'FAILED: crypt32 root enumeration')
sys.exit(0 if ok else 1)
